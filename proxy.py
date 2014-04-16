import functools
import time
import logging

from Pyro4 import futures, errors, threadutil, URI, Proxy
from Pyro4.core import _AsyncProxyAdapter, _AsyncRemoteMethod
from twisted.internet.defer import Deferred
from twisted.internet import reactor


__author__ = 'Clayton Daley'

log = logging.getLogger("pyro.proxy")
log.debug("Loading Pyro Proxy Module for Twisted")


class PyroDeferredService(_AsyncProxyAdapter):
    """
    Pyro already offers async calls, but these calls (and all callbacks submitted to the Pyro object) run in a separate
    thread.  To minimize concurrency issues, PyroDeferredService relays this response through a Deferred (returned by
    each __getattr__ call).
    """
    def __init__(self, proxy):
        super(PyroDeferredService, self).__init__(proxy)

    def __getattr__(self, name):
        return _PyroDeferredMethod(self._AsyncProxyAdapter__proxy, name)


# noinspection PyProtectedMember
class _PyroDeferredMethod(_AsyncRemoteMethod):
    """
    Re-implements _AsyncRemoteMethod to return Deferred instead of Pyro4.futures.FutureResult.  As this is a minimal
    patch, see Pyro4.futures.FutureResult for the bulk of the functionality.
    """
    def __call__(self, *args, **kwargs):

        d = Deferred()

        def trigger_deferred(response):
            # If we call the callback directly, it ends up in this thread, may not be thread-safe and definitely does
            # not handle errors correctly.  Instead, leave it to reactor to initiate the process when it's ready (and
            # safe).
            reactor.callLater(0, d.callback, None)
            return response

        def get_future_value(_):
            return result.value

        d.addCallback(get_future_value)

        result = _FutureResultErrCallback()
        result.then(trigger_deferred)
        thread = threadutil.Thread(target=self.__asynccall, args=(result, args, kwargs))
        thread.setDaemon(True)
        thread.start()

        return d

    def __asynccall(self, asyncresult, args, kwargs):
        super(_PyroDeferredMethod, self)._AsyncRemoteMethod__asynccall(asyncresult, args, kwargs)


class _FutureResultErrCallback(futures.FutureResult):
    """
    The core FutureResult implementation bypasses all callbacks if an exception occurs (consuming the Exception),
    making it extremely difficult to integrate with Twisted since we don't want to be polling objects for error states.
    This implementation modifies the default behavior to make it (more) compatible with Twisted.

    NOTE: This class alters the behavior of callbacks.  See _FutureResultErrCallback.then() and
     _FutureResultErrCallback.set_value() for implementation details.
    """
    def set_value(self, value):
        """
        The core FutureResult implementation skipped the callchain if the value was an instance of  _ExceptionWrapper.
        To ensure that errors are handed off to callbacks, this function had to be modified.

        NOTE:  This function also alters the behavior of then() callbacks.  See _FutureResultErrCallback.then()
        for implementation details.
        """
        with self.valueLock:
            # noinspection PyAttributeOutsideInit
            self._FutureResult__value = value
            for call, args, kwargs in self.callchain:
                call = functools.partial(call, self._FutureResult__value)
                call(*args, **kwargs)
            self.callchain = []
            self._FutureResult__ready.set()

    def then(self, call, *args, **kwargs):
        """
        Unlike the core implementation, _FutureResultErrCallback no longer chains callbacks together.  Instead, any
        callback registered with _FutureResultErrCallback.then() is sent the final value for the FutureResult.  In the
        event of an Exception, the result will have type Pyro4.futures._ExceptionWrapper.  Callbacks are responsible
        for handling this condition appropriately and may re-raising the exception using the .raiseIt() function.
        """
        if self._FutureResult__ready.isSet():
            # value is already known, we need to process it immediately (can't use the callchain anymore)
            call = functools.partial(call, self._FutureResult__value)
            call(*args, **kwargs)
        else:
            # add the call to the callchain, it will be processed later when the result arrives
            with self.valueLock:
                self.callchain.append((call, args, kwargs))
        return self

    def get_value(self):
        return super(_FutureResultErrCallback, self).get_value()

    value = property(get_value, set_value, None, "The result value of the call. Reading it will block if not available yet.")


# noinspection PyPep8Naming,PyProtectedMember
class PyroPatientProxy(Proxy):
    """
    PyroPatientProxy exists to prevent NamingErrors (the lack of a namingserver) from raising an exception. Instead,
    the class will retry "indefinitely".  This should ONLY be used in combination with asynchronous calls to avoid
    indefinite blocking of the thread.

    To minimize the amount of recoding necessary, we hijack all calls to __pyroCreateConnection, reroute them to
    _pyroReconnect and use the original __pyroCreateConnection by reassigning it to _PyroConnect.
    """
    def __init__(self, uri, retries=43200):
        self.ns_found = False
        self.retries = retries
        super(PyroPatientProxy, self).__init__(uri)

    _PyroConnect = Proxy._Proxy__pyroCreateConnection

    def _Proxy__pyroCreateConnection(self):
        self._pyroReconnect()

    def __copy__(self):
        uriCopy = URI(self._pyroUri)
        return PyroPatientProxy(uriCopy)

    def _pyroReconnect(self):
        """(re)connect the proxy to the daemon containing the pyro object which the proxy is for"""
        log.info("(Re)connecting proxy to the daemon for %s" % self._pyroUri)
        self._pyroRelease()
        tries = self.retries
        while tries:
            try:
                self._PyroConnect()
                return
            except (errors.CommunicationError, errors.NamingError) as e:
                if isinstance(e, errors.CommunicationError) and self.ns_found is False:
                    self.ns_found = True
                    log.info("Naming Server found by %s" % self._pyroUri)
                elif isinstance(e, errors.NamingError) and self.ns_found is True:
                    self.ns_found = False
                    log.info("Naming Server lost by %s" % self._pyroUri)
                tries -= 1
                if tries:
                    time.sleep(2)
        if self.ns_found:
            msg = "Failed to reconnect to daemon for %s" % self._pyroUri
        else:
            msg = "Failed to reconnect to namingserver for %s" % self._pyroUri
        log.error(msg)
        raise errors.ConnectionClosedError(msg)