"""Dispatcher 测试：按账号排序、钩子、drain。

这里不会启动真实的 gRPC 通道。相反，``FakeTransport`` 暴露 dispatcher
使用的同样接口（``stream_events`` 以及 ``_BoundClient`` 转发到的
``send_text`` 方法），并让每个测试把事件推入 asyncio.Queue。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from fastmeow._dispatcher import Dispatcher
from fastmeow.context import Ctx
from fastmeow.exceptions import BackpressureError
from fastmeow.router import Router
from fastmeow.types import (
    ConnectedEvent,
    Event,
    MessageEvent,
    SendResult,
)

# ---------------------------------------------------------------------------
# 伪对象
# ---------------------------------------------------------------------------


class FakeTransport:
    """:class:`fastmeow._transport.Transport` 的最小替身。

    dispatcher 只会碰到：
        * ``stream_events()`` -> 异步迭代器
        * （通过 ``_BoundClient`` 间接调用）``send_text(...)``
    其余部分都不会用到，所以不实现。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self.send_calls: list[dict[str, Any]] = []
        # 在进入 stream_events 生成器时置位，这样测试可以等待
        # dispatcher 真正开始消费。
        self.streaming = asyncio.Event()

    async def push(self, event: Event) -> None:
        await self._queue.put(event)

    async def end_stream(self) -> None:
        await self._queue.put(None)

    async def stream_events(
        self, *, include_soft_events: bool = False
    ) -> AsyncIterator[Event]:
        self.streaming.set()
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def send_text(
        self,
        *,
        account_key: str,
        to_jid: str,
        body: str,
        client_msg_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        self.send_calls.append(
            {
                "account_key": account_key,
                "to_jid": to_jid,
                "body": body,
                "client_msg_id": client_msg_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return SendResult(
            message_id="srv-id",
            server_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            deduped=False,
        )


def make_message(seq: int, account_key: str, text: str = "hi") -> MessageEvent:
    return MessageEvent(
        seq=seq,
        sidecar_id="sc",
        account_key=account_key,
        account_jid=f"{account_key}@s.whatsapp.net",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        message_id=f"M{seq}",
        chat_jid=f"{account_key}-chat@s.whatsapp.net",
        sender_jid=f"{account_key}-peer@s.whatsapp.net",
        from_me=False,
        is_group=False,
        text=text,
    )


def make_connected(seq: int, account_key: str) -> ConnectedEvent:
    return ConnectedEvent(
        seq=seq,
        sidecar_id="sc",
        account_key=account_key,
        account_jid=f"{account_key}@s.whatsapp.net",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_routes_event_to_handler() -> None:
    transport = FakeTransport()
    router = Router()
    seen: list[str] = []

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        seen.append(msg.text)

    d = Dispatcher(transport, router)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    await transport.push(make_message(1, "alice", "ping"))
    # 让按账号 worker 有机会取走它。
    for _ in range(20):
        if seen:
            break
        await asyncio.sleep(0.01)

    await transport.end_stream()
    await d.stop()

    assert seen == ["ping"]


@pytest.mark.asyncio
async def test_per_account_ordering_preserved() -> None:
    transport = FakeTransport()
    router = Router()
    seen: list[tuple[str, int]] = []

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        # 慢处理器不得打乱同一账号的事件顺序。
        await asyncio.sleep(0.005)
        seen.append((msg.account_key, msg.seq))

    d = Dispatcher(transport, router)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    for seq in range(1, 6):
        await transport.push(make_message(seq, "alice"))
    for seq in range(101, 106):
        await transport.push(make_message(seq, "bob"))

    # 排空。
    for _ in range(100):
        if len(seen) == 10:
            break
        await asyncio.sleep(0.01)

    await transport.end_stream()
    await d.stop()

    alice_seqs = [s for k, s in seen if k == "alice"]
    bob_seqs = [s for k, s in seen if k == "bob"]
    assert alice_seqs == [1, 2, 3, 4, 5]
    assert bob_seqs == [101, 102, 103, 104, 105]


@pytest.mark.asyncio
async def test_cross_account_concurrency() -> None:
    """账号 A 上的慢处理器不得阻塞账号 B。"""
    transport = FakeTransport()
    router = Router()
    a_release = asyncio.Event()
    b_done = asyncio.Event()

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        if msg.account_key == "alice":
            await a_release.wait()
        else:
            b_done.set()

    d = Dispatcher(transport, router)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    await transport.push(make_message(1, "alice"))
    await transport.push(make_message(1, "bob"))

    # 即使 alice 的处理器挂起，bob 也必须完成。
    await asyncio.wait_for(b_done.wait(), timeout=1.0)

    a_release.set()
    await transport.end_stream()
    await d.stop()


@pytest.mark.asyncio
async def test_handler_exception_routed_to_on_error() -> None:
    transport = FakeTransport()
    router = Router()

    @router.message()
    async def boom(msg: MessageEvent, ctx: Ctx) -> None:
        if msg.seq == 1:
            raise RuntimeError("kaboom")

    captured: list[tuple[Event, BaseException]] = []

    async def on_error(event: Event, exc: BaseException) -> None:
        captured.append((event, exc))

    d = Dispatcher(transport, router, on_error=on_error)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    await transport.push(make_message(1, "alice"))
    await transport.push(make_message(2, "alice", "still alive"))

    # 等待两个事件都处理完（一个错误 + 一个成功）。
    for _ in range(100):
        if captured and len(transport.send_calls) >= 0:
            await asyncio.sleep(0.02)
            break
        await asyncio.sleep(0.01)
    # 稳定一下。
    await asyncio.sleep(0.05)

    await transport.end_stream()
    await d.stop()

    assert len(captured) == 1
    assert isinstance(captured[0][1], RuntimeError)
    assert captured[0][0].seq == 1


@pytest.mark.asyncio
async def test_on_event_hook_fires_before_dispatch() -> None:
    transport = FakeTransport()
    router = Router()
    order: list[str] = []

    async def on_event(event: Event) -> None:
        order.append(f"hook-{event.seq}")

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        order.append(f"dispatch-{msg.seq}")

    d = Dispatcher(transport, router, on_event=on_event)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    await transport.push(make_message(1, "alice"))

    for _ in range(50):
        if len(order) == 2:
            break
        await asyncio.sleep(0.01)

    await transport.end_stream()
    await d.stop()

    assert order == ["hook-1", "dispatch-1"]


@pytest.mark.asyncio
async def test_on_event_hook_exception_does_not_block_dispatch() -> None:
    transport = FakeTransport()
    router = Router()
    seen: list[int] = []

    async def on_event(event: Event) -> None:
        raise RuntimeError("hook bug")

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        seen.append(msg.seq)

    d = Dispatcher(transport, router, on_event=on_event)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    await transport.push(make_message(1, "alice"))
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)

    await transport.end_stream()
    await d.stop()

    assert seen == [1]


@pytest.mark.asyncio
async def test_ctx_client_send_text_routes_to_transport() -> None:
    """传入 Ctx 的绑定 client 必须命中正确的 account_key。"""
    transport = FakeTransport()
    router = Router()

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        await ctx.client.send_text("peer@s.whatsapp.net", "pong")

    d = Dispatcher(transport, router)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    await transport.push(make_message(1, "alice"))

    for _ in range(50):
        if transport.send_calls:
            break
        await asyncio.sleep(0.01)

    await transport.end_stream()
    await d.stop()

    assert len(transport.send_calls) == 1
    call = transport.send_calls[0]
    assert call["account_key"] == "alice"
    assert call["to_jid"] == "peer@s.whatsapp.net"
    assert call["body"] == "pong"


@pytest.mark.asyncio
async def test_double_start_raises() -> None:
    transport = FakeTransport()
    router = Router()
    d = Dispatcher(transport, router)  # type: ignore[arg-type]
    await d.start()
    with pytest.raises(RuntimeError, match="already started"):
        await d.start()
    await transport.end_stream()
    await d.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    transport = FakeTransport()
    router = Router()
    d = Dispatcher(transport, router)  # type: ignore[arg-type]
    await d.start()
    await transport.end_stream()
    await d.stop()
    # 第二次 stop 也不得抛出异常。
    await d.stop()


@pytest.mark.asyncio
async def test_stream_eof_completes_run_until_stopped() -> None:
    transport = FakeTransport()
    router = Router()
    d = Dispatcher(transport, router)  # type: ignore[arg-type]

    async def driver() -> None:
        await transport.streaming.wait()
        await transport.push(make_connected(1, "alice"))
        await transport.end_stream()

    drive = asyncio.create_task(driver())
    await asyncio.wait_for(d.run_until_stopped(), timeout=1.0)
    await drive


@pytest.mark.asyncio
async def test_queue_overflow_fails_fast() -> None:
    """当按账号队列已满时，dispatcher 必须快速失败。

    0.1.0 语义：每个流入的事件都很关键，因此静默的
    drop-oldest 会让处理器状态失步。读取循环必须抛出
    :class:`BackpressureError`，并且 dispatcher 必须停止。
    """
    transport = FakeTransport()
    router = Router()
    gate = asyncio.Event()

    @router.message()
    async def handler(msg: MessageEvent, ctx: Ctx) -> None:
        # 无限挂起，这样队列就能被塞满。
        await gate.wait()

    d = Dispatcher(transport, router, per_account_queue_size=2)  # type: ignore[arg-type]
    await d.start()
    await transport.streaming.wait()

    # 1 -> handler（挂起）。2、3 -> 队列（满）。4 -> 溢出。
    await transport.push(make_message(1, "alice"))
    await asyncio.sleep(0.02)  # 让 handler 取走事件 1
    await transport.push(make_message(2, "alice"))
    await transport.push(make_message(3, "alice"))
    await transport.push(make_message(4, "alice"))

    # 读取循环应以 BackpressureError 终止。
    with pytest.raises(BackpressureError):
        await asyncio.wait_for(d.run_until_stopped(), timeout=1.0)

    # 释放挂起的 handler，以便 stop() 可以 drain。
    gate.set()
    await d.stop()
