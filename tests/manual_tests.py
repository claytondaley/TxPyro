__author__ = 'Clayton Daley'

import logging
logging.basicConfig(level=logging.INFO)

log = logging.getLogger("test_pyro")
log.info("Logger started")

import pyro.proxy
import Pyro4
import Pyro4.futures
from pprint import pprint, pformat
from twisted.internet import reactor
import twisted.internet.defer

uri = "PYRONAME:mod_52b107eb0947a7fc310000c6"
envelopeTo = "recipient@mail.com"
envelopeFrom = "sender@mail.com"
message = """MIME-Version: 1.0\r\nFrom: sender@mail.com\r\nSubject: Test Mail using Twisted\r\nTo: recipient@mail.com\r\nContent-Type: text/plain; charset="us-ascii"\r\nContent-Transfer-Encoding: 7bit\r\n\r\nContent of plaintext mail to be sent through Twisted. Thanks, Clayton\r\n.\r\n"""


def stop_reactor(*args):
    reactor.stop()

def log_and_relay(response):
    log.info("Callback responded %s" % str(response))
    return response


def run_native():
    moda_twist = Pyro4.async(Pyro4.Proxy(uri))
    log.info("Running test in Native")
    f = moda_twist.email_process_message(message, envelopeTo, envelopeFrom)
    log.info("Proxy returned type %s" % str(f.__class__))
    # NOTE that native code will not execute callbacks on error
    f.then(log_and_relay)
    return f


def run_deferred():
    mod_twist = pyro.proxy.PyroPatientProxy(uri)
    modd_twist = pyro.proxy.PyroDeferredService(mod_twist)
    log.info("Running test in Deferred")
    d = modd_twist.email_process_message(message, envelopeTo, envelopeFrom)
    log.info("Proxy returned type %s" % str(d.__class__))
    d.addBoth(log_and_relay)
    d.addBoth(stop_reactor)
    return d

# This file is currently configured to be put in the parent directory of pyro.proxy
# For deferred, user must manually reactor.run() after d = run_deferred()
# Native does not require the reactor
# Native should propagate NamingError errors up through 'f' if the nameserver is unavailable
# Deferred should wait indefinitely for a nameserver