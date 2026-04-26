"""Tests for the Router: signature inspection + dispatch + filter chains."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fastmeow.context import Ctx
from fastmeow.exceptions import HandlerSignatureError
from fastmeow.filters import F
from fastmeow.router import Router, SkipHandler
from fastmeow.types import (
    ConnectedEvent,
    DisconnectedEvent,
    MessageEvent,
    QREvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_msg(**overrides: Any) -> MessageEvent:
    base = {
        "seq": 1,
        "sidecar_id": "sc",
        "account_key": "alice",
        "account_jid": "aaa@s.whatsapp.net",
        "observed_at": datetime(2025, 1, 1, tzinfo=UTC),
        "message_id": "MID",
        "chat_jid": "chat@s.whatsapp.net",
        "sender_jid": "sender@s.whatsapp.net",
        "from_me": False,
        "is_group": False,
        "text": "",
        "reply_to_message_id": "",
        "timestamp": None,
    }
    base.update(overrides)
    return MessageEvent(**base)  # type: ignore[arg-type]


def make_connected() -> ConnectedEvent:
    return ConnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def make_qr(code: str = "raw-code") -> QREvent:
    return QREvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        code=code,
        ttl_seconds=20,
    )


def make_ctx(event: Any) -> Ctx:
    # Filters / dispatch don't touch the client, so a mock is fine.
    return Ctx(
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
        event=event,
        client=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Decorator + signature validation
# ---------------------------------------------------------------------------


async def test_simple_message_handler_dispatched() -> None:
    router = Router()
    seen: list[str] = []

    @router.message()
    async def handle(msg: MessageEvent, ctx: Ctx) -> None:
        seen.append(msg.text)

    msg = make_msg(text="hello")
    await router.dispatch(msg, make_ctx(msg))
    assert seen == ["hello"]


async def test_handler_can_omit_ctx() -> None:
    router = Router()
    seen: list[str] = []

    @router.message()
    async def handle(msg: MessageEvent) -> None:
        seen.append(msg.text)

    msg = make_msg(text="solo")
    await router.dispatch(msg, make_ctx(msg))
    assert seen == ["solo"]


async def test_handler_can_omit_msg() -> None:
    router = Router()
    seen: list[str] = []

    @router.connected()
    async def on_up(ctx: Ctx) -> None:
        seen.append(ctx.account_key)

    ev = make_connected()
    await router.dispatch(ev, make_ctx(ev))
    assert seen == ["alice"]


async def test_event_alias_works() -> None:
    router = Router()
    captured: list[Any] = []

    @router.message()
    async def handle(event: MessageEvent) -> None:
        captured.append(event)

    msg = make_msg(text="x")
    await router.dispatch(msg, make_ctx(msg))
    assert captured == [msg]


async def test_match_param_injection() -> None:
    router = Router()
    captured: list[str] = []

    @router.message(F.text.regex(r"^hi (?P<who>\w+)"))
    async def handle(msg: MessageEvent, match: Any) -> None:
        assert match is not None
        captured.append(match.group("who"))

    await router.dispatch(make_msg(text="hi alice"), make_ctx(make_msg(text="hi alice")))
    assert captured == ["alice"]


async def test_match_is_none_without_regex_filter() -> None:
    router = Router()
    captured: list[Any] = []

    @router.message()
    async def handle(msg: MessageEvent, match: Any) -> None:
        captured.append(match)

    msg = make_msg(text="x")
    await router.dispatch(msg, make_ctx(msg))
    assert captured == [None]


async def test_qr_param_only_on_qr_handlers() -> None:
    router = Router()

    with pytest.raises(HandlerSignatureError):

        @router.message()
        async def bad(qr: Any) -> None:  # pragma: no cover
            pass


async def test_qr_param_works_on_qr_handler() -> None:
    router = Router()
    seen: list[str] = []

    @router.qr()
    async def on_qr(qr: QREvent) -> None:
        seen.append(qr.code)

    ev = make_qr("CODE-123")
    await router.dispatch(ev, make_ctx(ev))
    assert seen == ["CODE-123"]


async def test_unknown_param_raises() -> None:
    router = Router()

    with pytest.raises(HandlerSignatureError):

        @router.message()
        async def bad(weird_thing: Any) -> None:  # pragma: no cover
            pass


async def test_unknown_param_with_default_allowed() -> None:
    """User wants to bind something themselves via partial — allowed
    when there's a default value."""
    router = Router()
    seen: list[int] = []

    @router.message()
    async def ok(msg: MessageEvent, my_state: int = 7) -> None:
        seen.append(my_state)

    msg = make_msg(text="x")
    await router.dispatch(msg, make_ctx(msg))
    assert seen == [7]


async def test_sync_handler_rejected() -> None:
    router = Router()

    def bad(msg: MessageEvent) -> None:  # pragma: no cover
        pass

    with pytest.raises(HandlerSignatureError):
        router.message()(bad)  # type: ignore[arg-type]


async def test_msg_and_event_simultaneously_rejected() -> None:
    router = Router()

    with pytest.raises(HandlerSignatureError):

        @router.message()
        async def bad(msg: Any, event: Any) -> None:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# Filter chains
# ---------------------------------------------------------------------------


async def test_filter_skips_non_matching() -> None:
    router = Router()
    seen: list[str] = []

    @router.message(F.text == "ping")
    async def handle(msg: MessageEvent) -> None:
        seen.append(msg.text)

    msg = make_msg(text="pong")
    await router.dispatch(msg, make_ctx(msg))
    assert seen == []


async def test_callable_filter_supported() -> None:
    router = Router()
    seen: list[str] = []

    def has_at(ev: Any) -> bool:
        return "@" in getattr(ev, "text", "")

    @router.message(has_at)
    async def handle(msg: MessageEvent) -> None:
        seen.append(msg.text)

    await router.dispatch(make_msg(text="hello@world"), make_ctx(make_msg(text="hello@world")))
    await router.dispatch(make_msg(text="plain"), make_ctx(make_msg(text="plain")))
    assert seen == ["hello@world"]


async def test_async_callable_filter_supported() -> None:
    router = Router()
    seen: list[str] = []

    async def is_dm(ev: Any) -> bool:
        return not getattr(ev, "is_group", False)

    @router.message(is_dm)
    async def handle(msg: MessageEvent) -> None:
        seen.append(msg.text)

    await router.dispatch(make_msg(text="dm", is_group=False), make_ctx(make_msg(text="dm")))
    await router.dispatch(make_msg(text="group", is_group=True), make_ctx(make_msg(text="group")))
    assert seen == ["dm"]


# ---------------------------------------------------------------------------
# Multiple handlers + SkipHandler
# ---------------------------------------------------------------------------


async def test_first_match_wins_short_circuits_iteration() -> None:
    """First matching handler wins; later candidates do not run."""
    router = Router()
    log: list[str] = []

    @router.message()
    async def first(msg: MessageEvent) -> None:
        log.append("first")

    @router.message()
    async def second(msg: MessageEvent) -> None:
        log.append("second")

    msg = make_msg(text="x")
    await router.dispatch(msg, make_ctx(msg))
    assert log == ["first"]


async def test_skip_handler_falls_through() -> None:
    router = Router()
    log: list[str] = []

    @router.message()
    async def first(msg: MessageEvent) -> None:
        log.append("first")
        raise SkipHandler

    @router.message()
    async def second(msg: MessageEvent) -> None:
        log.append("second")

    msg = make_msg(text="x")
    await router.dispatch(msg, make_ctx(msg))
    assert log == ["first", "second"]


# ---------------------------------------------------------------------------
# Router composition
# ---------------------------------------------------------------------------


async def test_include_router_dispatches_subhandlers() -> None:
    parent = Router(name="parent")
    child = Router(name="child")
    log: list[str] = []

    @child.message()
    async def child_h(msg: MessageEvent) -> None:
        log.append("child")

    parent.include_router(child)

    @parent.message()
    async def parent_h(msg: MessageEvent) -> None:
        log.append("parent")

    msg = make_msg(text="x")
    await parent.dispatch(msg, make_ctx(msg))
    # First-match-wins: parent's own handler is first in iteration
    # order, so the child handler does not run.
    assert log == ["parent"]


async def test_include_router_falls_through_to_child_via_skip() -> None:
    """Child handlers are reachable when parent handlers raise SkipHandler."""
    parent = Router(name="parent")
    child = Router(name="child")
    log: list[str] = []

    @child.message()
    async def child_h(msg: MessageEvent) -> None:
        log.append("child")

    parent.include_router(child)

    @parent.message()
    async def parent_h(msg: MessageEvent) -> None:
        log.append("parent")
        raise SkipHandler

    msg = make_msg(text="x")
    await parent.dispatch(msg, make_ctx(msg))
    assert log == ["parent", "child"]


async def test_include_router_rejects_self() -> None:
    r = Router()
    with pytest.raises(ValueError):
        r.include_router(r)


async def test_include_router_rejects_cycle() -> None:
    a = Router(name="a")
    b = Router(name="b")
    a.include_router(b)
    with pytest.raises(ValueError):
        b.include_router(a)


# ---------------------------------------------------------------------------
# Dispatch error propagation
# ---------------------------------------------------------------------------


async def test_handler_exception_propagates() -> None:
    router = Router()

    @router.message()
    async def boom(msg: MessageEvent) -> None:
        raise RuntimeError("kaboom")

    msg = make_msg(text="x")
    with pytest.raises(RuntimeError, match="kaboom"):
        await router.dispatch(msg, make_ctx(msg))


# ---------------------------------------------------------------------------
# Disconnected event sanity
# ---------------------------------------------------------------------------


async def test_disconnected_handler_runs() -> None:
    router = Router()
    seen: list[str] = []

    @router.disconnected()
    async def on_down(ctx: Ctx) -> None:
        ev = ctx.event
        assert isinstance(ev, DisconnectedEvent)
        seen.append(ev.reason)

    ev = DisconnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        reason="kicked",
    )
    await router.dispatch(ev, make_ctx(ev))
    assert seen == ["kicked"]


# ensure async helpers are typed for mypy
_: Awaitable[None] | None = None
