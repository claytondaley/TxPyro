twisted-pyro
============

Provides a Deferred-based interface for Pyro4's client calls and a Factory/Protocol that provides server-side Pyro4 support.

This is a preliminary (alpha) effort, but feel free to use or contribute!

Introduction
============

I needed to migrate a [Pyro4](http://pythonhosted.org/Pyro4/) application to [Twisted](twistedmatrix.com).  To simplify testing, I wanted to move it in pieces.  Unfortunately, Pyro4 is a synchronous application and Twisted is an asynchronous application so they didn't play well together.  This package includes a variety of client- and server-side classes to enable the two to coexist.

Client: PyroDeferredService
===========================

I was pleased to learn that Pyro4 had an async interface until I dug into the details.  The biggest problem was that Pyro's `FutureResult` (equivalent to `Deferred`) provided no [errback](http://twistedmatrix.com/documents/13.0.0/core/howto/defer.html#auto4) strategy.  Instead, it consumes all errors, trashing the callback chain.  I also had concerns about thread safety since all of FutureResult's `.then()` calls (equivalent to `addCallback()`) run in a separate thread along with the original async call (see the second sentence of the [Note](https://pythonhosted.org/Pyro4/clientcode.html#asynchronous-future-remote-calls-call-chains)).

This package attempts to address both issues by making deep modifications (private and mangled) to Pyro4.Proxy and its associated async classes:
 - Calls still use the standard (to Pyro4) `proxy.method()` interface but now return a `Deferred`.
 - This `Deferred` starts with a single callback (with one ignored parameter) that gets and returns the FutureResult's result (or raises the FutureResult's error).
 - To maximize thread safety, the Deferred's callback is triggered indirectly.  Specifically, Pyro's `FutureResult` object includes only one callback (run in Pyro's async thread) which calls `reactor.callFromThread(reactor.callLater, 0, d.callback, None)`.
 - I'm reasonably confident that this will always push the Deferred's actual callback execution into the Reactor's thread, providing thread safety.
 - In principle, the Deferred's first callback (to get the FutureResult's value) blocks, but the callback should only occur after a value (or an exception) is available locally.
 
To set up a connection to base, simply run:

    import Pyro4
    import pyro.proxy
    
    # The Pyro4.Proxy is "out of the box" so you can use any standard URI
    uri = "my_pyro_uri"
    proxy = Pyro4.Proxy(uri)
    deferred_generating_proxy = pyro.proxy.PyroDeferredService(proxy)
    
    # As mentioned above, calls use the "transparent" proxy.method() syntax:
    deferred = deferred_generating_proxy.remote_method()

Server: Pyro4Protocol & Pyro4ServerFactory
============================================

The first migrated component only needed to use Pyro4 as a client.  To migrate additional components, I needed an implementation of Pyro4's server-side logic inside Twisted.  As is true of most synchronous code, minor modifications were not going to do the trick.  Instead, I rearranged existing Pyro4 logic (found mostly in `Daemon.handleRequest` and `Message.recv`) into Twisted's Protocol and Factory framework.  Since this code continues to use significant portions of the Pyro4 core, it's possible (but by no means certain) that it will support past and future Pyro4 versions.

The following is an example of making two objects (foo and bar) available as Pyro4 remote objects:

	PORT = 5557
	
	pyro_factory = Pyro4ServerFactory()
	pyro_service = internet.TCPServer(PORT, pyro_factory)
	
	# In Pyro4, a URI is used by clients to find server objects and
	# twisted-pyro provides a 'register()' method on the factory to
	# generate the correct URI. Due to Twisted's architecture, there
	# is no straightforward way for a factory to interrogate it's
	# Transports for host/port information. Specifically, Twisted 
	# allows ProtocolFactory to be attached to multiple Transports 
	# (e.g. ports attached to interfaces) and to bind to all 
	# interfaces (host: 0.0.0.0).
	
	# This example uses a Pyro4 method and an internet address to
	# find an internet-facing adapter.
	HOST = Pyro4.socketutil.getInterfaceAddress("8.8.8.8")
	
	# Finally, twisted-pyro provides a 'setAddress()' method as the 
	# preferred means of providing externally-accessible host/port 
	# information to the factory.
	pyro_factory.setAddress(HOST, PORT)
	
	# Once this information has been provided to the factory, it can
	# generate URIs for local objects.
	foo = Foo()
	bar = Bar()
	fooUri = pyro_factory.register(foo)
	barUri = pyro_factory.register(bar)
	
	# Like Pyro4, the URIs need to be registered with a nameserver or 
	# otherwise transferred to clients.
	
	# This method still uses the core library's blocking call to find 
	# the nameserver. Generally, this is not a problem since it can 
	# be run once and cached locally
    nameserver = locateNS()
	
	# Finally, the objects are registered with the nameserver (Pyro's
	# DNS). Like a DNS, Pyro offers a <friendly name> (i.e. 'path') 
	# that can be "hard-coded" into the client and server. As long as
	# the two resources are attached to the same nameserver, Pyro4
	# will automatically resolve the static name into a dynamic URI.
	nameserver.register(<friendly name>, fooUri)
	nameserver.register(<friendly name>, barUri)

Other:  PyroPatientProxy
========================

**IN GENERL, THIS CLASS SHOULD NOT BE USED**

The proxy file also includes `PyroPatientProxy`, an implementation of `Pyro4.Proxy` that doesn't raise NamingError.  This means that the proxy will retry its search for the nameserver not just the remote daemon.  By default, this occurs no more than once every 2 seconds for 43200 retries (24h plus any delay in the retry schedule).  Since calls to this object can block "indefinitely", it should ONLY be used in conjunction with the `PyroDeferredService` or Pyro4's `_AsyncProxyAdapter` accessed by calling `Pyro4.async(proxy)`.  **PLEASE NOTE THAT THIS PROXY WILL START WITHOUT A NAMING SERVER.  IF YOU USE THE CLASS, YOU ARE RESPONSIBLE FOR ENSURING EVENTUAL ACCESS TO A NAMING SERVER.  OTHERWISE, IT WILL SIT IDLE FOR AT LEAST 24H BEFORE RASING AN EXCEPTION.**

