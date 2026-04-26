"""Magic filter ``F`` — aiogram-style declarative event predicates.

A filter is a thing the router evaluates against an event before
deciding whether to invoke a handler. FastMeow ships a tiny "magic"
DSL inspired by aiogram's :class:`MagicFilter`::

    from fastmeow import F

    @router.message(F.text == "ping")
    async def pong(msg, ctx): ...

    @router.message(F.is_group & F.text.startswith("/"))
    async def commands(msg, ctx): ...

    @router.message(F.text.regex(r"^hello (?P<name>\w+)"))
    async def greet(msg, ctx, match): ...

How it works
------------
``F`` is a singleton :class:`_MagicRoot`. Attribute access
(``F.text``), comparison (``== "ping"``), and binary-bool combinators
(``& | ~``) all return new :class:`_Magic` nodes that *describe* the
predicate without evaluating it. The dispatcher later calls
:meth:`_Magic.resolve(event)` to get a concrete :class:`bool` plus an
optional ``re.Match`` (so handlers can capture regex groups).

Design constraints
------------------
* No magic on user-defined types — everything operates on the public
  :class:`fastmeow.types.Event` family. If you need a richer test,
  pass a plain callable instead: ``@router.message(my_predicate)``.
* No reflection / inspect tricks. The magic AST is interpreted, not
  compiled. This trades a little speed for crystal-clear errors and
  picklability (relevant if we ever ship a worker mode).
* All comparisons are short-circuit safe: any branch that touches an
  attribute the event doesn't have evaluates to ``False``, never raises.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Union

__all__ = ["F", "Filter", "FilterResult"]


# A user-supplied filter can be either:
#   - a magic node (``F.text == "ping"``),
#   - a plain callable returning bool / awaitable bool,
#   - ``None`` (no filter).
Filter = Union["_Magic", Callable[..., bool], Callable[..., Awaitable[bool]]]


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Outcome of evaluating a filter chain against an event.

    ``passed`` is the boolean answer; ``match`` is the
    :class:`re.Match` produced by the *first* regex predicate in the
    chain that matched, or ``None``. Handlers that take a ``match``
    parameter receive this value.
    """

    passed: bool
    match: re.Match[str] | None = None


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


class _Magic:
    """Base class for magic AST nodes.

    Subclasses override :meth:`evaluate` to produce a ``(bool, match)``
    pair. ``match`` is ``None`` for non-regex nodes; for combinators
    it's the first non-None match found among children.
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
    """Constant boolean (used internally for ``True``/``False`` literals)."""

    __slots__ = ("_value",)

    def __init__(self, value: bool) -> None:
        self._value = value

    def evaluate(self, event: Any) -> tuple[bool, re.Match[str] | None]:
        return self._value, None


class _Attr(_Magic):
    """Attribute access chain rooted at the event.

    ``F.text`` produces ``_Attr(("text",))``; ``F.foo.bar`` produces
    ``_Attr(("foo", "bar"))``. Truthiness of the resolved value is the
    default boolean; richer predicates wrap this in :class:`_Cmp` etc.
    """

    __slots__ = ("_path",)

    def __init__(self, path: tuple[str, ...]) -> None:
        self._path = path

    # Allow further attribute chaining. We deliberately don't use
    # ``__getattr__`` on the root ``_MagicRoot`` for this — keeping
    # the chain class separate makes the type signatures cleaner.
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

    # Keep this class hashable so it can live in a set/dict if anyone
    # wants to. Identity hash is fine — magic nodes are not values.
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


# Sentinel used by _Attr to represent "attribute not found" without
# raising. Distinct from ``None`` because ``None`` is a legitimate
# value for many event fields.
_MISSING: Final = object()


class _Cmp(_Magic):
    """Binary comparison: ``F.x <op> literal``."""

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
    """``F.text.startswith("/")`` and friends.

    Always coerces the LHS to ``str`` via ``str(val)`` before testing,
    so comparing a numeric field still works predictably.
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
    """``F.text.regex(...)`` — produces a match for handler injection."""

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
    """``F.account_key.in_({"alice", "bob"})``."""

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
    """Singleton root. ``F.x`` returns ``_Attr(("x",))``."""

    __slots__ = ()

    def __getattr__(self, name: str) -> _Attr:
        if name.startswith("_"):
            raise AttributeError(name)
        return _Attr((name,))

    def __repr__(self) -> str:
        return "F"


F: Final[_MagicRoot] = _MagicRoot()
