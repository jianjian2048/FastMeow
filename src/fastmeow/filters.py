"""魔法过滤器 ``F`` — aiogram 风格的声明式事件谓词。

过滤器是路由器在决定是否调用处理器之前针对事件评估的内容。
FastMeow 提供了一个受 aiogram 的 :class:`MagicFilter` 启发的微型“魔法”DSL：:

    from fastmeow import F

    @router.message(F.text == "ping")
    async def pong(msg, ctx): ...

    @router.message(F.is_group & F.text.startswith("/"))
    async def commands(msg, ctx): ...

    @router.message(F.text.regex(r"^hello (?P<name>\w+)"))
    async def greet(msg, ctx, match): ...

工作原理
------------
``F`` 是一个 :class:`_MagicRoot` 单例。属性访问 (``F.text``)、
比较 (``== "ping"``) 以及二进制布尔组合器 (``& | ~``) 都会返回新的 :class:`_Magic` 节点，
这些节点*描述*了谓词而不会立即评估它。派发器随后会调用
:meth:`_Magic.resolve(event)` 来获取具体的 :class:`bool` 结果，
以及一个可选的 ``re.Match``（以便处理器捕获正则分组）。

设计约束
------------------
* 不对用户定义的类型使用魔法 — 所有操作都针对公开的 :class:`fastmeow.types.Event` 族。
  如果你需要进行更复杂的测试，请传递普通的 callable：``@router.message(my_predicate)``。
* 不使用反射或 inspect 技巧。魔法 AST 是解释执行的，而不是编译执行的。
  这牺牲了少许速度，以换取清晰的错误提示和可序列化性（如果我们以后提供 worker 模式，这将非常重要）。
* 所有比较都是短路安全的：任何触及事件不具备的属性的分支都会评估为 ``False``，永远不会抛出异常。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Union

__all__ = ["F", "Filter", "FilterResult"]


# 用户提供的过滤器可以是：
#   - 一个魔法节点 (``F.text == "ping"``),
#   - 一个返回 bool / awaitable bool 的普通 callable,
#   - ``None`` (无过滤器).
Filter = Union["_Magic", Callable[..., bool], Callable[..., Awaitable[bool]]]


@dataclass(frozen=True, slots=True)
class FilterResult:
    """针对事件评估过滤器链的结果。

    Args:
        passed: 布尔结果。
        match: 链中第一个匹配的正则谓词产生的 :class:`re.Match`，或者为 ``None``。
            接受 ``match`` 参数的处理器将接收此值。
    """

    passed: bool
    match: re.Match[str] | None = None


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


class _Magic:
    """魔法 AST 节点的基类。

    子类通过重写 :meth:`evaluate` 来产生 ``(bool, match)`` 对。
    对于非正则节点，``match`` 为 ``None``；对于组合器，它是子节点中找到的第一个非 None 匹配。
    """

    __slots__ = ()

    # -- combinators -------------------------------------------------------

    def __and__(self, other: _Magic) -> _Magic:
        return _And(self, _coerce(other))

    def __rand__(self, other: _Magic) -> _Magic:
        return _And(_coerce(other), self)

    def __or__(self, other: _Magic) -> _Magic:
        return _Or(self, _coerce(other))

    def __ror__(self, other: _Magic) -> _Magic:
        return _Or(_coerce(other), self)

    def __invert__(self) -> _Magic:
        return _Not(self)

    # -- evaluation --------------------------------------------------------

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        raise NotImplementedError  # pragma: no cover

    def resolve(self, event: Any) -> FilterResult:
        passed, match = self.evaluate(event)
        return FilterResult(passed=passed, match=match)


def _coerce(obj: Any) -> _Magic:
    if isinstance(obj, _Magic):
        return obj
    return _Const(bool(obj))


# -- leaves -----------------------------------------------------------------


class _Const(_Magic):
    """常量布尔值（内部用于 ``True``/``False`` 字面量）。"""

    __slots__ = ("_value",)

    def __init__(self, value: bool) -> None:
        self._value = value

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        return self._value, None


class _Attr(_Magic):
    """根植于事件的属性访问链。

    ``F.text`` 产生 ``_Attr(("text",))``；``F.foo.bar`` 产生 ``_Attr(("foo", "bar"))``。
    解析值的真值是默认的布尔值结果；更丰富的谓词会将此包装在 :class:`_Cmp` 等类中。
    """

    __slots__ = ("_path",)

    def __init__(self, path: tuple[str, ...]) -> None:
        self._path = path

    # 允许进一步的属性链式访问。我们特意不对根对象 ``_MagicRoot`` 使用
    # ``__getattr__`` 来实现此功能 — 将链式访问类分开可以使类型签名更清晰。
    def __getattr__(self, name: str) -> _Attr:
        if name.startswith("_"):
            raise AttributeError(name)
        return _Attr((*self._path, name))

    def _get(self, event: Any) -> Any:
        cur: Any = event
        for part in self._path:
            try:
                cur = getattr(cur, part)
            except AttributeError:
                return _MISSING
        return cur

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        val = self._get(event)
        if val is _MISSING:
            return False, None
        return bool(val), None

    # -- predicate builders -----------------------------------------------

    def __eq__(self, other: object) -> _Magic:  # type: ignore[override]
        return _Cmp(self, "eq", other)

    def __ne__(self, other: object) -> _Magic:  # type: ignore[override]
        return _Cmp(self, "ne", other)

    def __lt__(self, other: object) -> _Magic:
        return _Cmp(self, "lt", other)

    def __le__(self, other: object) -> _Magic:
        return _Cmp(self, "le", other)

    def __gt__(self, other: object) -> _Magic:
        return _Cmp(self, "gt", other)

    def __ge__(self, other: object) -> _Magic:
        return _Cmp(self, "ge", other)

    # 保持此类可哈希，以便它可以根据需要存放在 set/dict 中。
    # 使用标识哈希 (identity hash) 即可 — 魔法节点不是值对象。
    def __hash__(self) -> int:
        return id(self)

    # -- string helpers ---------------------------------------------------

    def startswith(self, prefix: str) -> _Magic:
        return _StringPredicate(self, "startswith", prefix)

    def endswith(self, suffix: str) -> _Magic:
        return _StringPredicate(self, "endswith", suffix)

    def contains(self, needle: str) -> _Magic:
        return _StringPredicate(self, "contains", needle)

    def regex(self, pattern: str | re.Pattern[str], *, flags: int = 0) -> _Magic:
        compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, flags)
        return _Regex(self, compiled)

    def in_(self, container: object) -> _Magic:
        return _In(self, container)


# _Attr 使用的哨兵对象，用于在不抛出异常的情况下表示“属性未找到”。
# 与 ``None`` 不同，因为 ``None`` 可能是许多事件字段的合法值。
_MISSING: Final = object()


class _Cmp(_Magic):
    """二元比较：``F.x <op> literal``。"""

    __slots__ = ("_lhs", "_op", "_rhs")

    def __init__(self, lhs: _Attr, op: str, rhs: object) -> None:
        self._lhs = lhs
        self._op = op
        self._rhs = rhs

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        val = self._lhs._get(event)
        if val is _MISSING:
            return False, None
        try:
            if self._op == "eq":
                return val == self._rhs, None
            if self._op == "ne":
                return val != self._rhs, None
            if self._op == "lt":
                return val < self._rhs, None
            if self._op == "le":
                return val <= self._rhs, None
            if self._op == "gt":
                return val > self._rhs, None
            if self._op == "ge":
                return val >= self._rhs, None
        except TypeError:
            return False, None
        return False, None  # pragma: no cover


class _StringPredicate(_Magic):
    """``F.text.startswith("/")`` 及其同类谓词。

    在测试之前，总是通过 ``str(val)`` 将左侧值强制转换为 ``str``，
    这样比较数值字段时依然符合预期。
    """

    __slots__ = ("_arg", "_kind", "_lhs")

    def __init__(self, lhs: _Attr, kind: str, arg: str) -> None:
        self._lhs = lhs
        self._kind = kind
        self._arg = arg

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        val = self._lhs._get(event)
        if val is _MISSING:
            return False, None
        s = val if isinstance(val, str) else str(val)
        if self._kind == "startswith":
            return s.startswith(self._arg), None
        if self._kind == "endswith":
            return s.endswith(self._arg), None
        if self._kind == "contains":
            return self._arg in s, None
        return False, None  # pragma: no cover


class _Regex(_Magic):
    """``F.text.regex(...)`` — 产生用于处理器注入的匹配结果。"""

    __slots__ = ("_lhs", "_pattern")

    def __init__(self, lhs: _Attr, pattern: re.Pattern[str]) -> None:
        self._lhs = lhs
        self._pattern = pattern

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        val = self._lhs._get(event)
        if val is _MISSING or not isinstance(val, str):
            return False, None
        m = self._pattern.search(val)
        return (m is not None), m


class _In(_Magic):
    """``F.account_key.in_({"alice", "bob"})``。"""

    __slots__ = ("_container", "_lhs")

    def __init__(self, lhs: _Attr, container: object) -> None:
        self._lhs = lhs
        self._container = container

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        val = self._lhs._get(event)
        if val is _MISSING:
            return False, None
        try:
            return val in self._container, None  # type: ignore[operator]
        except TypeError:
            return False, None


# -- combinators ------------------------------------------------------------


class _And(_Magic):
    __slots__ = ("_left", "_right")

    def __init__(self, left: _Magic, right: _Magic) -> None:
        self._left = left
        self._right = right

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        lp, lm = self._left.evaluate(event)
        if not lp:
            return False, None
        rp, rm = self._right.evaluate(event)
        if not rp:
            return False, None
        return True, lm or rm


class _Or(_Magic):
    __slots__ = ("_left", "_right")

    def __init__(self, left: _Magic, right: _Magic) -> None:
        self._left = left
        self._right = right

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        lp, lm = self._left.evaluate(event)
        if lp:
            return True, lm
        rp, rm = self._right.evaluate(event)
        return rp, rm


class _Not(_Magic):
    __slots__ = ("_inner",)

    def __init__(self, inner: _Magic) -> None:
        self._inner = inner

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        passed, _ = self._inner.evaluate(event)
        return not passed, None


# ---------------------------------------------------------------------------
# Root: the public ``F`` singleton
# ---------------------------------------------------------------------------


class _MagicRoot:
    """单例根节点。``F.x`` 返回 ``_Attr(("x",))``。"""

    __slots__ = ()

    def __getattr__(self, name: str) -> _Attr:
        if name.startswith("_"):
            raise AttributeError(name)
        return _Attr((name,))

    def __repr__(self) -> str:
        return "F"


F: Final[_MagicRoot] = _MagicRoot()
