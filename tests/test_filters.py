"""用于 magic-F 过滤器 DSL 的测试。"""

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
# 属性访问与真值判断
# ---------------------------------------------------------------------------


def test_attr_truthy() -> None:
    msg = FakeMsg(text="hi", is_group=True)
    assert F.text.resolve(msg).passed is True
    assert F.is_group.resolve(msg).passed is True


def test_attr_falsy() -> None:
    msg = FakeMsg()  # 全部为默认值
    assert F.text.resolve(msg).passed is False
    assert F.is_group.resolve(msg).passed is False


def test_attr_missing_returns_false_no_raise() -> None:
    """访问不存在的属性时必须不抛异常。

    过滤器是针对公开的 Event 联合类型编写的；并非每种事件类型
    都拥有所有字段，因此优雅返回 False 才是约定。
    """
    msg = FakeMsg()
    result = F.does_not_exist.resolve(msg)
    assert result.passed is False
    assert result.match is None


# ---------------------------------------------------------------------------
# 比较
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
# 字符串谓词
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
# 正则表达式
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
# 组合器
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
    """不支持 `in` 的容器应返回 False，而不是崩溃。"""
    msg = FakeMsg(text="x")
    # int 不可迭代；应在不抛异常的情况下求值为 False。
    res = F.text.in_(42).resolve(msg)
    assert res.passed is False


# ---------------------------------------------------------------------------
# 构造校验
# ---------------------------------------------------------------------------


def test_F_is_singleton_and_attr_chain_returns_magic() -> None:
    node = F.foo.bar.baz
    assert isinstance(node, _Magic)


def test_dunder_attr_blocked() -> None:
    """F 上的双下划线访问必须抛异常，这样无关查找（pickle、
    copy.deepcopy 等）就不会意外生成魔法节点。"""
    with pytest.raises(AttributeError):
        F.__nope__  # noqa: B018
