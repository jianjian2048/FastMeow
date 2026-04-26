"""Dispatcher: pumps Transport.stream_events() into Router.dispatch().

The dispatcher is the bridge between the wire (gRPC stream of proto
envelopes) and user code (Router with handlers). Its responsibilities:

    1. Maintain a single ``StreamEvents`` subscription on the Transport.
    2. For every event, build a :class:`Ctx` whose ``client`` is bound
       to the event's ``account_key``.
    3. Hand ``(event, ctx)`` to ``Router.dispatch``.
    4. Run handlers concurrently across accounts, but keep per-account
       events strictly ordered (so a slow handler for account A does
       not block account B, while two messages from account A still
       run in arrival order).
    5. Surface handler exceptions to a user-supplied ``on_error`` hook
       (default: log via :mod:`logging`) without killing the stream.

Design notes:
    * Per-account ordering is enforced with one ``asyncio.Task`` per
      account, fed by an ``asyncio.Queue``. The dispatcher never blocks
      on a handler -- it enqueues and moves on.
    * ``stop()`` is cooperative: cancels the stream-reader, drains the
      per-account queues, and waits up to ``drain_timeout`` for
      in-flight handlers to finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .context import AccountClient, Ctx
from .exceptions import BackpressureError
from .router import Router
from .types import Event, SendResult

if False:  # TYPE_CHECKING
    from ._transport import Transport


_log = logging.getLogger("fastmeow.dispatcher")


# ---------------------------------------------------------------------------
# AccountClient implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BoundClient:
    """Concrete :class:`AccountClient` backed by a Transport + account_key.

    A new instance is constructed for every dispatched event because the
    ``jid`` may change (e.g. right after pairing). Keeping it cheap and
    immutable also means handlers can stash it transiently inside their
    own data without worrying about races -- the underlying Transport
    is the one shared resource.
    """

    _transport: Transport
    account_key: str
    jid: str

    async def send_text(
        self,
        to_jid: str,
        body: str,
        *,
        client_msg_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        return await self._transport.send_text(
            account_key=self.account_key,
            to_jid=to_jid,
            body=body,
            client_msg_id=client_msg_id,
            reply_to_message_id=reply_to_message_id,
        )


# Static check: _BoundClient really satisfies AccountClient.
_: AccountClient = _BoundClient(_transport=None, account_key="", jid="")  # type: ignore[arg-type]
del _


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


ErrorHook = Callable[[Event, BaseException], Awaitable[None]]


async def _default_error_hook(event: Event, exc: BaseException) -> None:
    _log.exception(
        "handler error for %s (account=%s seq=%d)",
        type(event).__name__,
        event.account_key,
        event.seq,
        exc_info=exc,
    )


class Dispatcher:
    """Connect a :class:`Transport` stream to a :class:`Router`.

    Lifecycle::

        d = Dispatcher(transport, router)
        await d.start()        # spawns the reader task
        await d.run_forever()  # await until stop() or stream EOF
        await d.stop()         # cooperative shutdown

    A typical app calls ``await d.run_until_stopped()`` to combine the
    last two; see :class:`fastmeow.FastMeow`.
    """

    def __init__(
        self,
        transport: Transport,
        router: Router,
        *,
        on_error: ErrorHook | None = None,
        on_event: Callable[[Event], Awaitable[None]] | None = None,
        per_account_queue_size: int = 1024,
        drain_timeout: float = 10.0,
    ) -> None:
        self._transport = transport
        self._router = router
        self._on_error = on_error or _default_error_hook
        self._on_event = on_event
        self._queue_size = per_account_queue_size
        self._drain_timeout = drain_timeout

        self._reader_task: asyncio.Task[None] | None = None
        self._workers: dict[str, _AccountWorker] = {}
        self._stopped = asyncio.Event()
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._reader_task is not None:
            raise RuntimeError("dispatcher already started")
        self._reader_task = asyncio.create_task(self._read_loop(), name="fastmeow-dispatcher")

    async def run_until_stopped(self) -> None:
        """Block until either the stream ends or :meth:`stop` is called."""
        if self._reader_task is None:
            await self.start()
        assert self._reader_task is not None
        try:
            await self._reader_task
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Cancel the reader, drain per-account queues, await workers.

        Stop is fail-safe: the global stop flag is set first so workers
        exit naturally once their queue is empty, then ``put_nowait``
        wakes idle workers without ever blocking on a full queue. This
        avoids a deadlock where a slow handler keeps the queue full and
        ``stop()`` would otherwise block forever waiting to enqueue the
        sentinel.
        """
        if self._stopped.is_set():
            return
        self._stopped.set()

        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task

        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()

        # Wake up any worker currently waiting on an empty queue.
        # Workers also re-check ``_stopped`` after every event, so a busy
        # worker will exit on its own once its queue drains. We never
        # ``await put`` here -- that would deadlock against a full queue
        # whose consumer is stuck in a long handler.
        for w in workers:
            # Worker will see ``_stopped`` and exit after the next
            # event it processes (or is cancelled below on timeout).
            with contextlib.suppress(asyncio.QueueFull):
                w.queue.put_nowait(None)

        if workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(w.task for w in workers), return_exceptions=True),
                    timeout=self._drain_timeout,
                )
            except TimeoutError:
                for w in workers:
                    if not w.task.done():
                        w.task.cancel()

    # -- internals ---------------------------------------------------------

    async def _read_loop(self) -> None:
        """Single consumer of ``Transport.stream_events``."""
        try:
            async for event in self._transport.stream_events():
                await self._enqueue(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Stream errors (sidecar crash, network) are terminal for
            # the dispatcher in Phase 2. The app layer decides whether
            # to restart everything.
            _log.exception("event stream terminated with error")
            raise

    async def _enqueue(self, event: Event) -> None:
        worker = await self._get_worker(event.account_key)
        try:
            worker.queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            # 0.1.0: every streamed event is critical (message, qr,
            # pair_success, connected, disconnected, logged_out). Silent
            # drop-oldest would desynchronize handler state. Fail fast:
            # log loudly, set the global stop flag so workers exit, and
            # raise so the read loop terminates the dispatcher visibly.
            self._stopped.set()
            _log.error(
                "per-account queue overflow for %s (size=%d); failing fast. "
                "Handler is too slow or has stalled. Increase "
                "per_account_queue_size or speed up the handler.",
                event.account_key,
                self._queue_size,
            )
            raise BackpressureError(
                f"per-account queue overflow for {event.account_key} (size={self._queue_size})"
            ) from exc

    async def _get_worker(self, account_key: str) -> _AccountWorker:
        async with self._lock:
            existing = self._workers.get(account_key)
            if existing is not None:
                return existing
            queue: asyncio.Queue[Event | None] = asyncio.Queue(self._queue_size)
            task = asyncio.create_task(
                self._account_loop(account_key, queue),
                name=f"fastmeow-acct-{account_key}",
            )
            worker = _AccountWorker(queue=queue, task=task)
            self._workers[account_key] = worker
            return worker

    async def _account_loop(
        self,
        account_key: str,
        queue: asyncio.Queue[Event | None],
    ) -> None:
        """Per-account serial executor.

        Exits when:
          * a ``None`` sentinel is dequeued (graceful stop), or
          * ``_stopped`` is set and the queue is empty (drain complete).
        Both conditions are needed because :meth:`stop` may not be able
        to enqueue a sentinel against a full queue, and a busy worker
        could otherwise outlive the dispatcher.
        """
        while True:
            if self._stopped.is_set() and queue.empty():
                return
            event = await queue.get()
            if event is None:
                return
            ctx = Ctx(
                account_key=event.account_key,
                account_jid=event.account_jid,
                event=event,
                client=_BoundClient(
                    _transport=self._transport,
                    account_key=event.account_key,
                    jid=event.account_jid,
                ),
            )
            try:
                if self._on_event is not None:
                    try:
                        await self._on_event(event)
                    except Exception:
                        _log.exception("on_event hook raised; continuing dispatch")
                await self._router.dispatch(event, ctx)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                try:
                    await self._on_error(event, exc)
                except Exception:
                    _log.exception("on_error hook itself raised")


@dataclass
class _AccountWorker:
    queue: asyncio.Queue[Event | None]
    task: asyncio.Task[None]
