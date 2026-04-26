"""Per-event context object passed to user handlers.

A handler in FastMeow has one of these signatures::

    @router.message()
    async def echo(msg: MessageEvent, ctx: Ctx) -> None: ...

    @router.message()
    async def shorthand(msg: MessageEvent) -> None: ...

    @router.connected()
    async def on_up(ctx: Ctx) -> None: ...

The :class:`Ctx` packages everything a handler might need *for this
specific event* without forcing handlers to remember globals or fish
state out of an app object:

* ``account_key`` — which account dispatched this event.
* ``account_jid`` — the JID of that account at observation time.
* ``event``       — the public event object (same one bound to ``msg``
                    when the handler accepts both).
* ``client``      — an :class:`AccountClient` bound to ``account_key``;
                    use it to send arbitrary messages.
* ``reply()``     — convenience for the very common "respond in the
                    same chat" case. Available **only** when the event
                    is a :class:`MessageEvent`; otherwise raises
                    :class:`ReplyNotAvailableError` so the failure is
                    immediate and obvious instead of an opaque
                    ``AttributeError`` later.

Ctx instances are created by the dispatcher and discarded after the
handler returns. They are not safe to stash for use after the
handler exits — the underlying client may be torn down at any time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from fastmeow.exceptions import ReplyNotAvailableError
from fastmeow.types import Event, MessageEvent, SendResult

if TYPE_CHECKING:
    pass


__all__ = ["AccountClient", "Ctx"]


class AccountClient(Protocol):
    """Account-scoped RPC surface exposed on :attr:`Ctx.client`.

    Defined as a ``Protocol`` so the dispatcher can stay ignorant of
    the concrete gRPC implementation in :mod:`fastmeow._transport`,
    and so tests can substitute a fake without touching the wire.
    """

    @property
    def account_key(self) -> str: ...

    @property
    def jid(self) -> str:
        """Current JID for this account; empty string if unpaired."""
        ...

    async def send_text(
        self,
        to_jid: str,
        body: str,
        *,
        client_msg_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        """Send a plain-text message.

        ``client_msg_id`` is for idempotency. If you don't pass one,
        FastMeow generates a UUIDv4. Passing the same id within the
        sidecar's dedup window (5 min / 10k entries) returns the prior
        :class:`SendResult` with ``deduped=True`` instead of resending.
        """
        ...


@dataclass(frozen=True, slots=True)
class Ctx:
    """Per-event context handed to user handlers.

    Fields:
        account_key: stable user-chosen id for the account.
        account_jid: WhatsApp JID at the time of this event; may be
            empty during the brief pre-pair window.
        event: the public event being dispatched.
        client: account-scoped RPC surface; see :class:`AccountClient`.

    Use :meth:`reply` when responding to a :class:`MessageEvent`.
    Use :meth:`send` for any other outbound traffic (broadcast,
    cross-chat reply, scheduled messages, etc.).
    """

    account_key: str
    account_jid: str
    event: Event
    client: AccountClient

    # -- convenience -------------------------------------------------------

    async def reply(
        self,
        text: str,
        *,
        quoted: bool = True,
        client_msg_id: str | None = None,
    ) -> SendResult:
        """Reply in the same chat as the inbound message.

        ``quoted=True`` (default) attaches the inbound ``message_id`` as
        a quoted reference. Set ``quoted=False`` for a plain follow-up.

        Raises:
            ReplyNotAvailableError: if this Ctx was built for a
                non-message event (e.g. ``ConnectedEvent``).
        """
        if not isinstance(self.event, MessageEvent):
            raise ReplyNotAvailableError(
                f"ctx.reply() is only available for MessageEvent handlers; "
                f"this ctx wraps {type(self.event).__name__}. "
                f"Use ctx.client.send_text(jid, text) instead."
            )
        return await self.client.send_text(
            self.event.chat_jid,
            text,
            client_msg_id=client_msg_id,
            reply_to_message_id=self.event.message_id if quoted else None,
        )

    async def send(
        self,
        to_jid: str,
        text: str,
        *,
        client_msg_id: str | None = None,
    ) -> SendResult:
        """Send a text message to an arbitrary JID using this ctx's
        account.

        Equivalent to ``ctx.client.send_text(to_jid, text, ...)`` but
        spelled the same way as :meth:`reply` for symmetry, so handlers
        don't have to mix two call styles when they branch between
        "reply here" and "notify someone else".
        """
        return await self.client.send_text(
            to_jid,
            text,
            client_msg_id=client_msg_id,
        )

    # -- introspection helpers --------------------------------------------

    @property
    def is_message(self) -> bool:
        return isinstance(self.event, MessageEvent)

    @property
    def message(self) -> MessageEvent:
        """Return ``self.event`` narrowed to :class:`MessageEvent`.

        Convenience for handlers that prefer ``ctx.message.text`` over
        an isinstance check. Raises :class:`ReplyNotAvailableError`
        (same exception family as :meth:`reply`) if the event is not a
        message — this keeps the failure mode consistent.
        """
        if not isinstance(self.event, MessageEvent):
            raise ReplyNotAvailableError(
                f"ctx.message is only valid for MessageEvent contexts; "
                f"this ctx wraps {type(self.event).__name__}."
            )
        return self.event
