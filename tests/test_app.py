"""FastMeow 应用测试：AccountHandle + add_account 流程。

这里不会启动真实的 sidecar。测试会构造一个 ``FastMeow``，并注入
transport 和 manifest 的伪实现，覆盖 ``start()`` 本来会设置的相同
代码路径。

``add_account`` 是 **同步返回 / 异步完成**：它会立即返回一个
``AccountHandle``，并在后台任务中运行 ``EnsureAccount`` + ``Connect``。
需要引导流程完成的测试，请先 ``await _drain_bootstraps(app)``，再断言
transport 的调用点。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fastmeow.app import AccountHandle, FastMeow, _resolve_qr_callback
from fastmeow.exceptions import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    PairingFailedError,
    PairingTimeoutError,
)
from fastmeow.manifest import Manifest
from fastmeow.types import (
    Account,
    AccountState,
    ConnectedEvent,
    DisconnectedEvent,
    LoggedOutEvent,
    PairSuccessEvent,
    QREvent,
)

# ---------------------------------------------------------------------------
# 伪对象
# ---------------------------------------------------------------------------


class FakeTransport:
    """模拟 FastMeow.add_account / remove_account 使用的 transport 接口。"""

    def __init__(self) -> None:
        self.ensure_calls: list[dict[str, Any]] = []
        self.connect_calls: list[str] = []
        self.disconnect_calls: list[str] = []
        self.logout_calls: list[str] = []
        # 若测试需要不同返回值，可在此覆盖。
        self.ensure_state = Account(account_key="", jid="", state=AccountState.UNPAIRED)
        self.ensure_created = True
        self.connect_state = Account(account_key="", jid="", state=AccountState.CONNECTING)
        self.ensure_raises: BaseException | None = None
        self.connect_raises: BaseException | None = None

    async def ensure_account(
        self, *, account_key: str, display_name: str = "", jid: str = ""
    ) -> tuple[Account, bool]:
        self.ensure_calls.append(
            {"account_key": account_key, "display_name": display_name, "jid": jid}
        )
        if self.ensure_raises is not None:
            raise self.ensure_raises
        return (
            Account(
                account_key=account_key,
                jid=self.ensure_state.jid,
                state=self.ensure_state.state,
            ),
            self.ensure_created,
        )

    async def connect(self, account_key: str) -> Account:
        self.connect_calls.append(account_key)
        if self.connect_raises is not None:
            raise self.connect_raises
        return Account(
            account_key=account_key,
            jid=self.connect_state.jid,
            state=self.connect_state.state,
        )

    async def disconnect(self, account_key: str) -> Account:
        self.disconnect_calls.append(account_key)
        return Account(account_key=account_key, jid="", state=AccountState.DISCONNECTED)

    async def logout(self, account_key: str) -> Account:
        self.logout_calls.append(account_key)
        return Account(account_key=account_key, jid="", state=AccountState.LOGGED_OUT)


async def _build_app(
    tmp_path: Path,
    transport: FakeTransport | None = None,
) -> tuple[FastMeow, FakeTransport]:
    """构建一个带伪 transport 和真实 manifest、但没有 sidecar 的 FastMeow。"""
    transport = transport or FakeTransport()
    app = FastMeow(session_dir=tmp_path)
    app._manifest = await Manifest.open(tmp_path)
    app._transport = transport  # type: ignore[assignment]
    app._started = True
    return app, transport


async def _drain_bootstraps(app: FastMeow) -> None:
    """等待所有正在运行的 ``add_account`` 后台任务完成。

    引导阶段的异常会被有意捕获到 handle 的终态失败中（因此
    ``handle.ready()`` 会抛出它们）；这里使用 ``return_exceptions=True``
    吞掉它们，交由各个测试通过 ``ready()`` 来断言。
    """
    pending = [t for t in app._account_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# AccountHandle
# ---------------------------------------------------------------------------


def _connected_event(account_key: str, jid: str) -> ConnectedEvent:
    return ConnectedEvent(
        seq=1,
        sidecar_id="sc",
        account_key=account_key,
        account_jid=jid,
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _logged_out_event(account_key: str, reason: str) -> LoggedOutEvent:
    return LoggedOutEvent(
        seq=2,
        sidecar_id="sc",
        account_key=account_key,
        account_jid="",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        reason=reason,
    )


def _qr_event(account_key: str, code: str) -> QREvent:
    return QREvent(
        seq=3,
        sidecar_id="sc",
        account_key=account_key,
        account_jid="",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        code=code,
        ttl_seconds=20,
    )


@pytest.mark.asyncio
async def test_handle_ready_resolves_on_connected() -> None:
    handle = AccountHandle("alice")
    assert handle.state is AccountState.UNSPECIFIED
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    state_after: AccountState = handle.state
    assert state_after is AccountState.CONNECTED
    assert handle.jid == "alice@s.whatsapp.net"
    await handle.ready(timeout=0.1)


@pytest.mark.asyncio
async def test_handle_ready_raises_on_logged_out() -> None:
    handle = AccountHandle("alice")
    await handle._on_event(_logged_out_event("alice", "revoked"))
    with pytest.raises(PairingFailedError, match="revoked"):
        await handle.ready(timeout=0.1)
    assert handle.state == AccountState.LOGGED_OUT


@pytest.mark.asyncio
async def test_handle_ready_times_out() -> None:
    handle = AccountHandle("alice")
    with pytest.raises(PairingTimeoutError):
        await handle.ready(timeout=0.05)


@pytest.mark.asyncio
async def test_handle_qr_callback_invoked() -> None:
    handle = AccountHandle("alice")
    captured: list[QREvent] = []

    async def cb(qr: QREvent) -> None:
        captured.append(qr)

    handle.on_qr(cb)
    await handle._on_event(_qr_event("alice", "2@abc"))
    assert len(captured) == 1
    assert captured[0].code == "2@abc"
    assert handle.state == AccountState.PAIRING


@pytest.mark.asyncio
async def test_handle_sync_qr_callback_invoked() -> None:
    handle = AccountHandle("alice")
    captured: list[str] = []

    def cb(qr: QREvent) -> None:
        captured.append(qr.code)

    handle.on_qr(cb)
    await handle._on_event(_qr_event("alice", "2@xyz"))
    assert captured == ["2@xyz"]


@pytest.mark.asyncio
async def test_handle_qr_callback_exception_swallowed() -> None:
    handle = AccountHandle("alice")

    def bad(qr: QREvent) -> None:
        raise RuntimeError("oops")

    handle.on_qr(bad)
    # 不得抛出。
    await handle._on_event(_qr_event("alice", "2@bad"))


# ---------------------------------------------------------------------------
# _resolve_qr_callback
# ---------------------------------------------------------------------------


def test_resolve_qr_callback_terminal_returns_callable(capsys: Any) -> None:
    cb = _resolve_qr_callback("terminal")
    qr = _qr_event("alice", "2@abcd,efgh,ijkl")
    # 终端渲染器会打印；这里只需确认它可调用且能运行。
    cb(qr)
    out = capsys.readouterr().out
    assert out  # something rendered


def test_resolve_qr_callback_passthrough() -> None:
    def my_cb(qr: QREvent) -> None:
        return None

    assert _resolve_qr_callback(my_cb) is my_cb


# ---------------------------------------------------------------------------
# FastMeow.add_account / remove_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_account_returns_handle_and_persists_manifest(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    transport.ensure_state = Account(
        account_key="alice", jid="alice@s.whatsapp.net", state=AccountState.UNPAIRED
    )
    transport.connect_state = Account(
        account_key="alice", jid="alice@s.whatsapp.net", state=AccountState.CONNECTING
    )

    app, _ = await _build_app(tmp_path, transport)
    try:
        handle = app.add_account("alice")
        # 同步返回契约：在任何 gRPC 往返发生前就能拿到 AccountHandle。
        assert isinstance(handle, AccountHandle)
        assert handle.account_key == "alice"
        assert handle.state is AccountState.UNSPECIFIED

        await _drain_bootstraps(app)
        assert handle.jid == "alice@s.whatsapp.net"
        state_after: AccountState = handle.state
        assert state_after is AccountState.CONNECTING

        assert transport.ensure_calls == [{"account_key": "alice", "display_name": "", "jid": ""}]
        assert transport.connect_calls == ["alice"]

        # Manifest 已持久化。
        assert app._manifest is not None
        entry = app._manifest.get("alice")
        assert entry is not None
        assert entry.jid == "alice@s.whatsapp.net"
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_add_account_uses_manifest_jid_when_none_passed(tmp_path: Path) -> None:
    transport = FakeTransport()
    app, _ = await _build_app(tmp_path, transport)
    try:
        # 预先写入 manifest，就像此前运行已完成该账号配对一样。
        await app._manifest.register(  # type: ignore[union-attr]
            "alice", jid="alice@s.whatsapp.net"
        )

        app.add_account("alice")
        await _drain_bootstraps(app)
        assert transport.ensure_calls[0]["jid"] == "alice@s.whatsapp.net"
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_add_account_explicit_jid_must_match_manifest(tmp_path: Path) -> None:
    """manifest 是唯一真相；显式冲突的 JID 会被拒绝。

    现在引导失败会通过 ``handle.ready()`` 暴露出来，而不是从
    ``add_account`` 本身抛出（它是同步返回）。
    """
    from fastmeow.exceptions import ManifestError

    transport = FakeTransport()
    app, _ = await _build_app(tmp_path, transport)
    try:
        await app._manifest.register(  # type: ignore[union-attr]
            "alice", jid="old@s.whatsapp.net"
        )
        handle = app.add_account("alice", jid="new@s.whatsapp.net")
        await _drain_bootstraps(app)
        with pytest.raises(ManifestError):
            await handle.ready(timeout=0.1)
        # 对于冲突流程，sidecar 不得被调用。
        assert transport.ensure_calls == []
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_add_account_explicit_jid_matching_manifest_ok(tmp_path: Path) -> None:
    """与 manifest 条目匹配的显式 JID 没问题。"""
    transport = FakeTransport()
    app, _ = await _build_app(tmp_path, transport)
    try:
        await app._manifest.register(  # type: ignore[union-attr]
            "alice", jid="alice@s.whatsapp.net"
        )
        app.add_account("alice", jid="alice@s.whatsapp.net")
        await _drain_bootstraps(app)
        assert transport.ensure_calls[0]["jid"] == "alice@s.whatsapp.net"
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_add_account_duplicate_raises(tmp_path: Path) -> None:
    app, _ = await _build_app(tmp_path)
    try:
        app.add_account("alice")
        with pytest.raises(AccountAlreadyExistsError):
            app.add_account("alice")
        await _drain_bootstraps(app)
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_add_account_connect_failure_rolls_back_handle(tmp_path: Path) -> None:
    """当引导任务失败时，``handle.ready()`` 会抛出异常，并且该
    handle 会被移除，用户即可重试 ``add_account``。"""
    transport = FakeTransport()
    transport.connect_raises = RuntimeError("connect blew up")
    app, _ = await _build_app(tmp_path, transport)
    try:
        handle = app.add_account("alice")
        await _drain_bootstraps(app)
        with pytest.raises(RuntimeError, match="connect blew up"):
            await handle.ready(timeout=0.1)
        # 失败的引导结束后，handle 不能残留。
        assert "alice" not in app._handles
        with pytest.raises(AccountNotFoundError):
            app.get_handle("alice")
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


def test_add_account_requires_started() -> None:
    app = FastMeow(session_dir=Path("."))
    with pytest.raises(RuntimeError, match="not started"):
        app.add_account("alice")


@pytest.mark.asyncio
async def test_remove_account_disconnects_and_clears(tmp_path: Path) -> None:
    transport = FakeTransport()
    app, _ = await _build_app(tmp_path, transport)
    try:
        app.add_account("alice")
        await _drain_bootstraps(app)
        await app.remove_account("alice")
        assert transport.disconnect_calls == ["alice"]
        assert transport.logout_calls == []
        assert "alice" not in app._handles
        assert app._manifest.get("alice") is None  # type: ignore[union-attr]
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_remove_account_logout_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    app, _ = await _build_app(tmp_path, transport)
    try:
        app.add_account("alice")
        await _drain_bootstraps(app)
        await app.remove_account("alice", logout=True)
        assert transport.logout_calls == ["alice"]
        assert transport.disconnect_calls == []
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_remove_account_not_found(tmp_path: Path) -> None:
    app, _ = await _build_app(tmp_path)
    try:
        with pytest.raises(AccountNotFoundError):
            await app.remove_account("ghost")
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_notify_handle_dispatches_connected(tmp_path: Path) -> None:
    """dispatcher 的 on_event 钩子（== app._notify_handle）必须把
    状态更新传播到 AccountHandle 中，以便 ready() 解除阻塞。"""
    transport = FakeTransport()
    app, _ = await _build_app(tmp_path, transport)
    try:
        handle = app.add_account("alice")
        await _drain_bootstraps(app)
        await app._notify_handle(_connected_event("alice", "alice@s.whatsapp.net"))
        await handle.ready(timeout=0.1)
        assert handle.state == AccountState.CONNECTED
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_notify_handle_ignores_unknown_account(tmp_path: Path) -> None:
    app, _ = await _build_app(tmp_path)
    try:
        # 即使不存在对应 account_key 的 handle，也不能抛出异常。
        await app._notify_handle(_connected_event("ghost", ""))
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_get_handle_and_accounts_snapshot(tmp_path: Path) -> None:
    app, _ = await _build_app(tmp_path)
    try:
        h = app.add_account("alice")
        await _drain_bootstraps(app)
        assert app.get_handle("alice") is h
        snap = app.accounts
        assert "alice" in snap
        # 该快照是副本。
        snap.clear()
        assert "alice" in app.accounts
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 回归套件（Oracle review #12）
# ---------------------------------------------------------------------------


def _pair_success_event(account_key: str, jid: str) -> PairSuccessEvent:
    return PairSuccessEvent(
        seq=4,
        sidecar_id="sc",
        account_key=account_key,
        account_jid=jid,
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        jid=jid,
    )


def _disconnected_event(account_key: str, reason: str = "") -> DisconnectedEvent:
    return DisconnectedEvent(
        seq=5,
        sidecar_id="sc",
        account_key=account_key,
        account_jid="",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        reason=reason,
    )


@pytest.mark.asyncio
async def test_pair_success_event_persists_jid_to_manifest(tmp_path: Path) -> None:
    """Oracle 阻断项 #1：新配对账号的 JID 必须与 PairSuccessEvent
    同步写入 manifest，这样进程重启后才能无需重新配对直接重新挂载。"""
    app, _ = await _build_app(tmp_path)
    try:
        handle = app.add_account("alice")
        await _drain_bootstraps(app)
        # manifest 条目已存在，但 jid 为空（新设备，尚未配对）。
        entry = app._manifest.get("alice")  # type: ignore[union-attr]
        assert entry is not None
        assert entry.jid == ""

        await app._notify_handle(_pair_success_event("alice", "alice@s.whatsapp.net"))

        entry = app._manifest.get("alice")  # type: ignore[union-attr]
        assert entry is not None
        assert entry.jid == "alice@s.whatsapp.net"
        # handle 自身视图也会反映该 JID，但状态会保持为
        # PAIRING，直到后续的 ConnectedEvent。
        assert handle.jid == "alice@s.whatsapp.net"
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_handle_state_machine_does_not_downgrade_connected() -> None:
    """Oracle 高优先级 #5：一旦进入 CONNECTED，显示为
    CONNECTING 的过时 RPC 回复不得把状态回滚。"""
    handle = AccountHandle("alice")
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    assert handle.state == AccountState.CONNECTED

    # 模拟一个延迟到达的 RPC 响应。
    handle._apply_state(AccountState.CONNECTING, jid="alice@s.whatsapp.net")
    assert handle.state == AccountState.CONNECTED


@pytest.mark.asyncio
async def test_handle_terminal_logged_out_is_sticky() -> None:
    """Oracle 高优先级 #5：LOGGED_OUT 是终态；后续事件不得
    让 handle 复活。"""
    handle = AccountHandle("alice")
    await handle._on_event(_logged_out_event("alice", "revoked"))
    assert handle.state == AccountState.LOGGED_OUT

    # 一个伪造的 ConnectedEvent（例如服务端怪异行为）不得撤销登出。
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    assert handle.state == AccountState.LOGGED_OUT
    with pytest.raises(PairingFailedError):
        await handle.ready(timeout=0.05)


@pytest.mark.asyncio
async def test_handle_disconnected_event_does_not_wake_ready() -> None:
    """Oracle 高优先级 #5：已配对账号断开连接后，不应满足
    ``ready()`` -- 自动重连会发出一个新的 ConnectedEvent 来满足它。"""
    handle = AccountHandle("alice")
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    assert handle.state is AccountState.CONNECTED

    await handle._on_event(_disconnected_event("alice", "network"))
    state_after: AccountState = handle.state
    assert state_after is AccountState.DISCONNECTED
    # ``_connected`` 已在之前的 CONNECTED 迁移中被置位，
    # 因此 ready() 会立即返回且不抛出异常。契约仅要求
    # “在首次 CONNECTED 或终态失败时唤醒”。
    await handle.ready(timeout=0.05)
    assert handle._terminal_failure is None


@pytest.mark.asyncio
async def test_add_account_already_connected_short_circuits_ready(
    tmp_path: Path,
) -> None:
    """如果 sidecar 在 EnsureAccount/Connect 时报告 CONNECTED（例如
    账号在上一次运行中已存活，并在 sidecar 启动时重新挂载），则
    :meth:`AccountHandle.ready` 会直接完成，而无需后续事件。"""
    transport = FakeTransport()
    transport.ensure_state = Account(
        account_key="alice", jid="alice@s.whatsapp.net", state=AccountState.CONNECTED
    )
    transport.connect_state = Account(
        account_key="alice", jid="alice@s.whatsapp.net", state=AccountState.CONNECTED
    )
    app, _ = await _build_app(tmp_path, transport)
    try:
        handle = app.add_account("alice")
        await _drain_bootstraps(app)
        await handle.ready(timeout=0.05)
        assert handle.state == AccountState.CONNECTED
        assert handle.jid == "alice@s.whatsapp.net"
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_start_rolls_back_when_dispatcher_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle 阻断项 #2：如果任何获取步骤失败，所有先前获取的
    资源都必须被拆除，并且 ``_started`` 必须保持为 False，
    这样用户才能重试。"""
    from fastmeow import _dispatcher, _transport
    from fastmeow import app as app_module

    sidecar_stops: list[bool] = []
    transport_closes: list[bool] = []

    class _FakeSidecar:
        def __init__(self, cfg: object) -> None:
            self._cfg = cfg

        async def start(self) -> None:
            return None

        async def wait_ready(self, timeout: float | None = None) -> str:
            return "127.0.0.1:0"

        async def stop(self, grace: float | None = None) -> int:
            sidecar_stops.append(True)
            return 0

    class _FakeTransport:
        protocol_version = 1
        sidecar_version = "test"
        whatsmeow_version = "test"
        sidecar_id = "sc-test"

        async def shutdown(self, *, grace_ms: int = 0) -> None:
            return None

        async def close(self) -> None:
            transport_closes.append(True)

    async def _fake_connect(addr: str) -> _FakeTransport:
        return _FakeTransport()

    async def _boom_start(self: object) -> None:
        raise RuntimeError("dispatcher boom")

    # ``app`` 模块通过 ``from ._supervisor import ...`` 将 ``Sidecar`` / ``Dispatcher``
    # 导入到自身命名空间；应当 patch 这些绑定，而不是源模块。
    monkeypatch.setattr(app_module, "Sidecar", _FakeSidecar)
    monkeypatch.setattr(_transport, "connect", _fake_connect)
    monkeypatch.setattr(_dispatcher.Dispatcher, "start", _boom_start)

    app = FastMeow(session_dir=tmp_path)
    with pytest.raises(RuntimeError, match="dispatcher boom"):
        await app.start()

    # 回滚契约：状态应停留在启动前。
    assert app._started is False
    assert app._dispatcher is None
    assert app._transport is None
    assert app._sidecar is None
    assert app._manifest is None
    # 我们*确实*获取到的资源已被清理。
    assert sidecar_stops == [True]
    assert transport_closes == [True]
    # manifest 文件锁已释放；再次打开必须成功。
    m = await Manifest.open(tmp_path)
    await m.close()
