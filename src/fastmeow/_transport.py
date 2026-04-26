"""gRPC transport: typed wrapper around the GatewayService stubs.

This module is the *only* place in FastMeow that imports the generated
protobuf/grpc code. Everything else above it (dispatcher, app, user code)
talks in terms of public dataclasses from ``fastmeow.types`` and the
typed ``Transport`` methods defined here.

Goals:
    * Hide the generated stubs behind a narrow, ergonomic API.
    * Convert proto messages -> public dataclasses on the way in,
      and dataclass kwargs -> proto on the way out.
    * Translate ``grpc.aio.AioRpcError`` into the FastMeow exception
      hierarchy so callers never need to import ``grpc`` themselves.
    * Provide a single ``connect()`` that takes the address printed by
      the supervisor and returns a ready-to-use ``Transport``.

Reconnect policy (Phase 2):
    The sidecar runs on the same machine over loopback TCP; the channel
    only fails if the sidecar crashes. We expose ``StreamEvents`` as an
    async iterator and let the dispatcher decide what to do on
    ``UNAVAILABLE`` (typically: stop, surface to the user, don't retry
    silently). A future milestone may add automatic backoff retry here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from ._generated.fastmeow.v1 import gateway_pb2 as pb
from ._generated.fastmeow.v1 import gateway_pb2_grpc as pb_grpc
from .exceptions import (
    AccountAlreadyExistsError,
    AccountError,
    AccountNotFoundError,
    ConfigurationError,
    InvalidJIDError,
    MessageSendError,
    SidecarCrashedError,
    SidecarStartupError,
    TransportError,
)
from .types import Account, Event, SendResult, event_from_proto

# RPC operation tag for RPC-aware error translation.
# Keep this list in sync with Transport methods that call _translate().
RpcOp = Literal[
    "ping",
    "ensure_account",
    "connect",
    "disconnect",
    "logout",
    "send_message",
    "stream_events",
    "shutdown",
]

if TYPE_CHECKING:
    pass


# Phase 1 wire contract; bumped when the proto changes incompatibly.
PROTOCOL_VERSION = 1

# Default deadline for unary RPCs (seconds). StreamEvents has no deadline.
DEFAULT_DEADLINE = 30.0


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _translate(
    exc: grpc.aio.AioRpcError,
    *,
    op: RpcOp,
    account_key: str | None = None,
) -> Exception:
    """Map a gRPC error to a FastMeow exception, RPC-aware.

    Status code conventions used by the Go sidecar (Phase 1):
        * INVALID_ARGUMENT  -> bad request shape (e.g. missing JID)
        * NOT_FOUND         -> unknown account_key
        * ALREADY_EXISTS    -> account_key registered with a different JID
        * FAILED_PRECONDITION -> protocol mismatch (handshake) /
          wrong account state (lifecycle RPCs)
        * UNAVAILABLE       -> sidecar crashed / channel closed
        * INTERNAL          -> bug in the sidecar

    The ``op`` tag disambiguates codes that mean different things on
    different RPCs. The most important examples:
        * ``FAILED_PRECONDITION`` on ``ping``  -> SidecarStartupError
        * ``FAILED_PRECONDITION`` on lifecycle -> AccountError
        * ``INVALID_ARGUMENT`` on ``send_message`` -> MessageSendError
        * ``INVALID_ARGUMENT`` elsewhere       -> ConfigurationError /
          InvalidJIDError
    """
    code = exc.code()
    detail = exc.details() or ""

    # Channel-level failures: same regardless of op.
    if code == grpc.StatusCode.UNAVAILABLE:
        return SidecarCrashedError(f"sidecar unavailable: {detail}")

    # NOT_FOUND / ALREADY_EXISTS are account-shaped on every account RPC.
    if code == grpc.StatusCode.NOT_FOUND:
        return AccountNotFoundError(detail or (account_key or ""))
    if code == grpc.StatusCode.ALREADY_EXISTS:
        return AccountAlreadyExistsError(detail or (account_key or ""))

    if code == grpc.StatusCode.FAILED_PRECONDITION:
        # Handshake: protocol version mismatch.
        if op in ("ping", "shutdown"):
            return SidecarStartupError(f"precondition failed: {detail}")
        # Lifecycle / send: account is in the wrong state for this RPC.
        return AccountError(f"{op}: precondition failed: {detail}")

    if code == grpc.StatusCode.INVALID_ARGUMENT:
        # JID parse / unsupported server is its own subclass.
        if "jid" in detail.lower():
            return InvalidJIDError(detail)
        if op == "send_message":
            return MessageSendError(detail)
        # Bad request shape on a non-send RPC = caller-side configuration.
        return ConfigurationError(f"{op}: invalid argument: {detail}")

    return TransportError(f"{op}: {code.name}: {detail}")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport:
    """Typed facade over the GatewayService gRPC stub.

    Construct via :func:`connect`; do not instantiate directly.

    All methods raise FastMeow exceptions on failure -- never raw
    ``grpc.aio.AioRpcError``.
    """

    def __init__(
        self,
        channel: grpc.aio.Channel,
        stub: pb_grpc.GatewayServiceStub,
        *,
        protocol_version: int,
        sidecar_version: str,
        whatsmeow_version: str,
        sidecar_id: str,
    ) -> None:
        self._channel = channel
        self._stub = stub
        self.protocol_version = protocol_version
        self.sidecar_version = sidecar_version
        self.whatsmeow_version = whatsmeow_version
        self.sidecar_id = sidecar_id

    # -- lifecycle ----------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying gRPC channel."""
        await self._channel.close()

    # -- RPCs ---------------------------------------------------------------

    async def ensure_account(
        self,
        *,
        account_key: str,
        display_name: str = "",
        jid: str = "",
    ) -> tuple[Account, bool]:
        """Register / load an account. Returns ``(state, created)``.

        ``jid`` empty  -> create a new device, expect QR pairing.
        ``jid`` set    -> load the existing device (must match manifest).
        """
        req = pb.EnsureAccountRequest(account_key=account_key, display_name=display_name, jid=jid)
        try:
            resp: pb.EnsureAccountResponse = await self._stub.EnsureAccount(
                req, timeout=DEFAULT_DEADLINE
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="ensure_account", account_key=account_key) from exc
        return Account.from_proto(resp.state), bool(resp.created)

    async def connect(self, account_key: str) -> Account:
        """Bring an account online. Idempotent."""
        try:
            resp: pb.ConnectResponse = await self._stub.Connect(
                pb.ConnectRequest(account_key=account_key),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="connect", account_key=account_key) from exc
        return Account.from_proto(resp.state)

    async def disconnect(self, account_key: str) -> Account:
        """Take an account offline without logging out."""
        try:
            resp: pb.DisconnectResponse = await self._stub.Disconnect(
                pb.DisconnectRequest(account_key=account_key),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="disconnect", account_key=account_key) from exc
        return Account.from_proto(resp.state)

    async def logout(self, account_key: str) -> Account:
        """Log the device out of WhatsApp (revokes server-side session)."""
        try:
            resp: pb.LogoutResponse = await self._stub.Logout(
                pb.LogoutRequest(account_key=account_key),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="logout", account_key=account_key) from exc
        return Account.from_proto(resp.state)

    async def send_text(
        self,
        *,
        account_key: str,
        to_jid: str,
        body: str,
        client_msg_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        """Send a text message.

        ``client_msg_id`` enables idempotent retry. If omitted, FastMeow
        generates a fresh UUID4 per call so two sends never collide on
        the dedup key. Pass an explicit value when *you* want retry
        idempotency across reconnects.
        """
        effective_client_msg_id = client_msg_id or str(uuid4())
        text = pb.TextBody(body=body, reply_to_message_id=reply_to_message_id or "")
        req = pb.SendMessageRequest(
            account_key=account_key,
            to_jid=to_jid,
            client_msg_id=effective_client_msg_id,
            text=text,
        )
        try:
            resp: pb.SendMessageResponse = await self._stub.SendMessage(
                req, timeout=DEFAULT_DEADLINE
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="send_message", account_key=account_key) from exc
        return SendResult.from_proto(resp)

    async def stream_events(
        self,
        *,
        include_soft_events: bool = False,
        resume_after_seq: int = 0,
    ) -> AsyncIterator[Event]:
        """Subscribe to the sidecar event bus.

        Yields events translated from the proto envelope. The stream ends
        when the channel closes (clean shutdown) or raises
        ``SidecarCrashedError`` if the sidecar dies mid-stream.
        """
        req = pb.StreamEventsRequest(
            include_soft_events=include_soft_events,
            resume_after_seq=resume_after_seq,
        )
        call = self._stub.StreamEvents(req)
        try:
            async for envelope in call:  # pyright: ignore[reportGeneralTypeIssues]
                event = event_from_proto(envelope)
                if event is not None:
                    yield event
        except grpc.aio.AioRpcError as exc:
            # Sidecar going down during shutdown looks like CANCELLED;
            # surface it cleanly so the dispatcher can exit its loop.
            if exc.code() == grpc.StatusCode.CANCELLED:
                return
            raise _translate(exc, op="stream_events") from exc

    async def shutdown(self, *, grace_ms: int = 0) -> None:
        """Ask the sidecar to drain in-flight RPCs and exit.

        The supervisor process-level stop is the source of truth; this
        RPC is a polite request that lets the sidecar log
        ``shutdown trigger=rpc`` for diagnostics.
        """
        try:
            await self._stub.Shutdown(
                pb.ShutdownRequest(grace_ms=grace_ms), timeout=DEFAULT_DEADLINE
            )
        except grpc.aio.AioRpcError as exc:
            # UNAVAILABLE on shutdown is expected: server already gone.
            if exc.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.CANCELLED):
                return
            raise _translate(exc, op="shutdown") from exc


# ---------------------------------------------------------------------------
# Connect helper
# ---------------------------------------------------------------------------


async def connect(
    addr: str,
    *,
    expected_protocol_version: int = PROTOCOL_VERSION,
    handshake_timeout: float = 10.0,
) -> Transport:
    """Open a channel to the sidecar and verify the protocol version.

    ``addr`` is the ``host:port`` string the supervisor surfaces from the
    ``listening`` event. We do a Ping RPC immediately so a version
    mismatch fails fast, before any user code touches the channel.
    """
    options = [
        ("grpc.max_send_message_length", 16 * 1024 * 1024),
        ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        # Reasonable keepalive for a loopback channel; whatsmeow
        # itself runs over its own network connection so this only
        # matters when the sidecar wedges.
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
    ]
    channel = grpc.aio.insecure_channel(addr, options=options)
    stub = pb_grpc.GatewayServiceStub(channel)  # type: ignore[no-untyped-call]

    try:
        resp: pb.PingResponse = await stub.Ping(
            pb.PingRequest(client_protocol_version=expected_protocol_version),
            timeout=handshake_timeout,
        )
    except grpc.aio.AioRpcError as exc:
        await channel.close()
        raise _translate(exc, op="ping") from exc

    if resp.server_protocol_version != expected_protocol_version:
        await channel.close()
        raise SidecarStartupError(
            f"protocol version mismatch: client={expected_protocol_version} "
            f"server={resp.server_protocol_version}"
        )

    return Transport(
        channel,
        stub,
        protocol_version=resp.server_protocol_version,
        sidecar_version=resp.sidecar_version,
        whatsmeow_version=resp.whatsmeow_version,
        sidecar_id=resp.sidecar_id,
    )


# Helper for tests/dispatcher: build a Timestamp without leaking the proto type.
def _now_timestamp() -> Timestamp:  # pragma: no cover
    ts = Timestamp()
    ts.GetCurrentTime()
    return ts
