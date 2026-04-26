"""FastMeow: a Pythonic SDK for WhatsApp automation.

Quick start::

    from fastmeow import FastMeow, Router, F

    router = Router()

    @router.message(F.text == "ping")
    async def pong(msg, ctx):
        await ctx.reply("pong")

    async def main():
        async with FastMeow(session_dir="./sessions") as app:
            app.include_router(router)
            handle = await app.add_account("alice", on_qr="terminal")
            await handle.ready()
            await app.run_forever()

The package re-exports the curated public surface here so downstream
code can ``from fastmeow import X`` without reaching into private
submodules. Anything not listed in ``__all__`` is implementation detail
and may change between versions.
"""

from __future__ import annotations

from .app import AccountHandle, FastMeow
from .context import AccountClient, Ctx
from .exceptions import (
    AccountAlreadyExistsError,
    AccountError,
    AccountNotFoundError,
    BackpressureError,
    ConfigurationError,
    DispatchError,
    FastMeowError,
    HandlerSignatureError,
    InvalidJIDError,
    ManifestError,
    MessageSendError,
    MessagingError,
    PairingFailedError,
    PairingTimeoutError,
    ReplyNotAvailableError,
    SidecarBinaryNotFoundError,
    SidecarCrashedError,
    SidecarError,
    SidecarStartupError,
    TransportError,
)
from .filters import F, Filter, FilterResult
from .router import Router, SkipHandler
from .types import (
    Account,
    AccountState,
    ConnectedEvent,
    DisconnectedEvent,
    Event,
    LoggedOutEvent,
    MessageEvent,
    PairSuccessEvent,
    QREvent,
    SendResult,
    UnknownEvent,
)

__version__ = "0.1.0"

__all__ = [  # noqa: RUF022  -- grouped by domain, not alphabetical
    # core app
    "FastMeow",
    "AccountHandle",
    # routing
    "Router",
    "SkipHandler",
    "F",
    "Filter",
    "FilterResult",
    # context
    "Ctx",
    "AccountClient",
    # account
    "Account",
    "AccountState",
    # events
    "Event",
    "MessageEvent",
    "ConnectedEvent",
    "DisconnectedEvent",
    "PairSuccessEvent",
    "LoggedOutEvent",
    "QREvent",
    "UnknownEvent",
    "SendResult",
    # exceptions (full hierarchy)
    "FastMeowError",
    "ConfigurationError",
    "ManifestError",
    "SidecarError",
    "SidecarBinaryNotFoundError",
    "SidecarStartupError",
    "SidecarCrashedError",
    "TransportError",
    "AccountError",
    "AccountAlreadyExistsError",
    "AccountNotFoundError",
    "PairingTimeoutError",
    "PairingFailedError",
    "MessagingError",
    "InvalidJIDError",
    "MessageSendError",
    "DispatchError",
    "BackpressureError",
    "HandlerSignatureError",
    "ReplyNotAvailableError",
    # version
    "__version__",
]
