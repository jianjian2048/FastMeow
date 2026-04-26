"""Tests for FastMeow application: AccountHandle + add_account flow.

We don't spawn a real sidecar here. Instead the tests construct a
``FastMeow`` and inject fakes for the transport + manifest, exercising
the same code paths ``start()`` would have set up.

``add_account`` is **sync-return / async-complete**: it returns an
``AccountHandle`` immediately and runs ``EnsureAccount`` + ``Connect``
in a background task. Tests that need the bootstrap to have finished
``await _drain_bootstraps(app)`` before asserting transport call sites.
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
# Fakes
# ---------------------------------------------------------------------------


class FakeTransport:
    """Mocks the transport surface FastMeow.add_account / remove_account use."""

    def __init__(self) -> None:
        self.ensure_calls: list[dict[str, Any]] = []
        self.connect_calls: list[str] = []
        self.disconnect_calls: list[str] = []
        self.logout_calls: list[str] = []
        # Override these in tests if you need different return values.
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
    """Build a FastMeow with mocked transport + real manifest, no sidecar."""
    transport = transport or FakeTransport()
    app = FastMeow(session_dir=tmp_path)
    app._manifest = await Manifest.open(tmp_path)
    app._transport = transport  # type: ignore[assignment]
    app._started = True
    return app, transport


async def _drain_bootstraps(app: FastMeow) -> None:
    """Await every in-flight ``add_account`` background task.

    Bootstrap exceptions are intentionally captured into the handle's
    terminal failure (so ``handle.ready()`` raises them); we therefore
    swallow them here using ``return_exceptions=True`` and let the
    individual tests assert via ``ready()``.
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
    # Must not raise.
    await handle._on_event(_qr_event("alice", "2@bad"))


# ---------------------------------------------------------------------------
# _resolve_qr_callback
# ---------------------------------------------------------------------------


def test_resolve_qr_callback_terminal_returns_callable(capsys: Any) -> None:
    cb = _resolve_qr_callback("terminal")
    qr = _qr_event("alice", "2@abcd,efgh,ijkl")
    # The terminal renderer prints; just confirm it's callable and runs.
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
        # Sync-return contract: AccountHandle is available before any
        # gRPC round-trip has happened.
        assert isinstance(handle, AccountHandle)
        assert handle.account_key == "alice"
        assert handle.state is AccountState.UNSPECIFIED

        await _drain_bootstraps(app)
        assert handle.jid == "alice@s.whatsapp.net"
        state_after: AccountState = handle.state
        assert state_after is AccountState.CONNECTING

        assert transport.ensure_calls == [{"account_key": "alice", "display_name": "", "jid": ""}]
        assert transport.connect_calls == ["alice"]

        # Manifest persisted.
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
        # Pre-seed manifest as if a previous run paired this account.
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
    """Manifest is the source of truth; explicit conflicting JID is rejected.

    Bootstrap failure now surfaces through ``handle.ready()`` rather than
    from ``add_account`` itself (which is sync-return).
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
        # Sidecar must not have been called for the conflicting flow.
        assert transport.ensure_calls == []
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_add_account_explicit_jid_matching_manifest_ok(tmp_path: Path) -> None:
    """Explicit JID matching the manifest entry is fine."""
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
    """When the bootstrap task fails, ``handle.ready()`` raises and the
    handle is dropped so the user can retry ``add_account``."""
    transport = FakeTransport()
    transport.connect_raises = RuntimeError("connect blew up")
    app, _ = await _build_app(tmp_path, transport)
    try:
        handle = app.add_account("alice")
        await _drain_bootstraps(app)
        with pytest.raises(RuntimeError, match="connect blew up"):
            await handle.ready(timeout=0.1)
        # Handle must not linger after the failed bootstrap.
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
    """The dispatcher's on_event hook (== app._notify_handle) must
    propagate state updates into the AccountHandle so ready() unblocks."""
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
        # Must not raise even if no handle for the account_key exists.
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
        # Snapshot is a copy.
        snap.clear()
        assert "alice" in app.accounts
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Regression suite (Oracle review #12)
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
    """Oracle BLOCKER #1: a freshly-paired account's JID must be written
    to the manifest synchronously with the PairSuccessEvent so a process
    restart can reattach without re-pairing."""
    app, _ = await _build_app(tmp_path)
    try:
        handle = app.add_account("alice")
        await _drain_bootstraps(app)
        # Manifest entry exists but jid is empty (new device, pre-pair).
        entry = app._manifest.get("alice")  # type: ignore[union-attr]
        assert entry is not None
        assert entry.jid == ""

        await app._notify_handle(_pair_success_event("alice", "alice@s.whatsapp.net"))

        entry = app._manifest.get("alice")  # type: ignore[union-attr]
        assert entry is not None
        assert entry.jid == "alice@s.whatsapp.net"
        # Handle's own view also reflects the JID, but state stays
        # PAIRING until the subsequent ConnectedEvent.
        assert handle.jid == "alice@s.whatsapp.net"
    finally:
        await app._manifest.close()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_handle_state_machine_does_not_downgrade_connected() -> None:
    """Oracle HIGH #5: once CONNECTED, a stale RPC reply showing
    CONNECTING must not roll us back."""
    handle = AccountHandle("alice")
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    assert handle.state == AccountState.CONNECTED

    # Simulate a late RPC response.
    handle._apply_state(AccountState.CONNECTING, jid="alice@s.whatsapp.net")
    assert handle.state == AccountState.CONNECTED


@pytest.mark.asyncio
async def test_handle_terminal_logged_out_is_sticky() -> None:
    """Oracle HIGH #5: LOGGED_OUT is terminal; later events must not
    revive the handle."""
    handle = AccountHandle("alice")
    await handle._on_event(_logged_out_event("alice", "revoked"))
    assert handle.state == AccountState.LOGGED_OUT

    # A spurious ConnectedEvent (e.g. server quirk) must not undo logout.
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    assert handle.state == AccountState.LOGGED_OUT
    with pytest.raises(PairingFailedError):
        await handle.ready(timeout=0.05)


@pytest.mark.asyncio
async def test_handle_disconnected_event_does_not_wake_ready() -> None:
    """Oracle HIGH #5: a paired account dropping its connection should
    not satisfy ``ready()`` -- auto-reconnect emits a fresh
    ConnectedEvent that does."""
    handle = AccountHandle("alice")
    await handle._on_event(_connected_event("alice", "alice@s.whatsapp.net"))
    assert handle.state is AccountState.CONNECTED

    await handle._on_event(_disconnected_event("alice", "network"))
    state_after: AccountState = handle.state
    assert state_after is AccountState.DISCONNECTED
    # ``_connected`` was already set by the prior CONNECTED transition,
    # so ready() returns immediately without raising. The contract is
    # only "wake on first CONNECTED or terminal failure".
    await handle.ready(timeout=0.05)
    assert handle._terminal_failure is None


@pytest.mark.asyncio
async def test_add_account_already_connected_short_circuits_ready(
    tmp_path: Path,
) -> None:
    """If the sidecar reports CONNECTED on EnsureAccount/Connect (e.g.
    the account was already alive in a previous run reattached on
    sidecar boot), :meth:`AccountHandle.ready` resolves without needing
    a follow-up event."""
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
    """Oracle BLOCKER #2: if any acquisition step fails, every
    previously-acquired resource must be torn down and ``_started``
    must remain False so the user can retry."""
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

    # ``app`` module imports ``Sidecar`` / ``Dispatcher`` into its own
    # namespace via ``from ._supervisor import ...``; patch those bindings
    # rather than the source modules.
    monkeypatch.setattr(app_module, "Sidecar", _FakeSidecar)
    monkeypatch.setattr(_transport, "connect", _fake_connect)
    monkeypatch.setattr(_dispatcher.Dispatcher, "start", _boom_start)

    app = FastMeow(session_dir=tmp_path)
    with pytest.raises(RuntimeError, match="dispatcher boom"):
        await app.start()

    # Rollback contract: state is pre-start.
    assert app._started is False
    assert app._dispatcher is None
    assert app._transport is None
    assert app._sidecar is None
    assert app._manifest is None
    # Resources we *did* acquire were cleaned up.
    assert sidecar_stops == [True]
    assert transport_closes == [True]
    # Manifest file lock released; opening again must succeed.
    m = await Manifest.open(tmp_path)
    await m.close()
