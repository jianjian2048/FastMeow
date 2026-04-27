"""顶层 :class:`FastMeow` 应用。

这是大多数用户代码接触的公开入口点：:

    from fastmeow import FastMeow, Router

    router = Router()

    @router.message(F.text == "ping")
    async def pong(msg, ctx):
        await ctx.reply("pong")

    app = FastMeow(session_dir="./sessions")
    app.include_router(router)

    async def main():
        async with app:
            handle = app.add_account("alice", on_qr="terminal")
            await handle.ready()
            await app.run_forever()

``FastMeow`` 对象拥有三个协作组件：

* :class:`fastmeow._supervisor.Sidecar`  -- Go 子进程
* :class:`fastmeow._transport.Transport` -- gRPC 客户端
* :class:`fastmeow._dispatcher.Dispatcher` -- 将事件泵入路由器

它还拥有 :class:`fastmeow.manifest.Manifest`，以便账号 key 在重启后依然存在。

``add_account`` 是**同步返回，异步完成**的：它*同步*返回一个 :class:`AccountHandle`
（无需 ``await``），以便用户可以在设备开始配对之前附加 QR 回调，
然后 await ``handle.ready()`` 以阻塞直到账号达到 CONNECTED 状态（或失败）。
实际的 ``EnsureAccount`` / ``Connect`` gRPC 往返在由 :class:`FastMeow` 拥有的后台任务中运行；
它们的失败通过 :meth:`AccountHandle.ready` 暴露。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Literal

from . import _transport
from ._dispatcher import Dispatcher, ErrorHook, _BoundClient
from ._supervisor import Sidecar, SidecarConfig
from .context import AccountClient
from .exceptions import (
    AccountAlreadyExistsError,
    AccountError,
    AccountNotFoundError,
    PairingFailedError,
    PairingTimeoutError,
)
from .manifest import Manifest
from .router import Router
from .types import (
    AccountState,
    ConnectedEvent,
    DisconnectedEvent,
    Event,
    LoggedOutEvent,
    PairSuccessEvent,
    QREvent,
)

_log = logging.getLogger("fastmeow.app")


# ---------------------------------------------------------------------------
# AccountHandle
# ---------------------------------------------------------------------------


QRCallback = Callable[[QREvent], Awaitable[None]] | Callable[[QREvent], None]


class AccountHandle:
    """由 :meth:`FastMeow.add_account` 同步返回。

    允许用户代码等待配对/连接完成、附加额外的 QR 监听器，或在不重新查找的情况下查询当前状态。

    此句柄是事件驱动的：每当派发器看到 ``account_key`` 的事件时，:class:`FastMeow` 都会通知它。
    """

    def __init__(
        self,
        account_key: str,
        *,
        transport: _transport.Transport | None = None,
    ) -> None:
        self.account_key = account_key
        self._state: AccountState = AccountState.UNSPECIFIED
        self._jid: str = ""
        self._connected = asyncio.Event()
        self._terminal_failure: BaseException | None = None
        self._qr_subscribers: list[QRCallback] = []
        self._transport = transport

    # -- 公开 API --------------------------------------------------------

    @property
    def state(self) -> AccountState:
        return self._state

    @property
    def jid(self) -> str:
        return self._jid

    @property
    def client(self) -> AccountClient:
        """此账号的账号作用域 RPC 客户端。

        在处理器之外使用以发送消息或管理群组：
        ``await handle.client.send_text(jid, "hi")``、
        ``await handle.client.create_group("ops", participants=[...])``。

        当账号事件改变 JID 时，``handle.jid`` 更新但此客户端仍保持有效 ——
        每次属性访问都会重新构建一个绑定到当前 JID 的轻量级 :class:`_BoundClient`。

        Raises:
            RuntimeError: 如果在 :meth:`FastMeow.start` 之前访问。
        """
        if self._transport is None:
            raise RuntimeError(
                f"AccountHandle for {self.account_key!r} is not bound to a "
                f"running FastMeow app yet; call app.start() (or use the async "
                f"context manager) before app.add_account()."
            )
        return _BoundClient(
            _transport=self._transport,
            account_key=self.account_key,
            jid=self._jid,
        )

    def on_qr(self, callback: QRCallback) -> None:
        """除了传递给 :meth:`FastMeow.add_account` 的观察器外，再注册一个额外的 QR 观察器。"""
        self._qr_subscribers.append(callback)

    async def ready(self, timeout: float | None = None) -> None:
        """阻塞直到账号达到 CONNECTED 状态。

        Raises:
            PairingTimeoutError: 如果 ``timeout`` 首先超时。
            PairingFailedError: 如果账号最终处于 LOGGED_OUT 状态或在连接前失败。
        """
        if self._connected.is_set():
            if self._terminal_failure is not None:
                raise self._terminal_failure
            return
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise PairingTimeoutError(
                f"account {self.account_key!r} not connected after {timeout}s"
            ) from exc
        if self._terminal_failure is not None:
            raise self._terminal_failure

    # -- 内部：状态机 ------------------------------------------

    # 终态永远不会离开；CONNECTED 仅会转为随后的 DISCONNECTED/LOGGED_OUT（自动重连或远程登出）。
    _TERMINAL_STATES: frozenset[AccountState] = frozenset({AccountState.LOGGED_OUT})

    def _apply_state(self, state: AccountState, jid: str = "") -> None:
        """RPC 结果和事件共同使用的幂等状态转换。

        规则：
          * 永远不离开终态（目前仅有 LOGGED_OUT）。
          * 永远不将 CONNECTED 降级回 PAIRING / CONNECTING。
          * 设置 state == CONNECTED 会唤醒任何 :meth:`ready` 等待者。
          * 设置 state == LOGGED_OUT 会标记致命失败，并唤醒等待者以便其观察异常。
          * 仅当 ``jid`` 非空时才采用它。
        """
        if self._state in self._TERMINAL_STATES:
            return
        if jid:
            self._jid = jid
        # Forbid CONNECTED -> {PAIRING, CONNECTING} regressions; allow
        # CONNECTED -> {DISCONNECTED, LOGGED_OUT}.
        if self._state is AccountState.CONNECTED and state in (
            AccountState.PAIRING,
            AccountState.CONNECTING,
            AccountState.UNSPECIFIED,
        ):
            return
        self._state = state
        if state is AccountState.CONNECTED:
            self._connected.set()
        elif state is AccountState.LOGGED_OUT:
            if self._terminal_failure is None:
                self._terminal_failure = PairingFailedError(
                    f"account {self.account_key!r} logged out"
                )
            # Wake any awaiter so they can observe the failure.
            self._connected.set()

    def _fail(self, exc: BaseException) -> None:
        """将句柄标记为失败并取消 :meth:`ready` 的阻塞。"""
        if self._terminal_failure is None:
            self._terminal_failure = exc
        self._connected.set()

    # -- 内部：由 FastMeow 事件钩子调用 --------------------------

    async def _on_event(self, event: object) -> None:
        if isinstance(event, ConnectedEvent):
            self._apply_state(AccountState.CONNECTED, jid=event.account_jid)
        elif isinstance(event, DisconnectedEvent):
            # 不要唤醒 ready()：断开连接的已配对账号通常会自动重连并再次发出 ConnectedEvent。
            # 配对前的断开无需关注（QR 重试将继续）。
            if self._state is AccountState.CONNECTED:
                self._state = AccountState.DISCONNECTED
        elif isinstance(event, PairSuccessEvent):
            # JID 持久化在 FastMeow 层处理（manifest）。
            # 此处仅更新句柄的视图；状态保持为 PAIRING，直到随后的 ConnectedEvent。
            if event.jid:
                self._jid = event.jid
        elif isinstance(event, LoggedOutEvent):
            self._terminal_failure = PairingFailedError(
                f"account {self.account_key!r} logged out: {event.reason}"
            )
            self._apply_state(AccountState.LOGGED_OUT)
        elif isinstance(event, QREvent):
            self._apply_state(AccountState.PAIRING)
            for cb in list(self._qr_subscribers):
                try:
                    res = cb(event)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    _log.exception("QR callback failed for %s", self.account_key)


# ---------------------------------------------------------------------------
# FastMeow 应用
# ---------------------------------------------------------------------------


class FastMeow:
    """单进程 WhatsApp 自动化应用。

    Args:
        session_dir: 存储 sqlite 会话和 manifest 的目录。
        sidecar_config: 覆盖默认的监督器配置。
        on_error: 处理器异常的异步错误钩子；默认为 ``logging.exception``。

    作为异步上下文管理器使用，以保证子进程的拆除：:

        async with FastMeow(session_dir="./sessions") as app:
            ...
    """

    def __init__(
        self,
        *,
        session_dir: Path | str,
        sidecar_config: SidecarConfig | None = None,
        on_error: ErrorHook | None = None,
    ) -> None:
        self._session_dir = Path(session_dir).resolve()
        self._on_error = on_error

        if sidecar_config is None:
            sidecar_config = SidecarConfig(session_dir=self._session_dir)
        else:
            # Honor user override but force session_dir alignment.
            sidecar_config.session_dir = self._session_dir
        self._sidecar_config = sidecar_config

        self._router = Router(name="root")
        self._sidecar: Sidecar | None = None
        self._transport: _transport.Transport | None = None
        self._dispatcher: Dispatcher | None = None
        self._manifest: Manifest | None = None
        self._handles: dict[str, AccountHandle] = {}
        self._account_tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._stopped = False

    # -- 路由 -----------------------------------------------------------

    def include_router(self, router: Router) -> None:
        """在应用的根路由器下挂载一个 :class:`Router`。

        转发至 :meth:`Router.include_router`，因此循环检测和命名行为与用户构建的路由器一致。
        """
        self._router.include_router(router)

    # -- 异步上下文管理器 --------------------------------------------

    async def __aenter__(self) -> FastMeow:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """启动 sidecar，打开 gRPC 通道，开始派发。

        资源获取是**原子性**的：如果任何步骤抛出异常（sidecar 启动失败、gRPC 握手超时、
        派发器无法启动），在原始异常传播之前，每个先前获取的资源都会被拆除。
        实例将保持在其 :meth:`start` 之前的状态，并可以重试。
        """
        if self._started:
            raise RuntimeError("FastMeow already started")

        manifest: Manifest | None = None
        sidecar: Sidecar | None = None
        transport: _transport.Transport | None = None
        dispatcher: Dispatcher | None = None
        try:
            manifest = await Manifest.open(self._session_dir)

            sidecar = Sidecar(self._sidecar_config)
            await sidecar.start()
            addr = await sidecar.wait_ready()
            _log.info("sidecar listening at %s", addr)

            transport = await _transport.connect(addr)
            _log.info(
                "handshake OK: proto=%d sidecar=%s whatsmeow=%s id=%s",
                transport.protocol_version,
                transport.sidecar_version,
                transport.whatsmeow_version,
                transport.sidecar_id,
            )

            dispatcher = Dispatcher(
                transport,
                self._router,
                on_error=self._on_error,
                on_event=self._notify_handle,
            )
            await dispatcher.start()
        except BaseException:
            # Reverse-order teardown of whatever we managed to bring up.
            if dispatcher is not None:
                try:
                    await dispatcher.stop()
                except Exception:
                    _log.exception("rollback: dispatcher.stop() failed")
            if transport is not None:
                try:
                    await transport.shutdown()
                except Exception:
                    _log.debug("rollback: transport.shutdown() raised", exc_info=True)
                try:
                    await transport.close()
                except Exception:
                    _log.exception("rollback: transport.close() failed")
            if sidecar is not None:
                try:
                    await sidecar.stop()
                except Exception:
                    _log.exception("rollback: sidecar.stop() failed")
            if manifest is not None:
                try:
                    await manifest.close()
                except Exception:
                    _log.exception("rollback: manifest.close() failed")
            raise

        # Commit: publish refs and flip the started flag last so a
        # racing observer never sees half-initialized state.
        self._manifest = manifest
        self._sidecar = sidecar
        self._transport = transport
        self._dispatcher = dispatcher
        self._started = True

    async def stop(self) -> None:
        """协作式地关闭派发器、传输层和 sidecar。"""
        if self._stopped:
            return
        self._stopped = True

        # 首先取消任何正在进行的账号引导任务，以免它们与传输层关闭竞争。
        pending = [t for t in self._account_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._account_tasks.clear()

        if self._dispatcher is not None:
            await self._dispatcher.stop()
        if self._transport is not None:
            try:
                await self._transport.shutdown()
            except Exception:
                _log.debug("transport.shutdown() raised", exc_info=True)
            await self._transport.close()
        if self._sidecar is not None:
            await self._sidecar.stop()
        if self._manifest is not None:
            await self._manifest.close()

    async def run_forever(self) -> None:
        """阻塞直到派发器流结束或 :meth:`stop` 运行。"""
        if self._dispatcher is None:
            raise RuntimeError("FastMeow not started")
        await self._dispatcher.run_until_stopped()

    # -- 账号管理 ------------------------------------------------

    def add_account(
        self,
        account_key: str,
        *,
        display_name: str = "",
        jid: str | None = None,
        on_qr: QRCallback | Literal["terminal"] | None = None,
    ) -> AccountHandle:
        """注册并启动一个账号。

        **同步返回，异步完成。** 此调用立即返回一个 :class:`AccountHandle`，
        在进行任何 gRPC 往返之前。句柄的 :meth:`AccountHandle.ready` 会阻塞，
        直到配对和连接完成（或失败）。

        实际的 ``EnsureAccount`` / ``Connect`` 调用在此 :class:`FastMeow` 拥有的后台任务中运行。
        引导过程中的失败会作为从 :meth:`AccountHandle.ready` 抛出的异常暴露出来。

        Args:
            account_key: 稳定的用户选择 ID。
            display_name: 可选的 WhatsApp 显示名称（新配对）。
            jid: 预先存在的要加载的 JID。``None`` 表示查询 manifest 清单；
                如果 manifest 清单中没有条目，则创建一个新设备（需要二维码配对）。
            on_qr: 接收每个 :class:`QREvent` 的异步/同步 callable，
                或者字面量字符串 ``"terminal"`` 以使用内置的终端渲染器。

        Raises:
            RuntimeError: 如果应用尚未 :meth:`start`。
            AccountAlreadyExistsError: 如果 ``account_key`` 已在此 :class:`FastMeow` 实例中注册。
        """
        if not self._started or self._transport is None or self._manifest is None:
            raise RuntimeError("FastMeow not started; call start() or use 'async with'")

        if account_key in self._handles:
            raise AccountAlreadyExistsError(account_key)

        handle = AccountHandle(account_key, transport=self._transport)
        if on_qr is not None:
            handle.on_qr(_resolve_qr_callback(on_qr))
        self._handles[account_key] = handle

        # Schedule the bootstrap. Track the task so stop() can cancel
        # in-flight bootstraps cleanly.
        task = asyncio.create_task(
            self._bootstrap_account(handle, display_name=display_name, jid=jid),
            name=f"fastmeow-bootstrap-{account_key}",
        )
        self._account_tasks.add(task)
        task.add_done_callback(self._account_tasks.discard)

        return handle

    async def _bootstrap_account(
        self,
        handle: AccountHandle,
        *,
        display_name: str,
        jid: str | None,
    ) -> None:
        """为新添加的账号运行 EnsureAccount + Connect。

        任何异常都会捕获到 ``handle._terminal_failure`` 中，
        以便 :meth:`AccountHandle.ready` 将其抛出；我们不会将其传播到 asyncio 任务层
        （否则任务在垃圾回收时会记录未处理异常警告）。
        """
        # _started + transport + manifest 由 add_account 保证。
        assert self._transport is not None
        assert self._manifest is not None
        account_key = handle.account_key

        try:
            # 解析有效 JID：显式参数 > manifest 清单 > "" (新建)。
            effective_jid = jid
            if effective_jid is None:
                existing = self._manifest.get(account_key)
                effective_jid = existing.jid if existing is not None else ""

            # 在与 sidecar 通信之前持久化意图，这样即使调用中途崩溃，manifest 清单也能保持一致。
            await self._manifest.register(account_key, jid=effective_jid)

            state, _created = await self._transport.ensure_account(
                account_key=account_key,
                display_name=display_name,
                jid=effective_jid,
            )
            handle._apply_state(state.state, jid=state.jid)
            if state.jid and state.jid != effective_jid:
                await self._manifest.update_jid(account_key, state.jid)

            # 在 EnsureAccount 之后连接；这将触发未配对设备的二维码生成和已配对设备的重新连接。
            connected_state = await self._transport.connect(account_key)
            handle._apply_state(connected_state.state, jid=connected_state.jid)
            if connected_state.jid and connected_state.jid != state.jid:
                await self._manifest.update_jid(account_key, connected_state.jid)
        except asyncio.CancelledError:
            # stop() 正在拆除我们；将其作为失败暴露出来，以便任何 ready() 等待者被唤醒而不是永远挂起。
            handle._fail(
                PairingFailedError(f"account {account_key!r} bootstrap cancelled (app stopping)")
            )
            raise
        except BaseException as exc:
            _log.exception("bootstrap failed for account %r", account_key)
            handle._fail(exc)
            # 丢弃现在不可用的句柄，以便用户可以重试 add_account。
            self._handles.pop(account_key, None)

    async def remove_account(self, account_key: str, *, logout: bool = False) -> None:
        """断开（或登出）账号并在本地遗忘它。"""
        if self._transport is None or self._manifest is None:
            raise RuntimeError("FastMeow not started")
        if account_key not in self._handles:
            raise AccountNotFoundError(account_key)

        if logout:
            await self._transport.logout(account_key)
        else:
            await self._transport.disconnect(account_key)
        await self._manifest.remove(account_key)
        del self._handles[account_key]

    def get_handle(self, account_key: str) -> AccountHandle:
        """返回之前为 ``account_key`` 创建的句柄。"""
        h = self._handles.get(account_key)
        if h is None:
            raise AccountNotFoundError(account_key)
        return h

    @property
    def accounts(self) -> dict[str, AccountHandle]:
        """当前注册句柄的快照。"""
        return dict(self._handles)

    @property
    def router(self) -> Router:
        """根路由器；用户代码通常通过 :meth:`include_router` 挂载子路由器，而不是直接操作此属性。"""
        return self._router

    @property
    def transport(self) -> _transport.Transport:
        """底层的类型化 gRPC 客户端。为高级用户提供的逃生口。"""
        if self._transport is None:
            raise RuntimeError("FastMeow not started")
        return self._transport

    # -- 内部钩子 ----------------------------------------------------

    async def _notify_handle(self, event: Event) -> None:
        """将事件转发到其对应账号的 :class:`AccountHandle`。

        同时在 :class:`PairSuccessEvent` 上持久化新分配的 JID，
        以便进程重启后无需重新配对即可重新连接。
        manifest 清单写入是尽力而为的：任何错误都会被记录但永远不会抛出，
        因为下一个 :class:`ConnectedEvent`（它通过 ``account_jid`` 携带相同的 JID）将通过引导路径重试持久化。
        """
        handle = self._handles.get(event.account_key)
        if handle is None:
            return
        await handle._on_event(event)

        if isinstance(event, PairSuccessEvent) and event.jid and self._manifest is not None:
            try:
                await self._manifest.update_jid(event.account_key, event.jid)
            except Exception:
                _log.exception("failed to persist paired JID for %s", event.account_key)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _resolve_qr_callback(spec: QRCallback | Literal["terminal"]) -> QRCallback:
    """将 ``on_qr=`` 简写形式转换为 callable。"""
    if spec == "terminal":

        def render(qr: QREvent) -> None:
            print(qr.render_terminal())

        return render
    return spec


__all__ = ["AccountHandle", "FastMeow"]


# 重新导出以便用户可以根据需要执行 `from fastmeow import AccountError`；
# manifest 清单层也会抛出 ManifestError，均保留在 `exceptions` 中。
_ = AccountError
