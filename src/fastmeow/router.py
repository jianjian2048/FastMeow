"""Router — group handlers by event type and dispatch with filter
chains.

Inspired by aiogram's ``Router`` and FastStream's broker decorators.
A :class:`Router` is the user-facing handler registry::

    from fastmeow import Router, F

    router = Router()

    @router.message(F.text == "ping")
    async def pong(msg, ctx):
        await ctx.reply("pong")

    @router.connected()
    async def hello(ctx):
        log.info("account %s connected", ctx.account_key)

A :class:`fastmeow.FastMeow` app maintains a root router and lets the
user mount sub-routers via :meth:`Router.include_router` for code
organisation. Dispatch order is registration order; the first handler
whose filter chain returns ``passed=True`` *and* whose handler returns
without raising the special :class:`SkipHandler` sentinel wins.

Handler signature injection
---------------------------
A handler may take any combination of these four named parameters
(positional or keyword), in any order. Phase 1 whitelist:

* ``msg`` or ``event`` — the public event object
* ``ctx``              — the :class:`Ctx`
* ``qr``               — the :class:`QREvent` (only on QR handlers)
* ``match``            — ``re.Match`` from the first matching regex
                          filter, or ``None``

Any other parameter name (or an unrecognised parameter without a
default) raises :class:`HandlerSignatureError` at registration time —
fail-fast beats a confusing ``TypeError`` at first dispatch.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastmeow.context import Ctx
from fastmeow.exceptions import HandlerSignatureError
from fastmeow.filters import F as _F  # noqa: F401 — re-exported for type narrowing
from fastmeow.filters import Filter, FilterResult, _Magic
from fastmeow.types import (
    ConnectedEvent,
    DisconnectedEvent,
    Event,
    LoggedOutEvent,
    MessageEvent,
    PairSuccessEvent,
    QREvent,
    UnknownEvent,
)

log = logging.getLogger(__name__)

__all__ = ["Router", "SkipHandler"]


class SkipHandler(Exception):
    """Raise from inside a handler to indicate "this isn't actually for
    me, try the next one". Filters are the preferred way to skip; this
    is the escape hatch for cases where the decision needs the full
    handler context.
    """


# ---------------------------------------------------------------------------
# Internal handler record
# ---------------------------------------------------------------------------


# Python 3.13 supports type statements but we keep ``Callable`` aliases
# explicit for clarity in tracebacks.
HandlerFn = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ParamPlan:
    """Pre-computed plan for adapting a handler's signature.

    Built once at registration time so that the dispatcher does no
    reflection on the hot path.
    """

    accepts_event: bool
    """True if the handler has ``msg`` or ``event`` parameter."""

    event_param_name: str
    """The actual parameter name to bind the event to ('msg' or 'event').
    Empty if ``accepts_event`` is False."""

    accepts_ctx: bool
    accepts_qr: bool
    accepts_match: bool


@dataclass(frozen=True, slots=True)
class _Handler:
    fn: HandlerFn
    filters: tuple[Filter, ...]
    plan: _ParamPlan


@dataclass(slots=True)
class _HandlerSet:
    """Mutable collection of handlers for one event type."""

    items: list[_Handler] = field(default_factory=list)

    def add(self, h: _Handler) -> None:
        self.items.append(h)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Registers handlers and dispatches events to them.

    A user typically constructs one root :class:`Router`, decorates
    handlers on it, and passes it to :class:`fastmeow.FastMeow`. Larger
    applications can split handlers across multiple routers and combine
    them via :meth:`include_router`.
    """

    # One handler set per public event class. Using the class itself as
    # the dict key (rather than a string name) means dispatch is exact
    # and refactoring-safe.
    _SUPPORTED_EVENTS: tuple[type[Event], ...] = (
        MessageEvent,
        ConnectedEvent,
        DisconnectedEvent,
        QREvent,
        PairSuccessEvent,
        LoggedOutEvent,
        UnknownEvent,
    )

    def __init__(self, *, name: str | None = None) -> None:
        self.name = name or f"router-{id(self):x}"
        self._sets: dict[type[Event], _HandlerSet] = {
            cls: _HandlerSet() for cls in self._SUPPORTED_EVENTS
        }
        # Sub-routers are flattened at dispatch time so we don't pay
        # tree-walk overhead per event. Kept as a separate list for
        # diagnostics / repr.
        self._sub_routers: list[Router] = []

    # -- composition -------------------------------------------------------

    def include_router(self, other: Router) -> None:
        """Mount another router's handlers under this one.

        Handlers from ``other`` are appended to *this* router's sets at
        call time, so subsequent registrations on ``other`` are also
        visible. Cycles are rejected.
        """
        if other is self:
            raise ValueError("router cannot include itself")
        if self._would_create_cycle(other):
            raise ValueError(f"include_router would create a cycle: {self.name} <-> {other.name}")
        self._sub_routers.append(other)

    def _would_create_cycle(self, candidate: Router) -> bool:
        # DFS over candidate's transitive sub-routers looking for self.
        stack = [candidate]
        seen: set[int] = set()
        while stack:
            r = stack.pop()
            if id(r) in seen:
                continue
            seen.add(id(r))
            if r is self:
                return True
            stack.extend(r._sub_routers)
        return False

    # -- decorators --------------------------------------------------------

    def message(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        """Register a handler for :class:`MessageEvent`."""
        return self._make_decorator(MessageEvent, filters)

    def connected(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        return self._make_decorator(ConnectedEvent, filters)

    def disconnected(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        return self._make_decorator(DisconnectedEvent, filters)

    def qr(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        return self._make_decorator(QREvent, filters)

    def pair_success(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        return self._make_decorator(PairSuccessEvent, filters)

    def logged_out(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        return self._make_decorator(LoggedOutEvent, filters)

    def unknown(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        """Register a fallback handler for unrecognised events."""
        return self._make_decorator(UnknownEvent, filters)

    # -- registration internals -------------------------------------------

    def _make_decorator(
        self,
        event_cls: type[Event],
        filters: tuple[Filter, ...],
    ) -> Callable[[HandlerFn], HandlerFn]:
        def decorator(fn: HandlerFn) -> HandlerFn:
            if not asyncio.iscoroutinefunction(fn):
                raise HandlerSignatureError(
                    f"handler {fn.__qualname__} must be 'async def'; "
                    f"FastMeow handlers always run in the event loop."
                )
            plan = _build_param_plan(fn, event_cls)
            self._sets[event_cls].add(_Handler(fn=fn, filters=filters, plan=plan))
            return fn

        return decorator

    # -- dispatch ----------------------------------------------------------

    async def dispatch(self, event: Event, ctx: Ctx) -> None:
        """Run the first matching handler for ``event``.

        Iteration follows registration order. The first handler whose
        filter chain passes is invoked; the dispatch then stops. A
        handler may opt out by raising :class:`SkipHandler`, in which
        case iteration continues to the next candidate. Any other
        exception propagates to the caller (the dispatcher / app),
        which decides whether to log-and-continue or treat it as fatal.
        """
        cls = type(event)
        for handler in self._collect_for(cls):
            result = await _evaluate_filters(handler.filters, event)
            if not result.passed:
                continue
            try:
                await _invoke(handler, event, ctx, result.match)
            except SkipHandler:
                continue
            else:
                return

    def _collect_for(self, cls: type[Event]) -> list[_Handler]:
        own = self._sets.get(cls)
        if own is None and not self._sub_routers:
            return []
        out: list[_Handler] = list(own.items) if own else []
        for sub in self._sub_routers:
            out.extend(sub._collect_for(cls))
        return out

    def __repr__(self) -> str:
        counts = ", ".join(
            f"{cls.__name__}={len(s.items)}" for cls, s in self._sets.items() if s.items
        )
        return f"Router({self.name!r}{', ' + counts if counts else ''})"


# ---------------------------------------------------------------------------
# Filter evaluation
# ---------------------------------------------------------------------------


async def _evaluate_filters(
    filters: tuple[Filter, ...],
    event: Event,
) -> FilterResult:
    """Evaluate every filter; AND them together; collect first regex match."""
    if not filters:
        return FilterResult(passed=True, match=None)

    accumulated_match: re.Match[str] | None = None
    for f in filters:
        if isinstance(f, _Magic):
            res = f.resolve(event)
            if not res.passed:
                return FilterResult(passed=False)
            accumulated_match = accumulated_match or res.match
            continue
        # Plain callable: pass the event positionally. May be sync or async.
        out = f(event)
        if inspect.isawaitable(out):
            out = await out
        if not bool(out):
            return FilterResult(passed=False)
    return FilterResult(passed=True, match=accumulated_match)


# ---------------------------------------------------------------------------
# Signature inspection + invocation
# ---------------------------------------------------------------------------


_VALID_PARAM_NAMES = frozenset({"msg", "event", "ctx", "qr", "match"})


def _build_param_plan(fn: HandlerFn, event_cls: type[Event]) -> _ParamPlan:
    """Validate handler signature and produce a :class:`_ParamPlan`.

    Rules:
    * Only the names in :data:`_VALID_PARAM_NAMES` may appear without
      a default value.
    * A handler may take ``msg`` or ``event`` (not both); if it takes
      neither, it must take ``ctx`` (a no-arg handler is also allowed).
    * ``qr`` is only valid on QR handlers; we don't enforce this
      strictly because a user might genuinely use ``qr`` as a generic
      name and still expect the event injection — but we DO refuse
      ``qr`` for non-QR event classes to avoid surprising binds.
    """
    sig = inspect.signature(fn)
    accepts_event = False
    event_param_name = ""
    accepts_ctx = False
    accepts_qr = False
    accepts_match = False

    for pname, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            # *args / **kwargs are fine — the caller just won't fill them.
            continue
        if pname not in _VALID_PARAM_NAMES:
            if param.default is not inspect.Parameter.empty:
                # User-defined param with a default: we leave it alone.
                continue
            raise HandlerSignatureError(
                f"handler {fn.__qualname__} has unsupported parameter {pname!r}; "
                f"FastMeow only injects {sorted(_VALID_PARAM_NAMES)}. "
                f"Give the parameter a default value if you intend to bind "
                f"it yourself, or rename it."
            )
        if pname in ("msg", "event"):
            if accepts_event:
                raise HandlerSignatureError(
                    f"handler {fn.__qualname__} declares both 'msg' and 'event'; pick one."
                )
            accepts_event = True
            event_param_name = pname
        elif pname == "ctx":
            accepts_ctx = True
        elif pname == "qr":
            if event_cls is not QREvent:
                raise HandlerSignatureError(
                    f"handler {fn.__qualname__} declares 'qr' parameter but is "
                    f"registered for {event_cls.__name__}, not QREvent. "
                    f"Use 'event' (or 'msg') instead."
                )
            accepts_qr = True
        elif pname == "match":
            accepts_match = True

    return _ParamPlan(
        accepts_event=accepts_event,
        event_param_name=event_param_name,
        accepts_ctx=accepts_ctx,
        accepts_qr=accepts_qr,
        accepts_match=accepts_match,
    )


async def _invoke(
    handler: _Handler,
    event: Event,
    ctx: Ctx,
    match: re.Match[str] | None,
) -> None:
    plan = handler.plan
    kwargs: dict[str, Any] = {}
    if plan.accepts_event:
        kwargs[plan.event_param_name] = event
    if plan.accepts_ctx:
        kwargs["ctx"] = ctx
    if plan.accepts_qr:
        kwargs["qr"] = event  # safe by plan validation: only on QREvent handlers
    if plan.accepts_match:
        kwargs["match"] = match
    await handler.fn(**kwargs)
