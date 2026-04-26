# pyright: reportAny=false, reportExplicitAny=false
"""Public, stable, framework-agnostic data types for FastMeow.

These are the objects that user code touches: handler parameters, return
values from public APIs, fields of :class:`fastmeow.Ctx`. They are
intentionally decoupled from the generated protobuf classes so that:

1. Wire-format churn (renames, oneof additions) does not break user code.
2. Users who type-annotate their handlers get rich IDE support without
   importing anything from ``fastmeow._generated``.
3. Tests can construct fakes without touching gRPC.

Conversion from protobuf -> public type happens in
:mod:`fastmeow._dispatcher`. User code never converts the other way.

The ``reportAny`` / ``reportExplicitAny`` pragma above is local to this
module: protobuf-generated message classes are typed as ``Any`` by
basedpyright, so any conversion helper that touches them flags. mypy's
strict mode (the project's source of truth) handles the same code
correctly via the ``fastmeow._generated.*`` override in pyproject.toml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Avoid importing generated stubs at module import time so that
    # ``import fastmeow`` works even before the wheel's _generated/
    # subpackage has been built (e.g. during early development).
    from fastmeow._generated.fastmeow.v1 import gateway_pb2 as _pb


__all__ = [
    "Account",
    "AccountState",
    "ConnectedEvent",
    "DisconnectedEvent",
    "Event",
    "LoggedOutEvent",
    "MessageEvent",
    "PairSuccessEvent",
    "QREvent",
    "SendResult",
    "UnknownEvent",
]


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class AccountState(StrEnum):
    """Lifecycle state of a single WhatsApp account inside the sidecar.

    Values mirror ``fastmeow.v1.AccountState.State`` but use plain string
    members so they're JSON-friendly and round-trip cleanly through
    logging / persistence.
    """

    UNSPECIFIED = "UNSPECIFIED"
    UNPAIRED = "UNPAIRED"
    PAIRING = "PAIRING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    LOGGED_OUT = "LOGGED_OUT"
    RECOVERING = "RECOVERING"

    @classmethod
    def from_proto(cls, value: int) -> AccountState:
        """Map a proto enum int (``AccountState.State.STATE_*``) to us."""
        # Lazy import to keep this module proto-import-free for non-runtime use.
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        # The proto enum members are: STATE_UNSPECIFIED, STATE_UNPAIRED, ...
        # Strip the STATE_ prefix and look us up.
        name = pb.AccountState.State.Name(value)
        suffix = name.removeprefix("STATE_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED


@dataclass(frozen=True, slots=True)
class Account:
    """A WhatsApp account known to FastMeow.

    ``account_key`` is the user-chosen stable identifier (e.g. ``"alice"``).
    ``jid`` is the WhatsApp-issued JID; empty string until paired.
    """

    account_key: str
    jid: str
    state: AccountState
    reason: str = ""

    @classmethod
    def from_proto(cls, msg: _pb.AccountState) -> Account:
        return cls(
            account_key=msg.account_key,
            jid=msg.jid,
            state=AccountState.from_proto(msg.state),
            reason=msg.reason,
        )


# ---------------------------------------------------------------------------
# Events (public surface used by handler functions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EventBase:
    """Common metadata attached to every public event."""

    seq: int
    """Monotonic stream sequence number assigned by the sidecar."""

    sidecar_id: str
    """Sidecar instance id; useful for multi-sidecar deployments."""

    account_key: str
    """The :class:`Account` this event belongs to."""

    account_jid: str
    """JID of the account at observation time. Empty before pairing."""

    observed_at: datetime
    """Sidecar's wall clock when the event was emitted (UTC, tz-aware)."""


@dataclass(frozen=True, slots=True)
class MessageEvent(_EventBase):
    """An inbound (or own-device-echoed) WhatsApp message."""

    message_id: str
    chat_jid: str
    sender_jid: str
    from_me: bool
    is_group: bool
    text: str
    """The message body. Empty for non-text messages (Phase 1 ships text only)."""
    reply_to_message_id: str = ""
    """Message id this one quotes; empty if not a reply."""
    timestamp: datetime | None = None
    """WhatsApp's message timestamp (UTC, tz-aware). May be None."""

    # Convenience predicates -------------------------------------------------

    @property
    def is_dm(self) -> bool:
        """True if this is a 1:1 chat (i.e. not a group)."""
        return not self.is_group

    @property
    def is_reply(self) -> bool:
        return bool(self.reply_to_message_id)


@dataclass(frozen=True, slots=True)
class ConnectedEvent(_EventBase):
    """The account has (re)connected to WhatsApp servers."""


@dataclass(frozen=True, slots=True)
class DisconnectedEvent(_EventBase):
    """The account has dropped its connection. May reconnect automatically."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class PairSuccessEvent(_EventBase):
    """The account just finished QR pairing for the first time."""

    jid: str
    """The newly issued JID. Same value will populate ``account_jid``
    on every subsequent event for this account."""
    business_name: str = ""
    platform: str = ""


@dataclass(frozen=True, slots=True)
class LoggedOutEvent(_EventBase):
    """The account was logged out (user revoked, or sidecar-side logout)."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class QREvent(_EventBase):
    """A new QR code is available for the user to scan.

    ``code`` is the raw WhatsApp QR string (looks like
    ``2@xxxx,yyyy,zzzz``). Render it however you like; for terminal
    rendering install the ``qr`` extra and use
    :meth:`render_terminal`.
    """

    code: str
    ttl_seconds: int = 0

    def render_terminal(self) -> str:
        """Render the QR as ASCII art for terminal display.

        Requires the optional ``qr`` extra (``pip install fastmeow[qr]``).
        Returns a multi-line string. Does NOT print; the caller decides.
        """
        try:
            import qrcode  # type: ignore[import-untyped]  # pyright: ignore[reportMissingModuleSource]
        except ImportError as exc:
            raise ImportError(
                "QREvent.render_terminal() requires the 'qr' extra: " + "pip install fastmeow[qr]"
            ) from exc

        # ``qrcode`` writes ASCII art to a file-like object; we capture it.
        import io

        buf = io.StringIO()
        qr_obj = qrcode.QRCode(border=1)
        qr_obj.add_data(self.code)
        qr_obj.make(fit=True)
        qr_obj.print_ascii(out=buf, invert=False)
        return buf.getvalue()


@dataclass(frozen=True, slots=True)
class UnknownEvent(_EventBase):
    """An event the sidecar emitted but FastMeow doesn't have a typed
    wrapper for. Surfaced so users can inspect / log unexpected traffic.
    """

    go_type: str
    """The Go type name of the underlying whatsmeow event, e.g.
    ``*events.HistorySync``."""


# ``Event`` is the union type for any public event. Handlers can declare
# ``event: Event`` to accept anything, or use a concrete subtype.
Event = (
    MessageEvent
    | ConnectedEvent
    | DisconnectedEvent
    | PairSuccessEvent
    | LoggedOutEvent
    | QREvent
    | UnknownEvent
)


# ---------------------------------------------------------------------------
# RPC results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SendResult:
    """Result of a successful outbound message send."""

    message_id: str
    """WhatsApp-issued message id; same value the recipient will see."""

    server_timestamp: datetime
    """WhatsApp server timestamp (UTC, tz-aware)."""

    deduped: bool
    """True if the sidecar returned a previously-cached result for the
    same ``client_msg_id`` (within the 5-minute / 10k-entry LRU window)
    instead of actually sending again."""

    @classmethod
    def from_proto(cls, msg: _pb.SendMessageResponse) -> SendResult:
        ts = msg.server_timestamp.ToDatetime(tzinfo=UTC)
        return cls(
            message_id=msg.message_id,
            server_timestamp=ts,
            deduped=msg.deduped,
        )


# ---------------------------------------------------------------------------
# Internal helpers (not part of the public API but defined here so the
# dispatcher can build public events with one import).
# ---------------------------------------------------------------------------


def _ts(msg: _pb.StreamEventsResponse) -> datetime:
    """Extract observed_at as a UTC tz-aware datetime."""
    return msg.observed_at.ToDatetime(tzinfo=UTC)


def _base_kwargs(msg: _pb.StreamEventsResponse) -> dict[str, Any]:
    return {
        "seq": msg.seq,
        "sidecar_id": msg.sidecar_id,
        "account_key": msg.account_key,
        "account_jid": msg.account_jid,
        "observed_at": _ts(msg),
    }


def event_from_proto(msg: _pb.StreamEventsResponse) -> Event | None:
    """Convert a wire ``StreamEventsResponse`` to a public :data:`Event`.

    Returns ``None`` if the response carries no recognised oneof payload
    (e.g. bare keepalive). The dispatcher silently drops ``None``.
    """
    base = _base_kwargs(msg)
    which = msg.WhichOneof("event")

    if which == "message":
        m = msg.message
        ts: datetime | None = None
        if m.HasField("timestamp"):
            ts = m.timestamp.ToDatetime(tzinfo=UTC)
        return MessageEvent(
            **base,
            message_id=m.message_id,
            chat_jid=m.chat_jid,
            sender_jid=m.sender_jid,
            from_me=m.from_me,
            is_group=m.is_group,
            text=m.text,
            reply_to_message_id=m.reply_to_message_id,
            timestamp=ts,
        )
    if which == "connected":
        return ConnectedEvent(**base)
    if which == "disconnected":
        return DisconnectedEvent(**base, reason=msg.disconnected.reason)
    if which == "pair_success":
        p = msg.pair_success
        return PairSuccessEvent(
            **base,
            jid=p.jid,
            business_name=p.business_name,
            platform=p.platform,
        )
    if which == "logged_out":
        return LoggedOutEvent(**base, reason=msg.logged_out.reason)
    if which == "qr":
        return QREvent(
            **base,
            code=msg.qr.code,
            ttl_seconds=msg.qr.ttl_seconds,
        )
    if which == "unknown":
        return UnknownEvent(**base, go_type=msg.unknown.go_type)
    return None


# Silence unused-import warnings under TYPE_CHECKING for tools that are
# stricter than ruff's import resolver.
_ = field
