"""Phase 4.2 回执 / 在线状态 单测。

覆盖四层：

1. ``types`` —— 4 个 enum 的 ``from_proto`` / ``to_proto`` 往返；
   3 个新事件 dataclass 的 ``event_from_proto`` 分支。
2. ``router`` —— 3 个新装饰器 (``on_receipt`` / ``on_presence`` /
   ``on_chat_presence``) 派发到正确事件类型；以及
   ``has_soft_event_handlers()`` 自省（含子路由器递归）。
3. ``_BoundClient`` —— 4 个 RPC 方法 (``mark_read`` / ``send_presence`` /
   ``send_chat_presence`` / ``subscribe_presence``) 把参数正确转发到 transport，
   并用 enum (而非字符串) 传递。
4. ``_translate`` —— 回执 / 在线状态 RPC 的 INVALID_ARGUMENT 错误映射
   到 InvalidJIDError / ConfigurationError。

测试不接触网络：FakeRpcError 在内存中模拟 grpc 错误。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import grpc
import pytest

from fastmeow._dispatcher import _BoundClient
from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb
from fastmeow._transport import _translate
from fastmeow.exceptions import (
    ConfigurationError,
    InvalidJIDError,
)
from fastmeow.router import Router
from fastmeow.types import (
    ChatPresenceEvent,
    ChatPresenceMedia,
    ChatPresenceState,
    PresenceEvent,
    PresenceType,
    ReceiptEvent,
    ReceiptType,
    event_from_proto,
)

# ---------------------------------------------------------------------------
# Helpers: proto envelopes
# ---------------------------------------------------------------------------


def _envelope(seq: int = 1) -> pb.StreamEventsResponse:
    env = pb.StreamEventsResponse(
        seq=seq,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
    )
    env.observed_at.seconds = 1_700_000_000
    return env


# ---------------------------------------------------------------------------
# Types — enum round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum_cls", "prefix", "pb_enum"),
    [
        (ReceiptType, "RECEIPT_TYPE_", pb.ReceiptType),
        (PresenceType, "PRESENCE_TYPE_", pb.PresenceType),
        (ChatPresenceState, "CHAT_PRESENCE_STATE_", pb.ChatPresenceState),
        (ChatPresenceMedia, "CHAT_PRESENCE_MEDIA_", pb.ChatPresenceMedia),
    ],
)
def test_enum_round_trip(enum_cls: Any, prefix: str, pb_enum: Any) -> None:
    for member in enum_cls:
        proto_val = pb_enum.Value(f"{prefix}{member.value}")
        assert enum_cls.from_proto(proto_val) is member
        assert member.to_proto() == proto_val


@pytest.mark.parametrize(
    "enum_cls",
    [ReceiptType, PresenceType, ChatPresenceState, ChatPresenceMedia],
)
def test_enum_unknown_falls_back_to_unspecified(enum_cls: Any) -> None:
    # 任何未来的 proto 新增值都不应让 SDK 崩溃。
    assert enum_cls.from_proto(9999) is enum_cls.UNSPECIFIED


# ---------------------------------------------------------------------------
# event_from_proto — three new oneof branches
# ---------------------------------------------------------------------------


def test_event_from_proto_receipt() -> None:
    env = _envelope()
    env.receipt.chat_jid = "g@g.us"
    env.receipt.sender_jid = "peer@s.whatsapp.net"
    env.receipt.message_ids.extend(["M1", "M2"])
    env.receipt.timestamp.seconds = 1_700_000_000
    env.receipt.receipt_type = pb.RECEIPT_TYPE_READ
    out = event_from_proto(env)
    assert isinstance(out, ReceiptEvent)
    assert out.chat_jid == "g@g.us"
    assert out.sender_jid == "peer@s.whatsapp.net"
    assert out.message_ids == ("M1", "M2")
    assert out.receipt_type is ReceiptType.READ
    assert out.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_event_from_proto_presence_offline_with_last_seen() -> None:
    env = _envelope()
    env.presence.from_jid = "peer@s.whatsapp.net"
    env.presence.unavailable = True
    env.presence.last_seen.seconds = 1_700_000_000
    out = event_from_proto(env)
    assert isinstance(out, PresenceEvent)
    assert out.from_jid == "peer@s.whatsapp.net"
    assert out.unavailable is True
    assert out.last_seen == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_event_from_proto_presence_online_no_last_seen() -> None:
    env = _envelope()
    env.presence.from_jid = "peer@s.whatsapp.net"
    env.presence.unavailable = False
    out = event_from_proto(env)
    assert isinstance(out, PresenceEvent)
    assert out.unavailable is False
    assert out.last_seen is None


def test_event_from_proto_chat_presence() -> None:
    env = _envelope()
    env.chat_presence.chat_jid = "peer@s.whatsapp.net"
    env.chat_presence.sender_jid = "peer@s.whatsapp.net"
    env.chat_presence.state = pb.CHAT_PRESENCE_STATE_COMPOSING
    env.chat_presence.media = pb.CHAT_PRESENCE_MEDIA_AUDIO
    out = event_from_proto(env)
    assert isinstance(out, ChatPresenceEvent)
    assert out.state is ChatPresenceState.COMPOSING
    assert out.media is ChatPresenceMedia.AUDIO


# ---------------------------------------------------------------------------
# Router — three new decorators
# ---------------------------------------------------------------------------


async def test_router_on_receipt_decorator() -> None:
    router = Router()
    seen: list[ReceiptEvent] = []

    @router.on_receipt()
    async def on_r(event: ReceiptEvent) -> None:
        seen.append(event)

    env = _envelope()
    env.receipt.chat_jid = "g@g.us"
    env.receipt.sender_jid = "peer@s.whatsapp.net"
    env.receipt.message_ids.append("M1")
    env.receipt.receipt_type = pb.RECEIPT_TYPE_DELIVERED
    ev = event_from_proto(env)
    assert ev is not None
    await router.dispatch(ev, ctx=cast(Any, AsyncMock()))
    assert len(seen) == 1
    assert seen[0].receipt_type is ReceiptType.DELIVERED


async def test_router_on_presence_decorator() -> None:
    router = Router()
    seen: list[PresenceEvent] = []

    @router.on_presence()
    async def on_p(event: PresenceEvent) -> None:
        seen.append(event)

    env = _envelope()
    env.presence.from_jid = "peer@s.whatsapp.net"
    env.presence.unavailable = False
    ev = event_from_proto(env)
    assert ev is not None
    await router.dispatch(ev, ctx=cast(Any, AsyncMock()))
    assert len(seen) == 1
    assert seen[0].from_jid == "peer@s.whatsapp.net"


async def test_router_on_chat_presence_decorator() -> None:
    router = Router()
    seen: list[ChatPresenceEvent] = []

    @router.on_chat_presence()
    async def on_cp(event: ChatPresenceEvent) -> None:
        seen.append(event)

    env = _envelope()
    env.chat_presence.chat_jid = "peer@s.whatsapp.net"
    env.chat_presence.sender_jid = "peer@s.whatsapp.net"
    env.chat_presence.state = pb.CHAT_PRESENCE_STATE_PAUSED
    env.chat_presence.media = pb.CHAT_PRESENCE_MEDIA_TEXT
    ev = event_from_proto(env)
    assert ev is not None
    await router.dispatch(ev, ctx=cast(Any, AsyncMock()))
    assert len(seen) == 1
    assert seen[0].state is ChatPresenceState.PAUSED


# ---------------------------------------------------------------------------
# Router.has_soft_event_handlers() — auto include_soft_events introspection
# ---------------------------------------------------------------------------


def test_has_soft_event_handlers_false_when_no_handlers() -> None:
    assert Router().has_soft_event_handlers() is False


def test_has_soft_event_handlers_false_when_only_hard_handlers() -> None:
    router = Router()

    @router.connected()
    async def _h(event: Any) -> None: ...

    assert router.has_soft_event_handlers() is False


@pytest.mark.parametrize("decorator", ["on_receipt", "on_presence", "on_chat_presence"])
def test_has_soft_event_handlers_true_for_each_soft_decorator(decorator: str) -> None:
    router = Router()
    deco = getattr(router, decorator)()

    @deco
    async def _h(event: Any) -> None: ...

    assert router.has_soft_event_handlers() is True


def test_has_soft_event_handlers_traverses_sub_routers() -> None:
    parent = Router()
    child = Router()

    @child.on_receipt()
    async def _h(event: ReceiptEvent) -> None: ...

    parent.include_router(child)
    assert parent.has_soft_event_handlers() is True


# ---------------------------------------------------------------------------
# _BoundClient — 4 new RPCs forward correctly
# ---------------------------------------------------------------------------


def _make_client(transport: Any) -> _BoundClient:
    return _BoundClient(_transport=transport, account_key="alice", jid="aaa")


async def test_bound_client_mark_read_default_receipt_type() -> None:
    transport: Any = AsyncMock()
    transport.mark_read.return_value = None
    await _make_client(transport).mark_read(
        "g@g.us", "peer@s.whatsapp.net", ["M1", "M2"]
    )
    transport.mark_read.assert_awaited_once_with(
        account_key="alice",
        chat_jid="g@g.us",
        sender_jid="peer@s.whatsapp.net",
        message_ids=("M1", "M2"),
        receipt_type=ReceiptType.READ,
        read_at=None,
    )


async def test_bound_client_mark_read_played_with_timestamp() -> None:
    transport: Any = AsyncMock()
    when = datetime(2025, 1, 1, tzinfo=UTC)
    await _make_client(transport).mark_read(
        "peer@s.whatsapp.net",
        "peer@s.whatsapp.net",
        ["M1"],
        receipt_type=ReceiptType.PLAYED,
        read_at=when,
    )
    transport.mark_read.assert_awaited_once_with(
        account_key="alice",
        chat_jid="peer@s.whatsapp.net",
        sender_jid="peer@s.whatsapp.net",
        message_ids=("M1",),
        receipt_type=ReceiptType.PLAYED,
        read_at=when,
    )


async def test_bound_client_send_presence_passes_enum() -> None:
    transport: Any = AsyncMock()
    await _make_client(transport).send_presence(PresenceType.AVAILABLE)
    transport.send_presence.assert_awaited_once_with(
        account_key="alice", presence=PresenceType.AVAILABLE
    )


async def test_bound_client_send_chat_presence_default_media() -> None:
    transport: Any = AsyncMock()
    await _make_client(transport).send_chat_presence(
        "peer@s.whatsapp.net", ChatPresenceState.COMPOSING
    )
    transport.send_chat_presence.assert_awaited_once_with(
        account_key="alice",
        chat_jid="peer@s.whatsapp.net",
        state=ChatPresenceState.COMPOSING,
        media=ChatPresenceMedia.TEXT,
    )


async def test_bound_client_send_chat_presence_audio() -> None:
    transport: Any = AsyncMock()
    await _make_client(transport).send_chat_presence(
        "peer@s.whatsapp.net",
        ChatPresenceState.COMPOSING,
        media=ChatPresenceMedia.AUDIO,
    )
    transport.send_chat_presence.assert_awaited_once_with(
        account_key="alice",
        chat_jid="peer@s.whatsapp.net",
        state=ChatPresenceState.COMPOSING,
        media=ChatPresenceMedia.AUDIO,
    )


async def test_bound_client_subscribe_presence() -> None:
    transport: Any = AsyncMock()
    await _make_client(transport).subscribe_presence("peer@s.whatsapp.net")
    transport.subscribe_presence.assert_awaited_once_with(
        account_key="alice", jid="peer@s.whatsapp.net"
    )


# ---------------------------------------------------------------------------
# _translate — receipt / presence RPC error mapping
# ---------------------------------------------------------------------------


class _FakeRpcError(grpc.aio.AioRpcError):
    """grpc.aio.AioRpcError 的最小可构造替身 —— 只暴露 code()/details()。"""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:  # type: ignore[override]
        return self._code

    def details(self) -> str:  # type: ignore[override]
        return self._details


@pytest.mark.parametrize(
    "op", ["mark_read", "send_presence", "send_chat_presence", "subscribe_presence"]
)
def test_translate_invalid_jid_for_receipt_presence_ops(op: Any) -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "invalid jid 'foo'")
    out = _translate(exc, op=op, account_key="alice")
    assert isinstance(out, InvalidJIDError)


@pytest.mark.parametrize(
    "op", ["mark_read", "send_presence", "send_chat_presence", "subscribe_presence"]
)
def test_translate_other_invalid_argument_falls_back_to_config_error(op: Any) -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "missing message_ids")
    out = _translate(exc, op=op, account_key="alice")
    # 非 JID / 非 send_message 的 INVALID_ARGUMENT 落到 ConfigurationError。
    assert isinstance(out, ConfigurationError)
