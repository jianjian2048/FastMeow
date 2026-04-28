"""FastMeow: 为 WhatsApp 自动化设计的 Pythonic SDK。

快速开始::

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

本包在此处重新导出精选的公开接口，以便下游代码可以直接执行 ``from fastmeow import X``，
而无需深入私有子模块。任何未列入 ``__all__`` 的内容均为实现细节，
可能会在不同版本之间发生变化。
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
    ContextOperationNotAvailableError,
    DispatchError,
    FastMeowError,
    GroupError,
    GroupPermissionError,
    HandlerSignatureError,
    InvalidJIDError,
    InviteLinkInvalidError,
    InviteLinkRevokedError,
    ManifestError,
    MediaCorruptedError,
    MediaDownloadError,
    MediaError,
    MediaTooLargeError,
    MediaUnsupportedTypeError,
    MediaUploadError,
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
    ChatPresenceEvent,
    ChatPresenceMedia,
    ChatPresenceState,
    ConnectedEvent,
    DisconnectedEvent,
    Event,
    GroupInfo,
    GroupInfoEvent,
    GroupParticipant,
    GroupParticipantAction,
    GroupParticipantUpdateEvent,
    GroupParticipantUpdateResult,
    JoinedGroupEvent,
    LoggedOutEvent,
    Media,
    MediaInfo,
    MediaKind,
    MessageEvent,
    PairSuccessEvent,
    PresenceEvent,
    PresenceType,
    QREvent,
    ReceiptEvent,
    ReceiptType,
    SendResult,
    UnknownEvent,
)

__version__ = "0.1.0"

__all__ = [  # noqa: RUF022  -- 按领域分组，非字母序
    # 核心应用
    "FastMeow",
    "AccountHandle",
    # 路由
    "Router",
    "SkipHandler",
    "F",
    "Filter",
    "FilterResult",
    # 上下文
    "Ctx",
    "AccountClient",
    # 账号
    "Account",
    "AccountState",
    # 事件
    "Event",
    "MessageEvent",
    "ConnectedEvent",
    "DisconnectedEvent",
    "PairSuccessEvent",
    "LoggedOutEvent",
    "QREvent",
    "UnknownEvent",
    "JoinedGroupEvent",
    "GroupInfoEvent",
    "GroupParticipantUpdateEvent",
    "ReceiptEvent",
    "PresenceEvent",
    "ChatPresenceEvent",
    "SendResult",
    # 群组
    "GroupInfo",
    "GroupParticipant",
    "GroupParticipantAction",
    "GroupParticipantUpdateResult",
    # 媒体
    "Media",
    "MediaInfo",
    "MediaKind",
    # 回执 / 在线状态
    "ReceiptType",
    "PresenceType",
    "ChatPresenceState",
    "ChatPresenceMedia",
    # 异常 (完整层级)
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
    "GroupError",
    "GroupPermissionError",
    "InviteLinkInvalidError",
    "InviteLinkRevokedError",
    "MediaError",
    "MediaUploadError",
    "MediaDownloadError",
    "MediaTooLargeError",
    "MediaUnsupportedTypeError",
    "MediaCorruptedError",
    "DispatchError",
    "BackpressureError",
    "HandlerSignatureError",
    "ReplyNotAvailableError",
    "ContextOperationNotAvailableError",
    # 版本
    "__version__",
]
