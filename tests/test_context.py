"""用于 Ctx 上下文对象的测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fastmeow.context import Ctx
from fastmeow.exceptions import ReplyNotAvailableError
from fastmeow.types import ConnectedEvent, MessageEvent, SendResult


def make_msg() -> MessageEvent:
    return MessageEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        message_id="MID-1",
        chat_jid="chat@s.whatsapp.net",
        sender_jid="sender@s.whatsapp.net",
        from_me=False,
        is_group=False,
        text="hi",
        reply_to_message_id="",
        timestamp=None,
    )


def make_send_result() -> SendResult:
    return SendResult(
        message_id="OUT-1",
        server_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        deduped=False,
    )


# ---------------------------------------------------------------------------
# 回复()
# ---------------------------------------------------------------------------


async def test_reply_quotes_inbound_by_default() -> None:
    msg = make_msg()
    client: Any = AsyncMock()
    client.send_text.return_value = make_send_result()

    ctx = Ctx(account_key="alice", account_jid="aaa", event=msg, client=client)
    result = await ctx.reply("pong")

    client.send_text.assert_awaited_once_with(
        "chat@s.whatsapp.net",
        "pong",
        client_msg_id=None,
        reply_to_message_id="MID-1",
    )
    assert result.message_id == "OUT-1"


async def test_reply_unquoted() -> None:
    msg = make_msg()
    client: Any = AsyncMock()
    client.send_text.return_value = make_send_result()

    ctx = Ctx(account_key="alice", account_jid="aaa", event=msg, client=client)
    await ctx.reply("pong", quoted=False)

    args = client.send_text.await_args
    assert args is not None
    assert args.kwargs["reply_to_message_id"] is None


async def test_reply_passes_explicit_client_msg_id() -> None:
    msg = make_msg()
    client: Any = AsyncMock()
    client.send_text.return_value = make_send_result()

    ctx = Ctx(account_key="alice", account_jid="aaa", event=msg, client=client)
    await ctx.reply("pong", client_msg_id="my-uuid")

    args = client.send_text.await_args
    assert args is not None
    assert args.kwargs["client_msg_id"] == "my-uuid"


async def test_reply_raises_for_non_message_event() -> None:
    ev = ConnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    ctx = Ctx(account_key="alice", account_jid="aaa", event=ev, client=AsyncMock())
    with pytest.raises(ReplyNotAvailableError):
        await ctx.reply("hi")


# ---------------------------------------------------------------------------
# 发送()
# ---------------------------------------------------------------------------


async def test_send_to_arbitrary_jid_works_for_any_event() -> None:
    ev = ConnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    client: Any = AsyncMock()
    client.send_text.return_value = make_send_result()

    ctx = Ctx(account_key="alice", account_jid="aaa", event=ev, client=client)
    await ctx.send("someone@s.whatsapp.net", "hello")

    client.send_text.assert_awaited_once_with(
        "someone@s.whatsapp.net",
        "hello",
        client_msg_id=None,
    )


# ---------------------------------------------------------------------------
# .message 便捷访问器
# ---------------------------------------------------------------------------


def test_is_message_predicate() -> None:
    msg = make_msg()
    ctx_msg = Ctx(account_key="a", account_jid="j", event=msg, client=AsyncMock())
    assert ctx_msg.is_message is True

    ev = ConnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    ctx_other = Ctx(account_key="a", account_jid="j", event=ev, client=AsyncMock())
    assert ctx_other.is_message is False


def test_message_property_narrows() -> None:
    msg = make_msg()
    ctx = Ctx(account_key="a", account_jid="j", event=msg, client=AsyncMock())
    assert ctx.message is msg


def test_message_property_raises_on_non_message() -> None:
    ev = ConnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    ctx = Ctx(account_key="a", account_jid="j", event=ev, client=AsyncMock())
    with pytest.raises(ReplyNotAvailableError):
        _ = ctx.message
