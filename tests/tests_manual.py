__author__ = 'Clayton Daley'

import logging
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("twisted-pyro.tests_manual")
log.info("Loading Application")

import pyro.protocol

factory = pyro.protocol.Pyro4ProtocolFactory()


class Foo(object):
    def ping(self):
        log.info("I was pinged!!!")
        return "pong"


obj = Foo()
objId = factory.register(obj)
log.info("")

port = pyro.protocol.reactor.listenTCP(5555, factory)
log.info("PYRO available on port %d" % port.getHost().port)
pyro.protocol.reactor.run()