twisted-pyro
============

Provides a Deferred-based interface for Pyro4's built-in Async calls.

Introduction
============

I wanted to be able to migrate a Pyro4 application to Twisted in pieces.  I was excited to learn that Pyro4 had an async interface until I dug into the details.  The biggest problem was that Pyro's FutureResult (equivalent to Deferred) provided no errback strategy.  Instead, it consumes all errors, dumping the callback chain.  I also had concerns about thread safety since all of FutureResult's .then() calls (equivalent of addCallback) run in a separate thread along with Pyro's async behavior.

This package attempts to address both issues by making "deep" (private and mangled) modifications to Pyro4.Proxy and its associated async classes:
 - Calls using the traditional, "transparent" Pyro interface now return a Deferred.
 - This Deferred starts with a single callback to return the FutureResult's result (or raise the FutureResult's error).
 - To maximize thread safety, the Deferred is called indirectly.  Specifically, Pyro's FutureResult object includes only one callback (running in the Pyro thread) which calls reactor.callLater(0, d.callback, None).
 - I believe that this pushes the Deferred's actual callback execution into the Reactor's thread, providing thread safety.
 - In principle, the Deferred's first callback (to the FutureResult's value) blocks, but the callback should only occur after a value (or an exception) is available locally.
 
A proxy is initiated by initializing PyroDeferredService() with a standard Pyro4.Proxy.

This package also includes PyroPatientProxy, an implementation of Pyro4.Proxy that doesn't raise NamingError.  This means that the proxy will repeatedly search for the nameserver not just the remote daemon.  By default, this occurs no more than once every 2 seconds for 43200 retries (at least 24h).  Since calls to this object can block "indefinitely", it should ONLY be used in conjunction with the PyroDeferredService.  PLEASE NOTE THAT THIS PROXY WILL START WITHOUT A NAMING SERVER SO YOU NEED TO ENSURE THAT THIS SERVER WILL EVENTUALLY GAIN ACCESS TO THE NAMING SERVER.  OTHERWISE, IT WILL SIT IDLE FOR AT LEAST 24H BEFORE FINALLY DYING.
 
This is a preliminary (alpha) effort, but feel free to use or contribute!