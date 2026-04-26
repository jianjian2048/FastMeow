"""Top-level :class:`FastMeow` application.

This is the public entry point most user code touches::

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

The ``FastMeow`` object owns three collaborators:

* :class:`fastmeow._supervisor.Sidecar`  -- the Go subprocess
* :class:`fastmeow._transport.Transport` -- gRPC client
* :class:`fastmeow._dispatcher.Dispatcher` -- pumps events into the router

It also owns the :class:`fastmeow.manifest.Manifest` so account keys
survive restarts.

``add_account`` is **synchronous-returning, async-completing**: it
returns an :class:`AccountHandle` *synchronously* (no ``await`` needed)
so the user can attach QR callbacks before the device even starts
pairing, then awaits ``handle.ready()`` to block until the account
reaches CONNECTED (or fails). The actual ``EnsureAccount`` / ``Connect``
gRPC round-trips run in a background task owned by :class:`FastMeow`;
their failures are surfaced through :meth:`AccountHandle.ready`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Literal

from . import _transport
from ._dispatcher import Dispatcher, ErrorHook
from ._supervisor import Sidecar, SidecarConfig
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
    """Returned synchronously by :meth:`FastMeow.add_account`.

    Lets user code await pairing/connection completion, attach extra QR
    listeners, or query the current state without re-doing a lookup.

    The handle is event-driven: :class:`FastMeow` notifies it whenever
    the dispatcher sees an event for ``account_key``.
    """

    def __init__(self, account_key: str) -> None:
        self.account_key = account_key
        self._state: AccountState = AccountState.UNSPECIFIED
        self._jid: str = ""
        self._connected = asyncio.Event()
        self._terminal_failure: BaseException | None = None
        self._qr_subscribers: list[QRCallback] = []

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> AccountState:
        return self._state

    @property
    def jid(self) -> str:
        return self._jid

    def on_qr(self, callback: QRCallback) -> None:
        """Register an extra QR observer in addition to the one passed
        to :meth:`FastMeow.add_account`."""
        self._qr_subscribers.append(callback)

    async def ready(self, timeout: float | None = None) -> None:
        """Block until the account reaches CONNECTED.

        Raises:
            PairingTimeoutError: if ``timeout`` elapses first.
            PairingFailedError: if the account ends up LOGGED_OUT or
                otherwise failed before connecting.
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

    # -- internal: state machine ------------------------------------------

    # Terminal states never leave themselves; CONNECTED only yields to a
    # later DISCONNECTED/LOGGED_OUT (auto-reconnect or remote logout).
    _TERMINAL_STATES: frozenset[AccountState] = frozenset({AccountState.LOGGED_OUT})

    def _apply_state(self, state: AccountState, jid: str = "") -> None:
        """Idempotent state transition used by both RPC results and events.

        Rules:
          * Never leave a terminal state (currently only LOGGED_OUT).
          * Never downgrade CONNECTED back to PAIRING / CONNECTING.
          * Setting state == CONNECTED wakes any :meth:`ready` awaiter.
          * Setting state == LOGGED_OUT marks a terminal failure AND wakes
            awaiters so they can observe the exception.
          * ``jid`` is only adopted when non-empty.
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
        """Mark the handle as failed and unblock :meth:`ready`."""
        if self._terminal_failure is None:
            self._terminal_failure = exc
        self._connected.set()

    # -- internal: called by FastMeow event hook --------------------------

    async def _on_event(self, event: object) -> None:
        if isinstance(event, ConnectedEvent):
            self._apply_state(AccountState.CONNECTED, jid=event.account_jid)
        elif isinstance(event, DisconnectedEvent):
            # Don't wake ready(): a paired account that drops will normally
            # auto-reconnect and emit ConnectedEvent again. Pre-pair drops
            # are uninteresting (QR retries continue).
            if self._state is AccountState.CONNECTED:
                self._state = AccountState.DISCONNECTED
        elif isinstance(event, PairSuccessEvent):
            # JID persistence is handled at FastMeow layer (manifest).
            # Here we only update the handle's view; state stays PAIRING
            # until the subsequent ConnectedEvent.
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
# FastMeow application
# ---------------------------------------------------------------------------


class FastMeow:
    """Single-process WhatsApp automation app.

    Parameters:
        session_dir: Directory to store sqlite sessions and manifest.
        sidecar_config: Override the default supervisor configuration.
        on_error: Async error hook for handler exceptions; defaults to
            ``logging.exception``.

    Use as an async context manager to guarantee subprocess teardown::

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

    # -- routing -----------------------------------------------------------

    def include_router(self, router: Router) -> None:
        """Mount a :class:`Router` under the app's root router.

        Forwards to :meth:`Router.include_router` so cycle detection
        and naming behave identically to user-built routers.
        """
        self._router.include_router(router)

    # -- async context manager --------------------------------------------

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

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the sidecar, open the gRPC channel, start dispatching.

        Resource acquisition is **all-or-nothing**: if any step raises
        (sidecar fails to spawn, gRPC handshake times out, dispatcher
        cannot start) every previously-acquired resource is torn down
        before the original exception propagates. The instance is left
        in its pre-:meth:`start` state and may be retried.
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
        """Cooperatively shut down dispatcher, transport, sidecar."""
        if self._stopped:
            return
        self._stopped = True

        # Cancel any in-flight account bootstrap tasks first so they
        # don't race against the transport shutting down.
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
        """Block until the dispatcher's stream ends or :meth:`stop` runs."""
        if self._dispatcher is None:
            raise RuntimeError("FastMeow not started")
        await self._dispatcher.run_until_stopped()

    # -- account management ------------------------------------------------

    def add_account(
        self,
        account_key: str,
        *,
        display_name: str = "",
        jid: str | None = None,
        on_qr: QRCallback | Literal["terminal"] | None = None,
    ) -> AccountHandle:
        """Register and start an account.

        **Synchronous return, asynchronous completion.** This call returns
        an :class:`AccountHandle` immediately, before any gRPC round-trip
        happens. The handle's :meth:`AccountHandle.ready` blocks until
        pairing+connect complete (or fail).

        The actual ``EnsureAccount`` / ``Connect`` calls run in a
        background task owned by this :class:`FastMeow`. Failures during
        bootstrap are surfaced as exceptions raised from
        :meth:`AccountHandle.ready`.

        Args:
            account_key: Stable user-chosen id.
            display_name: Optional WhatsApp display name (new pairings).
            jid: Pre-existing JID to load. ``None`` means consult the
                manifest; if the manifest has no entry, create a new
                device (QR pairing required).
            on_qr: Either an async/sync callable receiving each
                :class:`QREvent`, or the literal string ``"terminal"``
                to use the bundled terminal renderer.

        Raises:
            RuntimeError: if the app has not been :meth:`start`-ed.
            AccountAlreadyExistsError: if ``account_key`` is already
                registered with this :class:`FastMeow` instance.
        """
        if not self._started or self._transport is None or self._manifest is None:
            raise RuntimeError("FastMeow not started; call start() or use 'async with'")

        if account_key in self._handles:
            raise AccountAlreadyExistsError(account_key)

        handle = AccountHandle(account_key)
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
        """Run EnsureAccount + Connect for a freshly-added account.

        Any exception is captured into ``handle._terminal_failure`` so
        :meth:`AccountHandle.ready` raises it; we do not propagate to
        the asyncio task layer (the task would otherwise log an
        unhandled-exception warning at GC time).
        """
        # _started + transport + manifest are guaranteed by add_account.
        assert self._transport is not None
        assert self._manifest is not None
        account_key = handle.account_key

        try:
            # Resolve effective JID: explicit arg > manifest > "" (new).
            effective_jid = jid
            if effective_jid is None:
                existing = self._manifest.get(account_key)
                effective_jid = existing.jid if existing is not None else ""

            # Persist intent before talking to the sidecar so a crash
            # mid-call still leaves the manifest consistent.
            await self._manifest.register(account_key, jid=effective_jid)

            state, _created = await self._transport.ensure_account(
                account_key=account_key,
                display_name=display_name,
                jid=effective_jid,
            )
            handle._apply_state(state.state, jid=state.jid)
            if state.jid and state.jid != effective_jid:
                await self._manifest.update_jid(account_key, state.jid)

            # Connect after EnsureAccount; this is what triggers QR for
            # unpaired devices and reattach for paired ones.
            connected_state = await self._transport.connect(account_key)
            handle._apply_state(connected_state.state, jid=connected_state.jid)
            if connected_state.jid and connected_state.jid != state.jid:
                await self._manifest.update_jid(account_key, connected_state.jid)
        except asyncio.CancelledError:
            # stop() is tearing us down; surface as failure so any
            # ready() awaiter wakes up rather than hanging forever.
            handle._fail(
                PairingFailedError(f"account {account_key!r} bootstrap cancelled (app stopping)")
            )
            raise
        except BaseException as exc:
            _log.exception("bootstrap failed for account %r", account_key)
            handle._fail(exc)
            # Drop the now-unusable handle so the user can retry add_account.
            self._handles.pop(account_key, None)

    async def remove_account(self, account_key: str, *, logout: bool = False) -> None:
        """Disconnect (or log out) an account and forget it locally."""
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
        """Return the previously-created handle for ``account_key``."""
        h = self._handles.get(account_key)
        if h is None:
            raise AccountNotFoundError(account_key)
        return h

    @property
    def accounts(self) -> dict[str, AccountHandle]:
        """Snapshot of currently registered handles."""
        return dict(self._handles)

    @property
    def router(self) -> Router:
        """Root router; user code usually mounts subrouters via
        :meth:`include_router` instead of touching this directly."""
        return self._router

    @property
    def transport(self) -> _transport.Transport:
        """Underlying typed gRPC client. Escape hatch for advanced users."""
        if self._transport is None:
            raise RuntimeError("FastMeow not started")
        return self._transport

    # -- internal hooks ----------------------------------------------------

    async def _notify_handle(self, event: Event) -> None:
        """Forward an event to its account's :class:`AccountHandle`.

        Also persists the freshly-issued JID on :class:`PairSuccessEvent`
        so a process restart can reattach without re-pairing. The
        manifest write is best-effort: any error is logged but never
        raised, since the next :class:`ConnectedEvent` (which carries
        the same JID via ``account_jid``) will retry persistence
        through the bootstrap path.
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
# Helpers
# ---------------------------------------------------------------------------


def _resolve_qr_callback(spec: QRCallback | Literal["terminal"]) -> QRCallback:
    """Translate the ``on_qr=`` shorthand to a callable."""
    if spec == "terminal":

        def render(qr: QREvent) -> None:
            print(qr.render_terminal())

        return render
    return spec


__all__ = ["AccountHandle", "FastMeow"]


# Re-export so users can `from fastmeow import AccountError` if they like;
# the manifest layer also raises ManifestError, kept in `exceptions`.
_ = AccountError
