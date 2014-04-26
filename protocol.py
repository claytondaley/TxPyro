import logging
log = logging.getLogger("twisted-pyro.server")
log.debug("Loading Pyro Protocol module for Twisted")

import os
import struct
import sys
import uuid

import Pyro4
from Pyro4 import util, errors, message
from Pyro4.futures import _ExceptionWrapper
from Pyro4.core import pyroObjectToAutoProxy
from Pyro4.message import Message

from proxy import PyroDeferredService

from twisted.internet import reactor, defer
from twisted.python.failure import Failure
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.internet.protocol import Protocol, Factory, ClientFactory

from pprint import pformat

__author__ = 'Clayton Daley'


class Pyro4NSClientFactory(ClientFactory):
    """
    Access to the nameserver (NS) is critical in simulating standard Pyro4 connection behavior.  Because we leverage
    the existing Pyro codebase (wrapped in proxy.PyroDeferredService), we don't want to imply that the user can provide
    alternate transport.  Instead, we deliver this functionality as a service that returns Deferreds.
    """

    # Our best guess of the server's preferred interface in case a user does not provide one to us
    _service_host = None

    def __init__(self):
        pass

    def buildProtocol(self, addr=None):
        """Create an instance of a subclass of Protocol.

        The returned instance will handle input on an incoming server
        connection, and an attribute \"factory\" pointing to the creating
        factory.

        @param addr: an object implementing L{twisted.internet.interfaces.IAddress}
        """
        if addr is not None:
            ns_host, ns_port = addr
        # Needs rewritten to run asynchronously
        proxy = Pyro4.locateNS(ns_host, ns_port)
        proxy._factory = self
        PyroDeferredService(proxy)
        # Get the server's host by leveraging the nameserver's IP address
        self._service_host = Pyro4.socketutil.getInterfaceAddress(proxy._pyroUri.host)
        return self.ns


class Pyro4Protocol(Protocol):
    def __init__(self):
        self.request = None
        self.response_flags = 0
        self.data = ''
        self.state = 'uninitialized'

    def connectionMade(self):
        """
        Handshake - should replicate Pyro4.Daemon._handshake()
        """
        if self.state == "server":
            log.info("Connection made with Pyro4Protocol")
            log.info("... attempting handshake")
            ser = util.get_serializer("marshal")
            data = ser.dumps("ok")
            msg = Message(Pyro4.message.MSG_CONNECTOK, data, ser.serializer_id, 0, 1)
            self.transport.write(msg.to_bytes())
            self.state = "header"

    def dataReceived(self, data):
        """
        This function must aggregate and standardize the I/O behavior of several Pyro4 functions:

         - Daemon.handleRequest
         - Message.recv
         - Protocol._pyroInvoke

        Due to differences between synchronous and asynchronous approaches, twisted-pyro uses states to determine how
        to route received data.  Because state data need not persist across connections (unlike state information in
        many applications), it is attached to the Protocol.  These states are:

         - server:  indicates that a handshake will be required upon connection
         - header:  waiting on enough data to parse a message header and respond accordingly (idle state for servers)
         - annotations:  header parsed, waiting on amount of annotation data requested in header
         - data:  header parsed, waiting on amount of data requested in header
         - response:  the other end is waiting for data from us (idle state for clients)
        """
        log.debug("Handling %d bytes of data" % len(data))
        self.data += data
        if self.state == "header" and len(self.data) >= Message.header_size:
            log.debug("... enough data received to process header.")
            # Have enough data to process headers
            self.request = Message.from_header(self.data[:Message.header_size])
            if Pyro4.config.LOGWIRE:
                log.debug("wiredata received: msgtype=%d flags=0x%x ser=%d seq=%d data=%r" %
                          (self.request.type, self.request.flags, self.request.serializer_id, self.request.seq, self.request.data))
            if self.required_message_types and self.request.type not in self.required_message_types:
                err = "invalid msg type %d received" % self.request.type
                log.error(err)
                self._return_error(errors.ProtocolError(err))
            if self.request.serializer_id not in \
                    set([util.get_serializer(ser_name).serializer_id
                         for ser_name in Pyro4.config.SERIALIZERS_ACCEPTED]):
                self._return_error(errors.ProtocolError("message used serializer that is not accepted: %d" % self.request.serializer_id))
            self.data = self.data[Message.header_size:]
            if self.request.annotations_size:
                self.state = "annotations"
            else:
                self.state = "data"

        if self.state == "annotations" and len(self.data) >= self.request.annotations_size:
            log.debug("... enough data received to process annotation.")
            self.request.annotations = {}
            annotations_data = self.data[:self.request.annotations_size]
            self.data = self.data[self.request.annotations_size:]
            i = 0
            while i < self.request.annotations_size:
                anno, length = struct.unpack("!4sH", annotations_data[i:i+6])
                self.request.annotations[anno] = annotations_data[i+6:i+6+length]
                i += 6+length
            if b"HMAC" in self.request.annotations and Pyro4.config.HMAC_KEY:
                if self.request.annotations[b"HMAC"] != self.request.hmac():
                    self._return_error(errors.SecurityError("message hmac mismatch"))
            elif (b"HMAC" in self.request.annotations) != bool(Pyro4.config.HMAC_KEY):
                # Message contains hmac and local HMAC_KEY not set, or vice versa. This is not allowed.
                err = "hmac key config not symmetric"
                log.warning(err)
                self._return_error(errors.SecurityError(err))
            self.state = "data"

        if self.state == "data" and len(self.data) >= self.request.data_size:
            log.debug("... enough data received to process data.")
            # A oneway call can be immediately followed by another call.  Otherwise, we should not receive any
            # additional data until we have sent a response
            if self.request.flags & Pyro4.message.FLAGS_ONEWAY:
                if self.request.type == message.MSG_PING:
                    error_msg = "ONEWAY ping doesn't make sense"
                    self._return_error(errors.ProtocolError(error_msg))
            else:
                if len(self.data) > self.request.data_size:
                    self.transport.loseConnection()
                    error_msg = "max message size exceeded (%d where max=%d)" % \
                                (self.request.data_size + self.request.annotations_size, Pyro4.config.MAX_MESSAGE_SIZE)
                    self._return_error(errors.ProtocolError(error_msg))

            # Transfer data to message
            self.request.data = self.data[:self.request.data_size]
            self.data = self.data[self.request.data_size:]

            # Execute message
            d = Deferred()

            if self.request.type == message.MSG_CONNECT:
                raise NotImplementedError("No action provided for MSG_CONNECT")

            elif self.request.type == message.MSG_CONNECTOK:
                # We only reach this spot if it is a valid message type so we're in client handshake mode.  Update
                # protocol to support client __call__ execution (i.e. invocation)
                self.required_message_types = [message.MSG_CONNECTOK]
                raise NotImplementedError("No action provided for MSG_CONNECTOK")

            elif self.request.type == message.MSG_CONNECTFAIL:
                raise NotImplementedError("No action provided for MSG_CONNECTFAIL")

            elif self.request.type == message.MSG_INVOKE:
                log.debug("Responding to invoke request.")
                # Must be a static method so the Protocol can reset after oneway messages
                d.addCallback(self._pyro_remote_call)
                reactor.callLater(0, d.callback, self.request)

            elif self.request.type == message.MSG_PING:
                log.debug("Responding to ping request.")
                reactor.callLater(0, d.callback, b"pong")

            elif self.request.type == message.MSG_RESULT:
                # Trigger callback with data or raise exception
                raise NotImplementedError("No action provided for MSG_RESULT")

            if self.request.flags & Pyro4.message.FLAGS_ONEWAY:
                log.debug("... ONEWAY request, not building response.")

                def reraise(response):
                    if isinstance(response, Exception):
                        log.exception("ONEWAY call resulted in an exception: %s" % str(response))
                        return Failure(response)
                d.addCallback(reraise)

                # In Pyro4 core, the ONEWAY_THREADED option determines whether or not the execution order of ONEWAY
                # calls is guaranteed. To replicate this behavior, we reference the same flag.
                if Pyro4.config.ONEWAY_THREADED:
                    self.reset()
                else:
                    self.state = "blocked"

                    def process_next_message(response):
                        self.reset()
                        return response
                    d.addBoth(process_next_message)
            else:
                log.debug("... appending response callbaccks.")
                # If the previous call was not oneway, we maintain state on the protocol
                log.debug("... setting state to 'response'")
                self.state = "response"
                log.debug("... adding build/send callbacks")
                d.addCallback(self._build_response)
                d.addCallback(self._send_response)

        if self.state == "response" and len(self.data) > 0:
            error_msg = "data received while in response state" % \
                        (self.request.data_size + self.request.annotations_size, Pyro4.config.MAX_MESSAGE_SIZE)
            self._return_error(errors.ProtocolError(error_msg))

        if self.state == "blocked":
            # Waiting on a ONEWAY call (with guaranteed execution order) to complete.  When it completes, the system
            # will reset itself to accept the next call.
            pass

    @inlineCallbacks
    def _pyro_remote_call(self, msg):
        result = []

        # Deserialize
        serializer = util.get_serializer_by_id(msg.serializer_id)
        objId, method, vargs, kwargs = serializer.deserializeCall(msg.data, compressed=msg.flags & Pyro4.message.FLAGS_COMPRESSED)

        # Sanitize kwargs
        if kwargs and sys.version_info < (2, 6, 5) and os.name != "java":
            # Python before 2.6.5 doesn't accept unicode keyword arguments
            kwargs = dict((str(k), kwargs[k]) for k in kwargs)

        # Individual or batch
        log.debug("Searching for object %s" % str(objId))
        obj = self.factory.objectsById.get(objId)
        log.debug("Found object with type %s" % str(type(obj)))
        if msg.flags & Pyro4.message.FLAGS_BATCH:
            for method, vargs, kwargs in vargs:
                log.debug("Running call %s with vargs %s and kwargs %s agasint object %s" %
                          (str(method), str(vargs), str(kwargs), str(obj)))
                response = yield Pyro4Protocol._pyro_run_call(obj, method, vargs, kwargs)
                if isinstance(response, Exception):
                    response = _ExceptionWrapper(response)
                result.append(response)
            # Return the final value
        else:
            result = Pyro4Protocol._pyro_run_call(obj, method, vargs, kwargs)

        log.debug("Returning result %s from _remote_call" % pformat(result))
        defer.returnValue(result)

    @staticmethod
    def _pyro_run_call(obj, method, vargs, kwargs):
        try:
            method = util.resolveDottedAttribute(obj, method, Pyro4.config.DOTTEDNAMES)
            return method(*vargs, **kwargs)
        except Exception:
            xt, xv = sys.exc_info()[0:2]
            log.debug("Exception occurred while handling request: %s", xv)
            tblines = util.formatTraceback(detailed=Pyro4.config.DETAILED_TRACEBACK)
            xv._pyroTraceback = tblines
            if sys.platform == "cli":
                util.fixIronPythonExceptionForPickle(xv, True)  # piggyback attributes
            return xv

    def _return_error(self, error):
        d = Deferred()
        reactor.callLater(0, d.callback, error)
        d.addCallback(self._build_response)
        d.addCallback(self._send_response)
        self.reset(True)
        return d

    def _build_response(self, result):
        # Determine appropriate response type
        if self.request.type == message.MSG_PING:
            msg_type = message.MSG_PING
        elif self.request.type == message.MSG_INVOKE:
            msg_type = message.MSG_RESULT
        else:
            err = "Attempting to respond to invalid message type."
            log.exception(err)
            raise errors.ProtocolError(err)

        flags = 0

        # Serialize and set flags
        serializer = util.get_serializer_by_id(self.request.serializer_id)
        try:
            data, compressed = serializer.serializeData(result)
        except:
            # the exception object couldn't be serialized, use a generic PyroError instead
            xt, xv, tb = sys.exc_info()
            msg = "Error serializing exception: %s. Original exception: %s: %s" % (str(xv), type(xv), str(xv))
            exc_value = errors.PyroError(msg)
            exc_value._pyroTraceback = tb
            if sys.platform == "cli":
                util.fixIronPythonExceptionForPickle(exc_value, True)  # piggyback attributes
            data, compressed = serializer.serializeData(exc_value)
        if compressed:
            flags |= message.FLAGS_COMPRESSED
        if self.request.flags & Pyro4.message.FLAGS_BATCH:
            flags |= Pyro4.message.FLAGS_BATCH

        if isinstance(result, Exception):
            flags = message.FLAGS_EXCEPTION
            if Pyro4.config.LOGWIRE:
                log.debug("daemon wiredata sending (error response): msgtype=%d flags=0x%x ser=%d seq=%d data=%r" %
                          (msg_type, flags, serializer.serializer_id, self.request.seq, data))
        elif self.request.type == message.MSG_PING or self.request.type == message.MSG_INVOKE:
            if Pyro4.config.LOGWIRE:
                log.debug("daemon wiredata sending: msgtype=%d flags=0x%x ser=%d seq=%d data=%r" %
                          (msg_type, flags, serializer.serializer_id, self.request.seq, data))

        return Message(msg_type, data, serializer.serializer_id, flags, self.request.seq)

    def _send_response(self, msg):
        log.info("In state %s" % self.state)
        if self.state != "response":
            error_msg = "Attempted to send response while protocol is not in response state)"
            raise errors.ProtocolError(error_msg)
        else:
            self.transport.write(msg.to_bytes())
            self.reset()
            # do work
            # append to end of callback chain to fix state

    """
    Client Call Reference

    The incoming request follows the following sequence (from Proxy._pyroInvoke)

    # Construct Message
    serializer = util.get_serializer(Pyro4.config.SERIALIZER)
    data, compressed = serializer.serializeCall(
        self._pyroConnection.objectId, methodname, vargs, kwargs,
        compress=Pyro4.config.COMPRESSION)
    if compressed:
        flags |= Pyro4.message.FLAGS_COMPRESSED
    if methodname in self._pyroOneway:
        flags |= Pyro4.message.FLAGS_ONEWAY

    # Send message, tracking sequence
    self._pyroSeq=(self._pyroSeq+1)&0xffff
    msg = Message(Pyro4.message.MSG_INVOKE, data, serializer.serializer_id, flags, self._pyroSeq)
    # _pyroConnection is a "raw data" SocketConnection
    self._pyroConnection.send(msg.to_bytes())

    # Either get response or return None (for oneway)
    if flags & Pyro4.message.FLAGS_ONEWAY:
        return None    # oneway call, no response data
    else:
        msg = Message.recv(self._pyroConnection, [Pyro4.message.MSG_RESULT])

    # Validate  message
    if seq!=self._pyroSeq:
        raise errors.ProtocolError(err)
    if msg.serializer_id != serializer.serializer_id:
        raise errors.ProtocolError(error)

    # Unserialize
    data = serializer.deserializeData(msg.data, compressed=msg.flags & Pyro4.message.FLAGS_COMPRESSED)

    if msg.flags & Pyro4.message.FLAGS_EXCEPTION:
        if sys.platform=="cli":
            util.fixIronPythonExceptionForPickle(data, False)
        raise data
    else:
        return data


    For Reference:
        https://pythonhosted.org/Pyro4/api/message.html
    """

    def reset(self, reset_data=False):
        log.info("Protocol Reset")
        self.request = None
        self.state = "header"
        if reset_data:
            self.data = ''
        else:
            # If we're not resetting the data, it's possible that self.data already contains the entire next message.
            # In this case, no more data will be received to cause the message to be processed.  To ensure that this
            # does not happen, we also need to schedule a call to dataReceived (including no actual data).
            reactor.callLater(0, self.dataReceived, '')

    def connectionLost(self, reason):
        pass


# noinspection PyPep8Naming
class Pyro4ServerFactory(Factory):
    protocol = Pyro4Protocol
    objectsById = dict()

    def register(self, obj, objectId=None):
        """
        Register a Pyro object under the given id. Note that this object is now only
        known inside this daemon, it is not automatically available in a name server.
        This method returns a URI for the registered object.
        """
        if objectId:
            if not isinstance(objectId, basestring):
                raise TypeError("objectId must be a string or None")
        else:
            objectId = "obj_" + uuid.uuid4().hex  # generate a new objectId
        if hasattr(obj, "_pyroId") and obj._pyroId != "":  # check for empty string is needed for Cython
            raise errors.DaemonError("object already has a Pyro id")
        if objectId in self.objectsById:
            raise errors.DaemonError("object already registered with that id")
        # set some pyro attributes
        obj._pyroId = objectId
        obj._pyroDaemon = self
        if Pyro4.config.AUTOPROXY:
            # register a custom serializer for the type to automatically return proxies
            # we need to do this for all known serializers
            for ser in util._serializers.values():
                ser.register_type_replacement(type(obj), pyroObjectToAutoProxy)
        # register the object in the mapping
        self.objectsById[obj._pyroId] = obj
        log.info("Registered object of type %s to id %s" % (type(obj), str(obj._pyroId)))
        log.debug("objectsById is now %s" % pformat(self.objectsById))
        return self.uriFor(objectId)

    def buildProtocol(self, addr):
        log.debug("Building protocol on address %s" % str(addr))
        protocol = self.protocol()
        protocol.factory = self
        protocol.state = "server"
        protocol.required_message_types = [message.MSG_INVOKE, message.MSG_PING]
        return protocol

    def setAddress(self, host, port):
        self.locationStr = "%s:%s" % (host, port)

    def uriFor(self, objectOrId=None, nat=True):
        """
        Get a URI for the given object (or object id) from this daemon.
        Only a daemon can hand out proper uris because the access location is
        contained in them.
        Note that unregistered objects cannot be given an uri, but unregistered
        object names can (it's just a string we're creating in that case).
        If nat is set to False, the configured NAT address (if any) is ignored and it will
        return an URI for the internal address.
        """
        if not isinstance(objectOrId, basestring):
            objectOrId=getattr(objectOrId, "_pyroId", None)
            if objectOrId is None:
                raise errors.DaemonError("object isn't registered")
        loc = self.locationStr
        return Pyro4.URI("PYRO:%s@%s" % (objectOrId, loc))


# noinspection PyPep8Naming
class Pyro4ClientFactory(Factory):
    protocol = Pyro4Protocol

    def __init__(self):
        raise NotImplementedError("Client functionality over Pyro4Protocol not yet supported.  " +
                                  "Use twisted-pyro.proxy.PyroDeferredService instead.")

    def register(self, obj, objectId=None):
        """
        Register a Pyro object under the given id. Note that this object is now only
        known inside this daemon, it is not automatically available in a name server.
        This method returns a URI for the registered object.
        """
        if objectId:
            if not isinstance(objectId, basestring):
                raise TypeError("objectId must be a string or None")
        else:
            objectId = "obj_" + uuid.uuid4().hex  # generate a new objectId
        if hasattr(obj, "_pyroId") and obj._pyroId != "":  # check for empty string is needed for Cython
            raise errors.DaemonError("object already has a Pyro id")
        if objectId in self.objectsById:
            raise errors.DaemonError("object already registered with that id")
        # set some pyro attributes
        obj._pyroId = objectId
        obj._pyroDaemon = self
        if Pyro4.config.AUTOPROXY:
            # register a custom serializer for the type to automatically return proxies
            # we need to do this for all known serializers
            for ser in util._serializers.values():
                ser.register_type_replacement(type(obj), pyroObjectToAutoProxy)
        # register the object in the mapping
        self.objectsById[obj._pyroId] = obj
        log.info("Registered object of type %s to id %s" % (type(obj), str(obj._pyroId)))
        log.debug("objectsById is now %s" % pformat(self.objectsById))
        return self.uriFor(objectId)

    def buildProtocol(self, addr):
        log.debug("Building protocol on address %s" % str(addr))
        protocol = self.protocol()
        protocol.factory = self
        protocol.state = "header"
        protocol.required_message_types = [message.MSG_CONNECTOK]
        return protocol


