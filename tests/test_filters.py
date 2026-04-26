"""Tests for the magic-F filter DSL."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from fastmeow.filters import F, _Magic


@dataclass
class FakeMsg:
    text: str = ""
    is_group: bool = False
    chat_jid: str = ""
    sender_jid: str = ""
    from_me: bool = False


# ---------------------------------------------------------------------------
# Attribute access & truthiness
# ---------------------------------------------------------------------------


def test_attr_truthy() -> None:
    msg = FakeMsg(text="hi", is_group=True)
    assert F.text.resolve(msg).passed is True
    assert F.is_group.resolve(msg).passed is True


def test_attr_falsy() -> None:
    msg = FakeMsg()  # all defaults
    assert F.text.resolve(msg).passed is False
    assert F.is_group.resolve(msg).passed is False


def test_attr_missing_returns_false_no_raise() -> None:
    """Accessing an attribute that doesn't exist must not raise.

    Filters are written against the public Event union; not every event
    type has every field, so a graceful False is the contract.
    """
    msg = FakeMsg()
    result = F.does_not_exist.resolve(msg)
    assert result.passed is False
    assert result.match is None


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


def test_eq() -> None:
    msg = FakeMsg(text="ping")
    assert (F.text == "ping").resolve(msg).passed is True
    assert (F.text == "pong").resolve(msg).passed is False


def test_ne() -> None:
    msg = FakeMsg(text="ping")
    assert (F.text != "pong").resolve(msg).passed is True
    assert (F.text != "ping").resolve(msg).passed is False


def test_eq_on_missing_attribute_is_false() -> None:
    msg = FakeMsg()
    assert (F.nope == "anything").resolve(msg).passed is False


# ---------------------------------------------------------------------------
# String predicates
# ---------------------------------------------------------------------------


def test_startswith() -> None:
    msg = FakeMsg(text="/help me")
    assert F.text.startswith("/").resolve(msg).passed is True
    assert F.text.startswith("hi").resolve(msg).passed is False


def test_endswith() -> None:
    msg = FakeMsg(text="hello.")
    assert F.text.endswith(".").resolve(msg).passed is True


def test_contains() -> None:
    msg = FakeMsg(text="please call me")
    assert F.text.contains("call").resolve(msg).passed is True
    assert F.text.contains("ignore").resolve(msg).passed is False


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------


def test_regex_match_returned() -> None:
    msg = FakeMsg(text="hello alice")
    res = F.text.regex(r"^hello (?P<name>\w+)").resolve(msg)
    assert res.passed is True
    assert res.match is not None
    assert res.match.group("name") == "alice"


def test_regex_no_match() -> None:
    msg = FakeMsg(text="goodbye")
    res = F.text.regex(r"^hello").resolve(msg)
    assert res.passed is False
    assert res.match is None


def test_regex_accepts_compiled_pattern() -> None:
    pat = re.compile(r"\d+")
    msg = FakeMsg(text="order 42 ready")
    res = F.text.regex(pat).resolve(msg)
    assert res.passed is True
    assert res.match is not None and res.match.group(0) == "42"


# ---------------------------------------------------------------------------
# Combinators
# ---------------------------------------------------------------------------


def test_and_short_circuits() -> None:
    msg = FakeMsg(text="ping", is_group=True)
    assert ((F.text == "ping") & F.is_group).resolve(msg).passed is True
    assert ((F.text == "pong") & F.is_group).resolve(msg).passed is False


def test_or_short_circuits() -> None:
    msg = FakeMsg(text="hi", is_group=False)
    assert ((F.text == "hi") | F.is_group).resolve(msg).passed is True
    assert ((F.text == "no") | F.is_group).resolve(msg).passed is False


def test_not() -> None:
    msg = FakeMsg(is_group=True)
    assert (~F.is_group).resolve(msg).passed is False
    assert (~F.from_me).resolve(msg).passed is True


def test_combined_carries_first_regex_match() -> None:
    msg = FakeMsg(text="hello bob", is_group=True)
    chain = F.is_group & F.text.regex(r"hello (?P<who>\w+)")
    res = chain.resolve(msg)
    assert res.passed is True
    assert res.match is not None
    assert res.match.group("who") == "bob"


# ---------------------------------------------------------------------------
# in_
# ---------------------------------------------------------------------------


def test_in_with_set() -> None:
    msg = FakeMsg(sender_jid="alice@s.whatsapp.net")
    assert (
        F.sender_jid.in_({"alice@s.whatsapp.net", "bob@s.whatsapp.net"}).resolve(msg).passed is True
    )
    assert F.sender_jid.in_({"x", "y"}).resolve(msg).passed is False


def test_in_unhashable_unsupported_falsy() -> None:
    """Containers that don't support `in` produce False, not crash."""
    msg = FakeMsg(text="x")
    # int isn't iterable; should evaluate to False without raising.
    res = F.text.in_(42).resolve(msg)
    assert res.passed is False


# ---------------------------------------------------------------------------
# Construction sanity
# ---------------------------------------------------------------------------


def test_F_is_singleton_and_attr_chain_returns_magic() -> None:
    node = F.foo.bar.baz
    assert isinstance(node, _Magic)


def test_dunder_attr_blocked() -> None:
    """Dunder access on F must raise so unrelated lookups (pickle,
    copy.deepcopy, etc.) don't accidentally produce magic nodes."""
    with pytest.raises(AttributeError):
        F.__nope__  # noqa: B018
