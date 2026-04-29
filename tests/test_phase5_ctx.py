"""Phase 5 Ctx 单元测试：reply_react / reply_edit / reply_revoke。

覆盖范围（plan §3.3）：

* ``Ctx.reply_react`` / ``reply_edit`` / ``reply_revoke``：仅
  :class:`MessageEvent` 上下文允许；其他事件抛 :class:`ReplyNotAvailableError`。
* ``reply_react`` 自动透传 ``msg.sender_jid`` 作为 ``target_sender_jid``
  （群里反应他人消息时的 whatsmeow 必需字段）。
* ``reply_revoke`` 同样透传 ``sender_jid`` 用于群管理员撤回路径。
* 调用形式：转发到 ``client.send_reaction/send_edit/send_revoke`` 时键值精确。

不依赖真实 sidecar；FakeClient 直接记录调用参数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fastmeow import (
    AccountClient,
    ConnectedEvent,
    Ctx,
    MessageEvent,
)
from fastmeow.exceptions import ReplyNotAvailableError
from fastmeow.types import SendResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)


_BASE_FIELDS: dict[str, Any] = {
    "seq": 1,
    "sidecar_id": "default",
    "account_key": "alice",
    "account_jid": "1@s.whatsapp.net",
    "observed_at": _FIXED_TS,
}


def _make_message_event(
    *,
    message_id: str = "MSG-CTX",
    chat_jid: str = "bob@s.whatsapp.net",
    sender_jid: str = "bob@s.whatsapp.net",
    is_group: bool = False,
) -> MessageEvent:
    return MessageEvent(
        **_BASE_FIELDS,
        message_id=message_id,
        chat_jid=chat_jid,
        sender_jid=sender_jid,
        from_me=False,
        is_group=is_group,
        text="hi",
    )


def _make_connected_event() -> ConnectedEvent:
    return ConnectedEvent(**_BASE_FIELDS)


def _make_ctx(*, event: Any) -> tuple[Ctx, Any]:
    client: Any = AsyncMock()
    client.send_reaction.return_value = SendResult(
        message_id="X", server_timestamp=_FIXED_TS, deduped=False
    )
    client.send_edit.return_value = SendResult(
        message_id="E", server_timestamp=_FIXED_TS, deduped=False
    )
    client.send_revoke.return_value = SendResult(
        message_id="R", server_timestamp=_FIXED_TS, deduped=False
    )
    typed_client: AccountClient = client  # type: ignore[assignment]
    ctx = Ctx(
        account_key="alice",
        account_jid="1@s.whatsapp.net",
        event=event,
        client=typed_client,
    )
    return ctx, client


# ---------------------------------------------------------------------------
# 1. reply_react — 转发 + 自动 sender_jid 透传
# ---------------------------------------------------------------------------


async def test_reply_react_forwards_to_client_send_reaction() -> None:
    ev = _make_message_event(
        message_id="M1",
        chat_jid="g@g.us",
        sender_jid="charlie@s.whatsapp.net",
        is_group=True,
    )
    ctx, client = _make_ctx(event=ev)

    out = await ctx.reply_react("👍")

    client.send_reaction.assert_awaited_once_with(
        "g@g.us",
        "M1",
        "👍",
        target_sender_jid="charlie@s.whatsapp.net",
    )
    assert out.message_id == "X"


async def test_reply_react_empty_emoji_removes_reaction() -> None:
    ev = _make_message_event(message_id="M2")
    ctx, client = _make_ctx(event=ev)

    await ctx.reply_react("")

    args, _kwargs = client.send_reaction.await_args
    assert args[2] == ""


async def test_reply_react_raises_on_non_message_event() -> None:
    ctx, client = _make_ctx(event=_make_connected_event())

    with pytest.raises(ReplyNotAvailableError):
        await ctx.reply_react("👍")
    client.send_reaction.assert_not_called()


# ---------------------------------------------------------------------------
# 2. reply_edit
# ---------------------------------------------------------------------------


async def test_reply_edit_forwards_to_client_send_edit() -> None:
    ev = _make_message_event(message_id="M3", chat_jid="bob@s.whatsapp.net")
    ctx, client = _make_ctx(event=ev)

    out = await ctx.reply_edit("corrected")

    client.send_edit.assert_awaited_once_with(
        "bob@s.whatsapp.net",
        "M3",
        "corrected",
    )
    assert out.message_id == "E"


async def test_reply_edit_raises_on_non_message_event() -> None:
    ctx, client = _make_ctx(event=_make_connected_event())

    with pytest.raises(ReplyNotAvailableError):
        await ctx.reply_edit("oops")
    client.send_edit.assert_not_called()


# ---------------------------------------------------------------------------
# 3. reply_revoke
# ---------------------------------------------------------------------------


async def test_reply_revoke_forwards_with_sender_jid() -> None:
    ev = _make_message_event(
        message_id="M4",
        chat_jid="g@g.us",
        sender_jid="dan@s.whatsapp.net",
        is_group=True,
    )
    ctx, client = _make_ctx(event=ev)

    out = await ctx.reply_revoke()

    client.send_revoke.assert_awaited_once_with(
        "g@g.us",
        "M4",
        target_sender_jid="dan@s.whatsapp.net",
    )
    assert out.message_id == "R"


async def test_reply_revoke_raises_on_non_message_event() -> None:
    ctx, client = _make_ctx(event=_make_connected_event())

    with pytest.raises(ReplyNotAvailableError):
        await ctx.reply_revoke()
    client.send_revoke.assert_not_called()
