"""Public exception hierarchy for FastMeow.

All FastMeow errors derive from :class:`FastMeowError`. Catching that one
type is sufficient to handle every library-raised exception.
"""

from __future__ import annotations


class FastMeowError(Exception):
    """Base class for every FastMeow-raised exception."""


# ---------------------------------------------------------------------------
# Configuration / setup
# ---------------------------------------------------------------------------


class ConfigurationError(FastMeowError):
    """Raised when an :class:`fastmeow.FastMeow` app is misconfigured.

    Examples: duplicate ``account_key``, missing required argument,
    invalid ``on_qr`` selector.
    """


class SidecarBinaryNotFoundError(FastMeowError):
    """Raised when the bundled Go sidecar binary cannot be located.

    The wheel ships ``fastmeow/_bin/<os>-<arch>/fastmeow-sidecar(.exe)``.
    In dev mode we fall back to ``./bin/fastmeow-sidecar(.exe)`` relative
    to the project root.
    """


class ManifestError(ConfigurationError):
    """Raised when the on-disk account manifest is corrupt, locked by
    another process, or drifts from the sidecar's session store.

    Subclass of :class:`ConfigurationError` because every cause is a
    setup-time / persistent-state issue, not a transient runtime fault.
    """


# ---------------------------------------------------------------------------
# Sidecar lifecycle
# ---------------------------------------------------------------------------


class SidecarError(FastMeowError):
    """Base class for sidecar process / transport failures."""


class SidecarStartupError(SidecarError):
    """Sidecar process exited or refused connections during startup."""


class SidecarCrashedError(SidecarError):
    """Sidecar process died unexpectedly while running."""


class TransportError(SidecarError):
    """gRPC channel / stream level failure."""


# ---------------------------------------------------------------------------
# Account / pairing
# ---------------------------------------------------------------------------


class AccountError(FastMeowError):
    """Base class for account-management failures."""


class AccountAlreadyExistsError(AccountError):
    """Raised when ``add_account`` is called twice with the same key."""


class AccountNotFoundError(AccountError):
    """Raised when an operation references an unknown ``account_key``."""


class PairingTimeoutError(AccountError):
    """User did not scan the QR within the configured timeout."""


class PairingFailedError(AccountError):
    """WhatsApp rejected pairing (typically QR expired or device limit)."""


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


class MessagingError(FastMeowError):
    """Base class for outbound-message failures."""


class InvalidJIDError(MessagingError):
    """Provided JID could not be parsed or is for an unsupported server."""


class MessageSendError(MessagingError):
    """Sidecar refused to deliver an outbound message."""


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------


class DispatchError(FastMeowError):
    """Base class for handler-registration / dispatch failures."""


class HandlerSignatureError(DispatchError):
    """Handler declared a parameter FastMeow cannot inject.

    Phase 1 supports the parameter names / annotations: ``msg`` / ``event``
    (any concrete event class), ``ctx`` (:class:`fastmeow.Ctx`), ``qr``
    (:class:`fastmeow.QREvent`), and ``match`` (regex match object,
    ``None`` outside regex filters). Unknown parameters fail fast.
    """


class ReplyNotAvailableError(DispatchError):
    """``ctx.reply`` was accessed outside a :class:`MessageEvent` handler.

    ``reply`` is a convenience that auto-targets the inbound chat. Other
    event types (Connected, Disconnected, QR, ...) have no inbound chat,
    so ``reply`` is intentionally unavailable. Use
    ``ctx.client.send_text(jid, text)`` instead.
    """


class BackpressureError(DispatchError):
    """Raised when a per-account event queue overflows.

    In 0.1.0 every streamed event is critical (message, qr, pair_success,
    connected, disconnected, logged_out), so silent drop-oldest would
    desynchronize user state. The dispatcher fails fast instead: it logs
    loudly, sets the global stop flag, and raises this on the affected
    path so the application stops visibly. Selective shedding may return
    once soft-event classes exist.
    """
