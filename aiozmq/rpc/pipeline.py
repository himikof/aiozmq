import asyncio
import zmq

from functools import partial

from .log import logger

from .base import (
    NotFoundError,
    ParametersError,
    Service,
    ServiceClosedError,
    _BaseProtocol,
    _BaseServerProtocol,
    )
from .util import (
    _MethodCall,
    )


@asyncio.coroutine
def connect_pipeline(*, connect=None, bind=None, loop=None,
                     translation_table=None):
    """A coroutine that creates and connects/binds Pipeline client instance.

    Usually for this function you need to use *connect* parameter, but
    ZeroMQ does not forbid to use *bind*.

    translation_table -- an optional table for custom value translators.

    loop --  an optional parameter to point
    ZmqEventLoop instance.  If loop is None then default
    event loop will be given by asyncio.get_event_loop() call.

    Returns PipelineClient instance.
    """
    if loop is None:
        loop = asyncio.get_event_loop()

    transp, proto = yield from loop.create_zmq_connection(
        lambda: _ClientProtocol(loop, translation_table=translation_table),
        zmq.PUSH, connect=connect, bind=bind)
    return PipelineClient(loop, proto)


@asyncio.coroutine
def serve_pipeline(handler, *, connect=None, bind=None, loop=None,
                   translation_table=None, log_exceptions=False):
    """A coroutine that creates and connects/binds Pipeline server instance.

    Usually for this function you need to use *bind* parameter, but
    ZeroMQ does not forbid to use *connect*.

    handler -- an object which processes incoming pipeline calls.
    Usually you like to pass AttrHandler instance.

    log_exceptions -- log exceptions from remote calls if True.

    translation_table -- an optional table for custom value translators.

    loop -- an optional parameter to point
       ZmqEventLoop instance.  If loop is None then default
       event loop will be given by asyncio.get_event_loop() call.

    Returns Service instance.
    """
    if loop is None:
        loop = asyncio.get_event_loop()

    trans, proto = yield from loop.create_zmq_connection(
        lambda: _ServerProtocol(loop, handler,
                                translation_table=translation_table,
                                log_exceptions=log_exceptions),
        zmq.PULL, connect=connect, bind=bind)
    return Service(loop, proto)


class _ClientProtocol(_BaseProtocol):

    def call(self, name, args, kwargs):
        if self.transport is None:
            raise ServiceClosedError()
        bname = name.encode('utf-8')
        bargs = self.packer.packb(args)
        bkwargs = self.packer.packb(kwargs)
        self.transport.write([bname, bargs, bkwargs])
        fut = asyncio.Future(loop=self.loop)
        fut.set_result(None)
        return fut


class PipelineClient(Service):

    def __init__(self, loop, proto):
        super().__init__(loop, proto)

    @property
    def notify(self):
        """Return object for dynamic Pipeline calls.

        The usage is:
        yield from client.pipeline.ns.func(1, 2)
        """
        return _MethodCall(self._proto)


class _ServerProtocol(_BaseServerProtocol):

    def msg_received(self, data):
        bname, bargs, bkwargs = data

        args = self.packer.unpackb(bargs)
        kwargs = self.packer.unpackb(bkwargs)
        try:
            name = bname.decode('utf-8')
            func = self.dispatch(name)
            args, kwargs, ret_ann = self.check_args(func, args, kwargs)
        except (NotFoundError, ParametersError) as exc:
            fut = asyncio.Future(loop=self.loop)
            fut.set_exception(exc)
        else:
            if asyncio.iscoroutinefunction(func):
                fut = asyncio.async(func(*args, **kwargs), loop=self.loop)
                self.pending_waiters.add(fut)
            else:
                fut = asyncio.Future(loop=self.loop)
                try:
                    fut.set_result(func(*args, **kwargs))
                except Exception as exc:
                    fut.set_exception(exc)
        fut.add_done_callback(partial(self.process_call_result,
                                      name=name, args=args, kwargs=kwargs))

    def process_call_result(self, fut, *, name, args, kwargs):
        self.pending_waiters.discard(fut)
        try:
            if fut.result() is not None:
                logger.warning("Pipeline handler %r returned not None", name)
        except (NotFoundError, ParametersError) as exc:
            logger.exception("Call to %r caused error: %r", name, exc)
        except asyncio.CancelledError:
            return
        except Exception:
            self.try_log(fut, name, args, kwargs)
