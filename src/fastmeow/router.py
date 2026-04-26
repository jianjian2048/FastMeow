"""路由器 — 按事件类型对处理器进行分组，并通过过滤器链进行派发。

受 aiogram 的 ``Router`` 和 FastStream 的 broker 装饰器启发。
:class:`Router` 是面向用户的处理器注册表：:

    from fastmeow import Router, F

    router = Router()

    @router.message(F.text == "ping")
    async def pong(msg, ctx):
        await ctx.reply("pong")

    @router.connected()
    async def hello(ctx):
        log.info("account %s connected", ctx.account_key)

:class:`fastmeow.FastMeow` 应用维护一个根路由器，并允许用户通过
:meth:`Router.include_router` 挂载子路由器以组织代码。
派发顺序即注册顺序；第一个过滤器链返回 ``passed=True`` 且处理器返回时
未抛出特殊的 :class:`SkipHandler` 哨兵异常的处理器胜出。

处理器签名注入
---------------------------
处理器可以采用这四个命名参数（位置参数或关键字参数）的任意组合，顺序不限。
Phase 1 白名单：

* ``msg`` 或 ``event`` — 公开事件对象
* ``ctx``              — :class:`Ctx`
* ``qr``               — :class:`QREvent`（仅限 QR 处理器）
* ``match``            — 第一个匹配的正则过滤器的 ``re.Match``，或者为 ``None``

任何其他参数名称（或没有默认值的未识别参数）都会在注册时抛出
:class:`HandlerSignatureError` — 尽早失败优于在首次派发时抛出令人困惑的 ``TypeError``。
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
    """在处理器内部抛出此异常，以表示“这实际上不是给我的，请尝试下一个”。
    过滤器是跳过处理的首选方式；此异常是为决策需要完整的处理器上下文的情况提供的逃生口。
    """


# ---------------------------------------------------------------------------
# 内部处理器记录
# ---------------------------------------------------------------------------


# Python 3.13 支持 type 语句，但为了回溯 (tracebacks) 清晰，
# 我们保持 ``Callable`` 别名显式定义。
HandlerFn = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ParamPlan:
    """预先计算的用于适配处理器签名的计划。

    在注册时构建一次，以便派发器在热路径 (hot path) 上不进行反射操作。
    """

    accepts_event: bool
    """如果处理器有 ``msg`` 或 ``event`` 参数，则为 True。"""

    event_param_name: str
    """绑定事件的实际参数名称（'msg' 或 'event'）。
    如果 ``accepts_event`` 为 False，则为空字符串。"""

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
    """单个事件类型的处理器可变集合。"""

    items: list[_Handler] = field(default_factory=list)

    def add(self, h: _Handler) -> None:
        self.items.append(h)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """注册处理器并将事件派发给它们。

    用户通常构造一个根 :class:`Router`，在其上装饰处理器，并将其传递给
    :class:`fastmeow.FastMeow`。较大的应用可以将处理器拆分到多个路由器中，
    并通过 :meth:`include_router` 将其组合。
    """

    # 每个公开事件类对应一个处理器集合。使用类本身作为 dict 的键
    # （而不是字符串名称）意味着派发是精确且重构安全的。
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
        # 子路由器在派发时被展平，因此我们不会为每个事件支付树遍历开销。
        # 作为一个单独的列表保留，用于诊断/展示。
        self._sub_routers: list[Router] = []

    # -- 组合 -------------------------------------------------------

    def include_router(self, other: Router) -> None:
        """将另一个路由器的处理器挂载到此路由器下。

        ``other`` 中的处理器在调用时会被追加到*此*路由器的集合中，
        因此随后在 ``other`` 上的注册也是可见的。拒绝循环包含。
        """
        if other is self:
            raise ValueError("router cannot include itself")
        if self._would_create_cycle(other):
            raise ValueError(f"include_router would create a cycle: {self.name} <-> {other.name}")
        self._sub_routers.append(other)

    def _would_create_cycle(self, candidate: Router) -> bool:
        # 对候选项的传递子路由器进行 DFS，寻找 self。
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

    # -- 装饰器 --------------------------------------------------------

    def message(self, *filters: Filter) -> Callable[[HandlerFn], HandlerFn]:
        """为 :class:`MessageEvent` 注册处理器。"""
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
        """为未识别的事件注册兜底处理器。"""
        return self._make_decorator(UnknownEvent, filters)

    # -- 注册内部逻辑 -------------------------------------------

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

    # -- 派发 ----------------------------------------------------------

    async def dispatch(self, event: Event, ctx: Ctx) -> None:
        """运行第一个匹配 ``event`` 的处理器。

        迭代遵循注册顺序。调用第一个通过过滤器链的处理器；随后停止派发。
        处理器可以通过抛出 :class:`SkipHandler` 来选择退出处理，
        在这种情况下迭代将继续到下一个候选项。
        任何其他异常都会传播给调用者（派发器 / 应用），
        由其决定是记录并继续还是视为致命错误。
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
# 过滤器评估
# ---------------------------------------------------------------------------


async def _evaluate_filters(
    filters: tuple[Filter, ...],
    event: Event,
) -> FilterResult:
    """评估每个过滤器；对它们进行“与”操作；收集第一个正则匹配结果。"""
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
        # 普通 callable：按位置传递事件。可以是同步或异步的。
        out = f(event)
        if inspect.isawaitable(out):
            out = await out
        if not bool(out):
            return FilterResult(passed=False)
    return FilterResult(passed=True, match=accumulated_match)


# ---------------------------------------------------------------------------
# 签名检查 + 调用
# ---------------------------------------------------------------------------


_VALID_PARAM_NAMES = frozenset({"msg", "event", "ctx", "qr", "match"})


def _build_param_plan(fn: HandlerFn, event_cls: type[Event]) -> _ParamPlan:
    """验证处理器签名并生成 :class:`_ParamPlan`。

    规则：
    * 只有 :data:`_VALID_PARAM_NAMES` 中的名称可以出现在没有默认值的参数中。
    * 处理器可以采用 ``msg`` 或 ``event``（不能同时采用）；如果两者都不采用，
      则必须采用 ``ctx``（也允许无参数处理器）。
    * ``qr`` 仅在 QR 处理器上有效；我们不严格强制执行此规则，因为用户可能确实将
      ``qr`` 用作泛型名称并仍期望事件注入 — 但我们确实拒绝为非 QR 事件类使用
      ``qr``，以避免令人惊讶的绑定。
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
