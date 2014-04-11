twisted-pyro
============

Provides a Deferred-based interface for Pyro4's built-in Async calls.

This is a preliminary (alpha) effort, but feel free to use or contribute!

PyroDeferredService
===================

I wanted to be able to migrate a [Pyro4](http://pythonhosted.org/Pyro4/) application to [Twisted](twistedmatrix.com) in pieces.  I was pleased to learn that Pyro4 had an async interface until I dug into the details.  The biggest problem was that Pyro's `FutureResult` (equivalent to `Deferred`) provided no [errback](http://twistedmatrix.com/documents/13.0.0/core/howto/defer.html#auto4) strategy.  Instead, it consumes all errors, trashing the callback chain.  I also had concerns about thread safety since all of FutureResult's `.then()` calls (equivalent to `addCallback()`) run in a separate thread along with the original async call (see the second sentence of the [Note](https://pythonhosted.org/Pyro4/clientcode.html#asynchronous-future-remote-calls-call-chains)).

This package attempts to address both issues by making deep modifications (private and mangled) to Pyro4.Proxy and its associated async classes:
 - Calls still use the standard (to Pyro4) `proxy.method()` interface but now return a `Deferred`.
 - This `Deferred` starts with a single callback (with one ignored parameter) that gets and returns the FutureResult's result (or raises the FutureResult's error).
 - To maximize thread safety, the Deferred's callback is triggered indirectly.  Specifically, Pyro's `FutureResult` object includes only one callback (run in Pyro's async thread) which calls `reactor.callLater(0, d.callback, None)`.
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
	
PyroPatientProxy
================

This package also includes `PyroPatientProxy`, an implementation of `Pyro4.Proxy` that doesn't raise NamingError.  This means that the proxy will retry its search for the nameserver not just the remote daemon.  By default, this occurs no more than once every 2 seconds for 43200 retries (24h plus any delay in the retry schedule).  Since calls to this object can block "indefinitely", it should ONLY be used in conjunction with the `PyroDeferredService` or Pyro4's `_AsyncProxyAdapter` accessed by calling `Pyro4.async(proxy)`.  **PLEASE NOTE THAT THIS PROXY WILL START WITHOUT A NAMING SERVER.  IF YOU USE THE CLASS, YOU ARE RESPONSIBLE FOR ENSURING EVENTUAL ACCESS TO A NAMING SERVER.  OTHERWISE, IT WILL SIT IDLE FOR AT LEAST 24H BEFORE RASING AN EXCEPTION.**

