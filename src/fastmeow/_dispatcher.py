"""派发器：将 Transport.stream_events() 泵入 Router.dispatch()。

派发器是线路层（proto 信封的 gRPC 流）与用户代码（带有处理器的 Router）之间的桥梁。
其职责包括：

    1. 在传输层上维持一个单一的 ``StreamEvents`` 订阅。
    2. 为每个事件构建一个 :class:`Ctx`，其 ``client`` 绑定到事件的 ``account_key``。
    3. 将 ``(event, ctx)`` 传递给 ``Router.dispatch``。
    4. 跨账号并发运行处理器，但在账号内部保持事件严格有序（这样账号 A 的慢速处理器
       不会阻塞账号 B，同时来自账号 A 的两条消息仍按到达顺序执行）。
    5. 将处理器异常暴露给用户提供的 ``on_error`` 钩子（默认：通过 :mod:`logging` 记录日志），
       而不中断流。

设计说明：
    * 账号内部的顺序性通过每个账号对应一个 ``asyncio.Task`` 并由 ``asyncio.Queue`` 驱动来保证。
      派发器绝不会在处理器上阻塞 —— 它入队后即继续处理下一个。
    * ``stop()`` 是协作式的：取消流读取器，排空每个账号的队列，并等待最多 ``drain_timeout`` 秒
       以完成正在运行的处理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .context import AccountClient, Ctx
from .exceptions import BackpressureError
from .router import Router
from .types import (
    Event,
    GroupInfo,
    GroupParticipantAction,
    GroupParticipantUpdateResult,
    SendResult,
)

if False:  # TYPE_CHECKING
    from ._transport import Transport


_log = logging.getLogger("fastmeow.dispatcher")


# ---------------------------------------------------------------------------
# AccountClient implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BoundClient:
    """由传输层 + account_key 支持的具体 :class:`AccountClient` 实现。

    每个派发的事件都会构建一个新实例，因为 ``jid`` 可能会发生变化（例如在配对之后）。
    保持其实例创建开销低且不可变，意味着处理器可以将其暂时存储在自己的数据中，
    而无需担心竞态条件 —— 底层的传输层才是唯一的共享资源。
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

    # -- 群组（Phase 4.1）-----------------------------------------------

    async def list_groups(self) -> tuple[GroupInfo, ...]:
        return await self._transport.list_joined_groups(self.account_key)

    async def get_group_info(self, group_jid: str) -> GroupInfo:
        return await self._transport.get_group_info(
            account_key=self.account_key,
            group_jid=group_jid,
        )

    async def preview_group_invite(self, invite_link_or_code: str) -> GroupInfo:
        return await self._transport.preview_group_invite(
            account_key=self.account_key,
            invite_link=invite_link_or_code,
        )

    async def join_group(self, invite_link_or_code: str) -> str:
        return await self._transport.join_group_via_invite(
            account_key=self.account_key,
            invite_link=invite_link_or_code,
        )

    async def leave_group(self, group_jid: str) -> None:
        await self._transport.leave_group(
            account_key=self.account_key,
            group_jid=group_jid,
        )

    async def create_group(
        self, name: str, participants: Sequence[str] = ()
    ) -> GroupInfo:
        return await self._transport.create_group(
            account_key=self.account_key,
            name=name,
            participant_jids=tuple(participants),
        )

    async def set_group_name(self, group_jid: str, name: str) -> GroupInfo:
        return await self._transport.update_group_settings(
            account_key=self.account_key,
            group_jid=group_jid,
            name=name,
        )

    async def set_group_topic(self, group_jid: str, topic: str) -> GroupInfo:
        return await self._transport.update_group_settings(
            account_key=self.account_key,
            group_jid=group_jid,
            topic=topic,
        )

    async def set_group_announce(
        self, group_jid: str, announce: bool
    ) -> GroupInfo:
        return await self._transport.update_group_settings(
            account_key=self.account_key,
            group_jid=group_jid,
            is_announce=announce,
        )

    async def set_group_locked(self, group_jid: str, locked: bool) -> GroupInfo:
        return await self._transport.update_group_settings(
            account_key=self.account_key,
            group_jid=group_jid,
            is_locked=locked,
        )

    async def add_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]:
        return await self._transport.update_group_participants(
            account_key=self.account_key,
            group_jid=group_jid,
            action=GroupParticipantAction.ADD,
            participant_jids=tuple(jids),
        )

    async def remove_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]:
        return await self._transport.update_group_participants(
            account_key=self.account_key,
            group_jid=group_jid,
            action=GroupParticipantAction.REMOVE,
            participant_jids=tuple(jids),
        )

    async def promote_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]:
        return await self._transport.update_group_participants(
            account_key=self.account_key,
            group_jid=group_jid,
            action=GroupParticipantAction.PROMOTE,
            participant_jids=tuple(jids),
        )

    async def demote_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]:
        return await self._transport.update_group_participants(
            account_key=self.account_key,
            group_jid=group_jid,
            action=GroupParticipantAction.DEMOTE,
            participant_jids=tuple(jids),
        )

    async def get_group_invite_link(
        self, group_jid: str, *, revoke: bool = False
    ) -> str:
        return await self._transport.get_group_invite_link(
            account_key=self.account_key,
            group_jid=group_jid,
            reset=revoke,
        )


# 静态检查：_BoundClient 确实满足 AccountClient 接口要求。
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
    """将 :class:`Transport` 流连接到 :class:`Router`。

    生命周期::

        d = Dispatcher(transport, router)
        await d.start()        # 启动读取器任务
        await d.run_forever()  # 等待直到 stop() 或流 EOF
        await d.stop()         # 协作式关闭

    典型的应用会调用 ``await d.run_until_stopped()`` 以结合最后两步；
    参见 :class:`fastmeow.FastMeow`。
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
        """阻塞直到流结束或调用 :meth:`stop`。"""
        if self._reader_task is None:
            await self.start()
        assert self._reader_task is not None
        try:
            await self._reader_task
        finally:
            await self.stop()

    async def stop(self) -> None:
        """取消读取器，排空每个账号的队列，并等待工作任务结束。

        停止操作是故障安全的：首先设置全局停止标志，使工作任务在队列为空时自然退出，
        然后通过 ``put_nowait`` 唤醒空闲的工作任务，且绝不会阻塞在已满的队列上。
        这避免了由于慢速处理器导致队列占满，进而使 ``stop()`` 在等待入队哨兵值时永久阻塞
        的死锁情况。
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

        # 唤醒当前正在等待空队列的所有工作任务。
        # 工作任务在处理完每个事件后也会重新检查 ``_stopped``，因此忙碌的工作任务
        # 会在队列排空后自行退出。我们在此处从不使用 ``await put`` —— 
        # 否则如果消费者卡在长耗时的处理器中且队列已满，将会导致死锁。
        for w in workers:
            # 工作任务将看到 ``_stopped`` 并在处理完下一个
            # 事件（或在超时后被下方取消）后退出。
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

    # -- 内部方法 ---------------------------------------------------------

    async def _read_loop(self) -> None:
        """``Transport.stream_events`` 的单一消费者。"""
        try:
            async for event in self._transport.stream_events():
                await self._enqueue(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 流错误（sidecar 崩溃、网络故障）在第二阶段对派发器而言是致命的。
            # app 层将决定是否重启所有组件。
            _log.exception("event stream terminated with error")
            raise

    async def _enqueue(self, event: Event) -> None:
        worker = await self._get_worker(event.account_key)
        try:
            worker.queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            # 0.1.0: 流式传输的每个事件都是关键的（消息、qr、配对成功、已连接、
            # 已断开连接、已注销）。静默丢弃旧事件会导致处理器状态失同步。
            # 快速失败：记录严重错误日志，设置全局停止标志以让工作任务退出，
            # 并抛出异常以使读取循环显式地终止派发器。
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
        """单个账号的串行执行器。

        退出条件：
          * 移出队列的值为 ``None`` 哨兵（优雅停止），或者
          * ``_stopped`` 已设置且队列为空（排空完成）。
        两个条件都是必要的，因为当队列满时 :meth:`stop` 可能无法入队哨兵值，
        而忙碌的工作任务可能比派发器存活更久。
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
