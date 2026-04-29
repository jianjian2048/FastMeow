"""Phase 5 出站单元测试：reactions / edits / revokes。

覆盖范围（plan §3.3）：

* ``Transport.send_reaction / send_edit / send_revoke``：proto 请求字段装配 +
  ``DEFAULT_DEADLINE`` 透传 + ``SendResult.from_proto`` 解码。
* ``_translate``：3 个新 op 在 ``INVALID_ARGUMENT`` 路径上分别落到
  :class:`ReactionError` / :class:`MessageEditError` / :class:`MessageRevokeError`，
  非"jid"细节才进特化分支；含 jid 的细节优先走 :class:`InvalidJIDError`。
* ``_BoundClient.send_reaction / send_edit / send_revoke``：账户绑定转发 +
  关键字签名。

不依赖真实 sidecar；FakeStub 直接构造 proto response。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest

from fastmeow._dispatcher import _BoundClient
from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb
from fastmeow._transport import Transport, _translate
from fastmeow.exceptions import (
    InvalidJIDError,
    MessageEditError,
    MessageRevokeError,
    ReactionError,
    TransportError,
)
from fastmeow.types import SendResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)


def _make_response(
    cls: type[Any], message_id: str = "WAID-PHASE5", deduped: bool = False
) -> Any:
    """构造 SendReaction/Edit/RevokeResponse；server_timestamp 取固定值。"""
    from google.protobuf.timestamp_pb2 import Timestamp

    ts = Timestamp()
    ts.FromDatetime(_FIXED_TS)
    resp = cls(message_id=message_id, deduped=deduped)
    resp.server_timestamp.CopyFrom(ts)
    return resp


class _FakeRpcError(grpc.aio.AioRpcError):
    """grpc.aio.AioRpcError 的最小可构造替身——仅暴露 code()/details()。"""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:  # type: ignore[override]
        return self._code

    def details(self) -> str:  # type: ignore[override]
        return self._details


def _make_transport(stub: Any) -> Transport:
    """直接构造 Transport 实例，绕过 connect/handshake；只用于驱动 RPC 方法。"""
    tx = Transport.__new__(Transport)
    tx._stub = stub  # type: ignore[attr-defined]
    return tx


# ---------------------------------------------------------------------------
# 1. Transport.send_reaction — 请求装配 / 响应解码 / 错误传播
# ---------------------------------------------------------------------------


async def test_send_reaction_assembles_request_fields() -> None:
    stub = AsyncMock()
    stub.SendReaction.return_value = _make_response(pb.SendReactionResponse)
    tx = _make_transport(stub)

    result = await tx.send_reaction(
        account_key="alice",
        chat_jid="g@g.us",
        target_message_id="MSG-1",
        emoji="👍",
        target_sender_jid="bob@s.whatsapp.net",
    )

    stub.SendReaction.assert_awaited_once()
    req = stub.SendReaction.await_args.args[0]
    assert isinstance(req, pb.SendReactionRequest)
    assert req.account_key == "alice"
    assert req.chat_jid == "g@g.us"
    assert req.target_message_id == "MSG-1"
    assert req.target_sender_jid == "bob@s.whatsapp.net"
    assert req.emoji == "👍"
    assert isinstance(result, SendResult)
    assert result.message_id == "WAID-PHASE5"
    assert result.deduped is False


async def test_send_reaction_target_sender_jid_defaults_to_empty_string() -> None:
    """DM 场景下 ``target_sender_jid=None`` 必须落成 proto 空字符串。"""
    stub = AsyncMock()
    stub.SendReaction.return_value = _make_response(pb.SendReactionResponse)
    tx = _make_transport(stub)

    await tx.send_reaction(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-2",
        emoji="🔥",
    )

    req = stub.SendReaction.await_args.args[0]
    assert req.target_sender_jid == ""


async def test_send_reaction_empty_emoji_means_remove() -> None:
    """空字符串 emoji 表示取消反应（whatsmeow RemoveReactionText）。"""
    stub = AsyncMock()
    stub.SendReaction.return_value = _make_response(pb.SendReactionResponse)
    tx = _make_transport(stub)

    await tx.send_reaction(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-3",
        emoji="",
    )

    req = stub.SendReaction.await_args.args[0]
    assert req.emoji == ""


async def test_send_reaction_translates_grpc_error_to_reaction_error() -> None:
    stub = AsyncMock()
    stub.SendReaction.side_effect = _FakeRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "emoji too long"
    )
    tx = _make_transport(stub)

    with pytest.raises(ReactionError):
        await tx.send_reaction(
            account_key="alice",
            chat_jid="bob@s.whatsapp.net",
            target_message_id="MSG-4",
            emoji="x" * 100,
        )


async def test_send_reaction_invalid_jid_takes_precedence() -> None:
    """grpc detail 含 'jid' 时应落 :class:`InvalidJIDError`，不进 ReactionError 分支。"""
    stub = AsyncMock()
    stub.SendReaction.side_effect = _FakeRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "bad jid format"
    )
    tx = _make_transport(stub)

    with pytest.raises(InvalidJIDError):
        await tx.send_reaction(
            account_key="alice",
            chat_jid="not-a-jid",
            target_message_id="MSG-5",
            emoji="👍",
        )


# ---------------------------------------------------------------------------
# 2. Transport.send_edit
# ---------------------------------------------------------------------------


async def test_send_edit_assembles_request_fields() -> None:
    stub = AsyncMock()
    stub.SendEdit.return_value = _make_response(pb.SendEditResponse, "WAID-EDIT")
    tx = _make_transport(stub)

    result = await tx.send_edit(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-6",
        new_text="updated text",
    )

    stub.SendEdit.assert_awaited_once()
    req = stub.SendEdit.await_args.args[0]
    assert isinstance(req, pb.SendEditRequest)
    assert req.account_key == "alice"
    assert req.chat_jid == "bob@s.whatsapp.net"
    assert req.target_message_id == "MSG-6"
    assert req.new_text == "updated text"
    assert result.message_id == "WAID-EDIT"


async def test_send_edit_translates_grpc_error_to_edit_error() -> None:
    stub = AsyncMock()
    stub.SendEdit.side_effect = _FakeRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "edit window expired"
    )
    tx = _make_transport(stub)

    with pytest.raises(MessageEditError):
        await tx.send_edit(
            account_key="alice",
            chat_jid="bob@s.whatsapp.net",
            target_message_id="MSG-7",
            new_text="too late",
        )


async def test_send_edit_invalid_jid_takes_precedence() -> None:
    stub = AsyncMock()
    stub.SendEdit.side_effect = _FakeRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "invalid jid"
    )
    tx = _make_transport(stub)

    with pytest.raises(InvalidJIDError):
        await tx.send_edit(
            account_key="alice",
            chat_jid="bad",
            target_message_id="MSG-8",
            new_text="x",
        )


# ---------------------------------------------------------------------------
# 3. Transport.send_revoke
# ---------------------------------------------------------------------------


async def test_send_revoke_assembles_request_fields() -> None:
    stub = AsyncMock()
    stub.SendRevoke.return_value = _make_response(pb.SendRevokeResponse, "WAID-REV")
    tx = _make_transport(stub)

    result = await tx.send_revoke(
        account_key="alice",
        chat_jid="g@g.us",
        target_message_id="MSG-9",
        target_sender_jid="bob@s.whatsapp.net",
    )

    stub.SendRevoke.assert_awaited_once()
    req = stub.SendRevoke.await_args.args[0]
    assert isinstance(req, pb.SendRevokeRequest)
    assert req.account_key == "alice"
    assert req.chat_jid == "g@g.us"
    assert req.target_message_id == "MSG-9"
    assert req.target_sender_jid == "bob@s.whatsapp.net"
    assert result.message_id == "WAID-REV"


async def test_send_revoke_target_sender_jid_defaults_to_empty_string() -> None:
    """撤回自己消息时 ``target_sender_jid=None`` 必须落空字符串
    （whatsmeow 内部用 EmptyJID 标记 FromMe=true）。"""
    stub = AsyncMock()
    stub.SendRevoke.return_value = _make_response(pb.SendRevokeResponse)
    tx = _make_transport(stub)

    await tx.send_revoke(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-10",
    )

    req = stub.SendRevoke.await_args.args[0]
    assert req.target_sender_jid == ""


async def test_send_revoke_translates_grpc_error_to_revoke_error() -> None:
    stub = AsyncMock()
    stub.SendRevoke.side_effect = _FakeRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "cannot revoke this message"
    )
    tx = _make_transport(stub)

    with pytest.raises(MessageRevokeError):
        await tx.send_revoke(
            account_key="alice",
            chat_jid="bob@s.whatsapp.net",
            target_message_id="MSG-11",
        )


async def test_send_revoke_invalid_jid_takes_precedence() -> None:
    stub = AsyncMock()
    stub.SendRevoke.side_effect = _FakeRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, "jid parse failed"
    )
    tx = _make_transport(stub)

    with pytest.raises(InvalidJIDError):
        await tx.send_revoke(
            account_key="alice",
            chat_jid="bad",
            target_message_id="MSG-12",
        )


# ---------------------------------------------------------------------------
# 4. _translate — 显式 op 分支覆盖
# ---------------------------------------------------------------------------


def test_translate_send_reaction_invalid_argument_maps_to_reaction_error() -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "missing emoji")
    out = _translate(exc, op="send_reaction", account_key="alice")
    assert isinstance(out, ReactionError)


def test_translate_send_edit_invalid_argument_maps_to_edit_error() -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "new_text empty")
    out = _translate(exc, op="send_edit", account_key="alice")
    assert isinstance(out, MessageEditError)


def test_translate_send_revoke_invalid_argument_maps_to_revoke_error() -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "not author")
    out = _translate(exc, op="send_revoke", account_key="alice")
    assert isinstance(out, MessageRevokeError)


def test_translate_send_reaction_unimplemented_falls_back_to_transport_error() -> None:
    """旧 sidecar+新 client 兼容：UNIMPLEMENTED 走通用 TransportError 末尾分支。"""
    exc = _FakeRpcError(grpc.StatusCode.UNIMPLEMENTED, "unknown method SendReaction")
    out = _translate(exc, op="send_reaction", account_key="alice")
    assert isinstance(out, TransportError)
    assert not isinstance(out, ReactionError)


# ---------------------------------------------------------------------------
# 5. _BoundClient — 账号绑定转发
# ---------------------------------------------------------------------------


def _make_bound(transport: Any) -> _BoundClient:
    return _BoundClient(_transport=transport, account_key="alice", jid="1@s.whatsapp.net")


async def test_bound_client_send_reaction_forwards_with_account_key() -> None:
    transport: Any = AsyncMock()
    transport.send_reaction.return_value = SendResult(
        message_id="X", server_timestamp=_FIXED_TS, deduped=False
    )
    bound = _make_bound(transport)

    out = await bound.send_reaction(
        "g@g.us", "MSG-A", "❤️", target_sender_jid="bob@s.whatsapp.net"
    )

    transport.send_reaction.assert_awaited_once_with(
        account_key="alice",
        chat_jid="g@g.us",
        target_message_id="MSG-A",
        emoji="❤️",
        target_sender_jid="bob@s.whatsapp.net",
    )
    assert out.message_id == "X"


async def test_bound_client_send_reaction_target_sender_default_none() -> None:
    transport: Any = AsyncMock()
    transport.send_reaction.return_value = SendResult(
        message_id="X", server_timestamp=_FIXED_TS, deduped=False
    )
    bound = _make_bound(transport)

    await bound.send_reaction("bob@s.whatsapp.net", "MSG-B", "👍")

    transport.send_reaction.assert_awaited_once_with(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-B",
        emoji="👍",
        target_sender_jid=None,
    )


async def test_bound_client_send_edit_forwards_positional_args() -> None:
    transport: Any = AsyncMock()
    transport.send_edit.return_value = SendResult(
        message_id="E", server_timestamp=_FIXED_TS, deduped=False
    )
    bound = _make_bound(transport)

    out = await bound.send_edit("bob@s.whatsapp.net", "MSG-C", "fixed typo")

    transport.send_edit.assert_awaited_once_with(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-C",
        new_text="fixed typo",
    )
    assert out.message_id == "E"


async def test_bound_client_send_revoke_forwards_positional_args() -> None:
    transport: Any = AsyncMock()
    transport.send_revoke.return_value = SendResult(
        message_id="R", server_timestamp=_FIXED_TS, deduped=False
    )
    bound = _make_bound(transport)

    out = await bound.send_revoke(
        "g@g.us", "MSG-D", target_sender_jid="bob@s.whatsapp.net"
    )

    transport.send_revoke.assert_awaited_once_with(
        account_key="alice",
        chat_jid="g@g.us",
        target_message_id="MSG-D",
        target_sender_jid="bob@s.whatsapp.net",
    )
    assert out.message_id == "R"


async def test_bound_client_send_revoke_target_sender_default_none() -> None:
    transport: Any = AsyncMock()
    transport.send_revoke.return_value = SendResult(
        message_id="R", server_timestamp=_FIXED_TS, deduped=False
    )
    bound = _make_bound(transport)

    await bound.send_revoke("bob@s.whatsapp.net", "MSG-E")

    transport.send_revoke.assert_awaited_once_with(
        account_key="alice",
        chat_jid="bob@s.whatsapp.net",
        target_message_id="MSG-E",
        target_sender_jid=None,
    )
