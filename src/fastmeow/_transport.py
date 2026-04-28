"""gRPC 传输层：GatewayService stub 的类型化封装。

此模块是 FastMeow 中 *唯一* 导入生成的 protobuf/gRPC 代码的地方。其上层的所有组件（派发器、app、用户代码）
都通过 ``fastmeow.types`` 中的公开数据类和此处定义的类型化 ``Transport`` 方法进行交互。

目标：
    * 将生成的 stub 隐藏在精简且易用的 API 之后。
    * 在输入时将 proto 消息转换为公开数据类，在输出时将数据类参数转换为 proto。
    * 将 ``grpc.aio.AioRpcError`` 转换为 FastMeow 异常体系，使调用者无需自行导入 ``grpc``。
    * 提供单一的 ``connect()`` 函数，接收监督器返回的地址并返回就绪的 ``Transport``。

重连策略（第二阶段）：
    sidecar 通过 loopback TCP 运行在同一台机器上；只有在 sidecar 崩溃时通道才会失效。
    我们将 ``StreamEvents`` 暴露为异步迭代器，并让派发器决定如何处理 ``UNAVAILABLE`` 错误
    （通常是停止运行并告知用户，不进行静默重试）。未来的里程碑可能会在此处添加自动退避重试机制。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from . import _media
from ._generated.fastmeow.v1 import gateway_pb2 as pb
from ._generated.fastmeow.v1 import gateway_pb2_grpc as pb_grpc
from .exceptions import (
    AccountAlreadyExistsError,
    AccountError,
    AccountNotFoundError,
    ConfigurationError,
    GroupError,
    GroupPermissionError,
    InvalidJIDError,
    InviteLinkInvalidError,
    InviteLinkRevokedError,
    MediaCorruptedError,
    MediaDownloadError,
    MediaTooLargeError,
    MediaUnsupportedTypeError,
    MediaUploadError,
    MessageSendError,
    SidecarCrashedError,
    SidecarStartupError,
    TransportError,
)
from .types import (
    Account,
    ChatPresenceMedia,
    ChatPresenceState,
    Event,
    GroupInfo,
    GroupParticipantAction,
    GroupParticipantUpdateResult,
    Media,
    MediaInfo,
    PresenceType,
    ReceiptType,
    SendResult,
    event_from_proto,
)

# 用于 RPC 相关错误转换的 RPC 操作标记。
# 请保持此列表与调用 _translate() 的 Transport 方法同步。
RpcOp = Literal[
    "ping",
    "ensure_account",
    "connect",
    "disconnect",
    "logout",
    "send_message",
    "stream_events",
    "shutdown",
    "list_joined_groups",
    "get_group_info",
    "preview_group_invite",
    "join_group_via_invite",
    "leave_group",
    "create_group",
    "update_group_settings",
    "update_group_participants",
    "get_group_invite_link",
    "mark_read",
    "send_presence",
    "send_chat_presence",
    "subscribe_presence",
    "send_media",
    "download_media",
]

# 上述群组 RPC 的集合，用于在 _translate 中识别群组操作。
_GROUP_OPS: frozenset[RpcOp] = frozenset(
    {
        "list_joined_groups",
        "get_group_info",
        "preview_group_invite",
        "join_group_via_invite",
        "leave_group",
        "create_group",
        "update_group_settings",
        "update_group_participants",
        "get_group_invite_link",
    }
)

if TYPE_CHECKING:
    pass


# 通信协议版本。
# 1 = Phase 1（文本消息 / 群组 / 回执 / 在线状态）。
# 2 = Phase 4.3（在 1 的基础上叠加 SendMedia / DownloadMedia 流式 RPC
#                 与 MediaMessageEvent；不删除任何既有 RPC 或字段）。
# proto 发生不向后兼容的变更时再次递增。
PROTOCOL_VERSION = 2

# 一元 RPC 的默认截止时间（秒）。StreamEvents 没有截止时间。
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
    """将 gRPC 错误映射为 FastMeow 异常（识别 RPC 类型）。

    Go sidecar 使用的状态码约定（第一阶段）：
        * INVALID_ARGUMENT    -> 请求格式错误（例如缺少 JID）
        * NOT_FOUND           -> 未知的 account_key
        * ALREADY_EXISTS      -> account_key 已注册到不同的 JID
        * FAILED_PRECONDITION -> 协议版本不匹配（握手）/ 账号状态错误（生命周期 RPC）
        * UNAVAILABLE         -> sidecar 崩溃 / 通道关闭
        * INTERNAL            -> sidecar 内部 bug

    ``op`` 标记用于区分在不同 RPC 中含义不同的状态码。最重要的示例：
        * ``ping`` 上的 ``FAILED_PRECONDITION``      -> SidecarStartupError
        * 生命周期 RPC 上的 ``FAILED_PRECONDITION`` -> AccountError
        * ``send_message`` 上的 ``INVALID_ARGUMENT`` -> MessageSendError
        * 其他位置的 ``INVALID_ARGUMENT``           -> ConfigurationError / InvalidJIDError
    """
    code = exc.code()
    detail = exc.details() or ""

    # 通道级故障：与具体 op 无关。
    if code == grpc.StatusCode.UNAVAILABLE:
        return SidecarCrashedError(f"sidecar unavailable: {detail}")

    # 群组 RPC 的细化映射（必须在通用 NOT_FOUND/INVALID_ARGUMENT 分支之前，
    # 否则邀请链接相关的 NOT_FOUND 会被错误地识别为 AccountNotFoundError）。
    if op in _GROUP_OPS:
        detail_l = detail.lower()
        if code == grpc.StatusCode.PERMISSION_DENIED:
            return GroupPermissionError(f"{op}: {detail}")
        if code == grpc.StatusCode.INVALID_ARGUMENT:
            if "invite" in detail_l:
                return InviteLinkInvalidError(detail)
            if "jid" in detail_l:
                return InvalidJIDError(detail)
            return GroupError(f"{op}: invalid argument: {detail}")
        if code == grpc.StatusCode.NOT_FOUND:
            if "invite" in detail_l:
                return InviteLinkRevokedError(detail)
            # 群组 RPC 上的 NOT_FOUND 可能是未知 account_key 或未知群组；
            # account_key 缺失时 sidecar 会先在请求验证阶段返回 INVALID_ARGUMENT，
            # 因此到这里通常是群组未找到。
            return GroupError(f"{op}: not found: {detail}")
        # 落到通用 GroupError（FAILED_PRECONDITION / INTERNAL 等）。
        return GroupError(f"{op}: {code.name}: {detail}")

    # 媒体 RPC 的细化映射（在通用分支之前）。
    # sidecar 的 ``mediaErrToStatus`` 已经把 Go 端的 sentinel 翻译为状态码，
    # 这里再按 op + status 二次细分到 Python 异常子类。
    if op in ("send_media", "download_media"):
        if code == grpc.StatusCode.INVALID_ARGUMENT:
            # ErrUnsupportedMediaKind / ErrMissingMimeType /
            # ErrUnknownMediaType / ErrMissingDownloadInfo / 缺 JID 等。
            if "jid" in detail.lower():
                return InvalidJIDError(detail)
            return MediaUnsupportedTypeError(f"{op}: {detail}")
        if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
            # ErrMediaTooLarge —— 出站超过 sidecar 配置上限。
            return MediaTooLargeError(detail)
        if code == grpc.StatusCode.DATA_LOSS:
            # whatsmeow 完整性校验失败：长度 / SHA-256 / HMAC 不匹配。
            return MediaCorruptedError(detail)
        if code == grpc.StatusCode.FAILED_PRECONDITION:
            # ErrDeclaredLengthMismatch（出站）/ ErrNoURLPresent（入站）。
            if op == "send_media":
                return MediaUploadError(f"{op}: {detail}")
            return MediaDownloadError(f"{op}: {detail}")
        if code == grpc.StatusCode.NOT_FOUND:
            # 仅可能来自 account_key 未注册；上面的通道级 UNAVAILABLE
            # 已被早一段提前消费，所以这里安全。
            return AccountNotFoundError(detail or (account_key or ""))
        # 其余（INTERNAL 等）按上传 / 下载方向落到对应基类。
        if op == "send_media":
            return MediaUploadError(f"{op}: {code.name}: {detail}")
        return MediaDownloadError(f"{op}: {code.name}: {detail}")

    # NOT_FOUND / ALREADY_EXISTS 在每个账号相关的 RPC 中都与账号有关。
    if code == grpc.StatusCode.NOT_FOUND:
        return AccountNotFoundError(detail or (account_key or ""))
    if code == grpc.StatusCode.ALREADY_EXISTS:
        return AccountAlreadyExistsError(detail or (account_key or ""))

    if code == grpc.StatusCode.FAILED_PRECONDITION:
        # 握手：协议版本不匹配。
        if op in ("ping", "shutdown"):
            return SidecarStartupError(f"precondition failed: {detail}")
        # 生命周期 / 发送：账号当前状态无法执行此 RPC。
        return AccountError(f"{op}: precondition failed: {detail}")

    if code == grpc.StatusCode.INVALID_ARGUMENT:
        # JID 解析失败或服务器不支持。
        if "jid" in detail.lower():
            return InvalidJIDError(detail)
        if op == "send_message":
            return MessageSendError(detail)
        # 非发送类 RPC 的请求格式错误 = 调用方配置问题。
        return ConfigurationError(f"{op}: invalid argument: {detail}")

    return TransportError(f"{op}: {code.name}: {detail}")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport:
    """GatewayService gRPC stub 的类型化门面。

    请通过 :func:`connect` 构建；请勿直接实例化。

    所有方法在失败时都会抛出 FastMeow 异常 —— 绝不会直接抛出 ``grpc.aio.AioRpcError``。
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

    # -- 生命周期 ----------------------------------------------------------

    async def close(self) -> None:
        """关闭底层的 gRPC 通道。"""
        await self._channel.close()

    # -- RPCs ---------------------------------------------------------------

    async def ensure_account(
        self,
        *,
        account_key: str,
        display_name: str = "",
        jid: str = "",
    ) -> tuple[Account, bool]:
        """注册 / 加载账号。返回 ``(state, created)``。

        ``jid`` 为空  -> 创建新设备，需进行二维码配对。
        ``jid`` 已设置 -> 加载现有设备（必须与 manifest 清单匹配）。
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
        """使账号上线。幂等。"""
        try:
            resp: pb.ConnectResponse = await self._stub.Connect(
                pb.ConnectRequest(account_key=account_key),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="connect", account_key=account_key) from exc
        return Account.from_proto(resp.state)

    async def disconnect(self, account_key: str) -> Account:
        """使账号下线但不退出登录。"""
        try:
            resp: pb.DisconnectResponse = await self._stub.Disconnect(
                pb.DisconnectRequest(account_key=account_key),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="disconnect", account_key=account_key) from exc
        return Account.from_proto(resp.state)

    async def logout(self, account_key: str) -> Account:
        """注销设备在 WhatsApp 的登录（撤销服务器端会话）。"""
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
        """发送文本消息。

        ``client_msg_id`` 可实现幂等重试。如果省略，FastMeow
        在每次调用时都会生成一个新的 UUID4，因此两次发送绝不会在
        去重键上发生碰撞。当 *你* 希望在重连时保持重试幂等性时，
        请传入一个显式的值。
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
        """订阅 sidecar 事件总线。

        产出从 proto 信封转换而来的事件。当通道关闭（优雅关闭）时
        流结束；如果 sidecar 在流运行期间异常终止，则抛出
        ``SidecarCrashedError``。
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
            # sidecar 在关闭期间退出会产生 CANCELLED 错误；
            # 对其进行平滑处理，以便派发器可以退出循环。
            if exc.code() == grpc.StatusCode.CANCELLED:
                return
            raise _translate(exc, op="stream_events") from exc

    # -- 群组 RPC（Phase 4.1） --------------------------------------------

    async def list_joined_groups(self, account_key: str) -> tuple[GroupInfo, ...]:
        """列出账号已加入的所有群组。"""
        try:
            resp: pb.ListJoinedGroupsResponse = await self._stub.ListJoinedGroups(
                pb.ListJoinedGroupsRequest(account_key=account_key),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="list_joined_groups", account_key=account_key) from exc
        return tuple(GroupInfo.from_proto(g) for g in resp.groups)

    async def get_group_info(self, *, account_key: str, group_jid: str) -> GroupInfo:
        """获取群组的元数据快照。"""
        try:
            resp: pb.GetGroupInfoResponse = await self._stub.GetGroupInfo(
                pb.GetGroupInfoRequest(account_key=account_key, group_jid=group_jid),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="get_group_info", account_key=account_key) from exc
        return GroupInfo.from_proto(resp.group_info)

    async def preview_group_invite(
        self, *, account_key: str, invite_link: str
    ) -> GroupInfo:
        """预览邀请链接背后的群组（不加入）。"""
        try:
            resp: pb.PreviewGroupInviteResponse = await self._stub.PreviewGroupInvite(
                pb.PreviewGroupInviteRequest(
                    account_key=account_key, invite_link=invite_link
                ),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="preview_group_invite", account_key=account_key) from exc
        return GroupInfo.from_proto(resp.group_info)

    async def join_group_via_invite(
        self, *, account_key: str, invite_link: str
    ) -> str:
        """通过邀请链接加入群组，返回群组 JID。"""
        try:
            resp: pb.JoinGroupViaInviteResponse = await self._stub.JoinGroupViaInvite(
                pb.JoinGroupViaInviteRequest(
                    account_key=account_key, invite_link=invite_link
                ),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(
                exc, op="join_group_via_invite", account_key=account_key
            ) from exc
        return resp.group_jid

    async def leave_group(self, *, account_key: str, group_jid: str) -> None:
        """退出群组。"""
        try:
            await self._stub.LeaveGroup(
                pb.LeaveGroupRequest(account_key=account_key, group_jid=group_jid),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="leave_group", account_key=account_key) from exc

    async def create_group(
        self,
        *,
        account_key: str,
        name: str,
        participant_jids: tuple[str, ...] = (),
    ) -> GroupInfo:
        """创建新群组并初始化成员。"""
        try:
            resp: pb.CreateGroupResponse = await self._stub.CreateGroup(
                pb.CreateGroupRequest(
                    account_key=account_key,
                    name=name,
                    participant_jids=list(participant_jids),
                ),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="create_group", account_key=account_key) from exc
        return GroupInfo.from_proto(resp.group_info)

    async def update_group_settings(
        self,
        *,
        account_key: str,
        group_jid: str,
        name: str | None = None,
        topic: str | None = None,
        is_announce: bool | None = None,
        is_locked: bool | None = None,
    ) -> GroupInfo:
        """部分更新群组设置（仅传入非 None 字段被应用）。

        sidecar 顺序应用 4 个 setter 并返回最新快照；不是事务，
        中途失败会让前面的更改保留。
        """
        req = pb.UpdateGroupSettingsRequest(
            account_key=account_key,
            group_jid=group_jid,
            name=name or "",
            has_name=name is not None,
            topic=topic or "",
            has_topic=topic is not None,
            is_announce=bool(is_announce) if is_announce is not None else False,
            has_is_announce=is_announce is not None,
            is_locked=bool(is_locked) if is_locked is not None else False,
            has_is_locked=is_locked is not None,
        )
        try:
            resp: pb.UpdateGroupSettingsResponse = await self._stub.UpdateGroupSettings(
                req, timeout=DEFAULT_DEADLINE
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(
                exc, op="update_group_settings", account_key=account_key
            ) from exc
        return GroupInfo.from_proto(resp.group_info)

    async def update_group_participants(
        self,
        *,
        account_key: str,
        group_jid: str,
        action: GroupParticipantAction,
        participant_jids: tuple[str, ...],
    ) -> tuple[GroupParticipantUpdateResult, ...]:
        """添加 / 移除 / 提升 / 降级群成员。返回逐条结果。"""
        # proto stub 期望嵌套 enum 类型；运行时 int 值与 enum 值等价。
        # 我们传字符串名让 protobuf 自行转换，避免类型层面的 cast。
        action_name = f"GROUP_PARTICIPANT_ACTION_{action.value}"
        try:
            resp: pb.UpdateGroupParticipantsResponse = (
                await self._stub.UpdateGroupParticipants(
                    pb.UpdateGroupParticipantsRequest(
                        account_key=account_key,
                        group_jid=group_jid,
                        action=action_name,
                        participant_jids=list(participant_jids),
                    ),
                    timeout=DEFAULT_DEADLINE,
                )
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(
                exc, op="update_group_participants", account_key=account_key
            ) from exc
        return tuple(
            GroupParticipantUpdateResult.from_proto(r) for r in resp.results
        )

    async def get_group_invite_link(
        self, *, account_key: str, group_jid: str, reset: bool = False
    ) -> str:
        """获取（或重置）群组邀请链接。"""
        try:
            resp: pb.GetGroupInviteLinkResponse = await self._stub.GetGroupInviteLink(
                pb.GetGroupInviteLinkRequest(
                    account_key=account_key, group_jid=group_jid, reset=reset
                ),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(
                exc, op="get_group_invite_link", account_key=account_key
            ) from exc
        return resp.invite_link

    # -- 回执 / 在线状态 RPC（Phase 4.2） --------------------------------

    async def mark_read(
        self,
        *,
        account_key: str,
        chat_jid: str,
        sender_jid: str,
        message_ids: tuple[str, ...],
        receipt_type: ReceiptType = ReceiptType.READ,
        read_at: datetime | None = None,
    ) -> None:
        """发送已读 / 已播放回执（标记一批消息）。

        ``receipt_type`` 默认为 ``READ``；语音消息建议传 ``PLAYED``。
        ``read_at=None`` 时由 sidecar 使用当前时间作为时间戳。
        """
        req = pb.MarkReadRequest(
            account_key=account_key,
            chat_jid=chat_jid,
            sender_jid=sender_jid,
            message_ids=list(message_ids),
            receipt_type=f"RECEIPT_TYPE_{receipt_type.value}",
        )
        if read_at is not None:
            ts = Timestamp()
            ts.FromDatetime(read_at)
            req.read_at.CopyFrom(ts)
        try:
            await self._stub.MarkRead(req, timeout=DEFAULT_DEADLINE)
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="mark_read", account_key=account_key) from exc

    async def send_presence(
        self,
        *,
        account_key: str,
        presence: PresenceType,
    ) -> None:
        """发送账号级在线状态（``AVAILABLE`` / ``UNAVAILABLE``）。"""
        try:
            await self._stub.SendPresence(
                pb.SendPresenceRequest(
                    account_key=account_key,
                    presence=f"PRESENCE_TYPE_{presence.value}",
                ),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="send_presence", account_key=account_key) from exc

    async def send_chat_presence(
        self,
        *,
        account_key: str,
        chat_jid: str,
        state: ChatPresenceState,
        media: ChatPresenceMedia = ChatPresenceMedia.TEXT,
    ) -> None:
        """发送会话级输入状态（"正在输入" / "已暂停"）。"""
        try:
            await self._stub.SendChatPresence(
                pb.SendChatPresenceRequest(
                    account_key=account_key,
                    chat_jid=chat_jid,
                    state=f"CHAT_PRESENCE_STATE_{state.value}",
                    media=f"CHAT_PRESENCE_MEDIA_{media.value}",
                ),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(
                exc, op="send_chat_presence", account_key=account_key
            ) from exc

    async def subscribe_presence(
        self,
        *,
        account_key: str,
        jid: str,
    ) -> None:
        """订阅指定联系人的在线状态推送。

        whatsmeow 会在订阅后异步推送 ``PresenceEvent``；此 RPC 仅传达订阅意图。
        """
        try:
            await self._stub.SubscribePresence(
                pb.SubscribePresenceRequest(account_key=account_key, jid=jid),
                timeout=DEFAULT_DEADLINE,
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(
                exc, op="subscribe_presence", account_key=account_key
            ) from exc

    # -- 媒体 RPC（Phase 4.3） --------------------------------------------

    async def send_media(
        self,
        *,
        account_key: str,
        to_jid: str,
        media: Media,
        client_msg_id: str | None = None,
        quoted_message_id: str = "",
    ) -> SendResult:
        """流式上传媒体并发送对应的 WhatsApp 消息。

        实现细节：

        * 使用 client streaming（``SendMedia``）：先 1 帧 ``init`` 携带元信息，
          然后若干 256 KiB ``chunk``。所有装配交给 :mod:`._media`。
        * ``client_msg_id`` 是上传幂等键。省略时由 FastMeow 生成 UUID4；
          想在重连时保留幂等性请显式传入。
        * 截止时间：媒体上传可能远超普通 RPC 的 30 秒 ——
          Phase 4.3 暂不强制 deadline，让 sidecar 决定何时放弃。
          后续如出现长尾再加可配阈值。
        """
        effective_client_msg_id = client_msg_id or str(uuid4())
        try:
            resp: pb.SendMediaResponse = await self._stub.SendMedia(
                _media.iter_send_requests(
                    account_key=account_key,
                    to_jid=to_jid,
                    media=media,
                    client_msg_id=effective_client_msg_id,
                    quoted_message_id=quoted_message_id,
                ),
            )
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="send_media", account_key=account_key) from exc
        return SendResult(
            message_id=resp.message_id,
            server_timestamp=resp.server_timestamp.ToDatetime(tzinfo=UTC),
            deduped=resp.deduped,
        )

    async def download_media(
        self,
        *,
        account_key: str,
        info: MediaInfo,
    ) -> bytes:
        """下载媒体并以 ``bytes`` 返回完整内容。

        适用于小到中等尺寸的载荷；大文件请优先使用
        :meth:`download_media_to`，避免单次内存峰值。
        """
        request = _media.build_download_request(account_key=account_key, info=info)
        call: AsyncIterator[pb.DownloadMediaChunk] = self._stub.DownloadMedia(request)
        try:
            return await _media.collect_download(call)
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="download_media", account_key=account_key) from exc

    async def download_media_to(
        self,
        *,
        account_key: str,
        info: MediaInfo,
        path: str | Path,
    ) -> Path:
        """流式下载媒体到 ``path``，返回最终路径。

        IO 卸载到工作线程；失败时半成品文件会被尽力清理（清理失败被吞掉，
        以免掩盖原异常）。父目录必须已存在。
        """
        request = _media.build_download_request(account_key=account_key, info=info)
        call: AsyncIterator[pb.DownloadMediaChunk] = self._stub.DownloadMedia(request)
        try:
            return await _media.write_download_to(call, path)
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc, op="download_media", account_key=account_key) from exc

    async def shutdown(self, *, grace_ms: int = 0) -> None:
        """请求 sidecar 清理运行中的 RPC 并退出。

        监督器的进程级停止是最终标准；此 RPC 是一个礼貌性请求，
        允许 sidecar 记录 ``shutdown trigger=rpc`` 以供诊断。
        """
        try:
            await self._stub.Shutdown(
                pb.ShutdownRequest(grace_ms=grace_ms), timeout=DEFAULT_DEADLINE
            )
        except grpc.aio.AioRpcError as exc:
            # 在关闭时出现 UNAVAILABLE 是预期的：服务器已关闭。
            if exc.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.CANCELLED):
                return
            raise _translate(exc, op="shutdown") from exc


# ---------------------------------------------------------------------------
# 连接助手
# ---------------------------------------------------------------------------


async def connect(
    addr: str,
    *,
    expected_protocol_version: int = PROTOCOL_VERSION,
    handshake_timeout: float = 10.0,
) -> Transport:
    """打开与 sidecar 的通道并验证协议版本。

    ``addr`` 是监督器从 ``listening`` 事件中提取的 ``host:port`` 字符串。
    我们立即执行 Ping RPC 以确保版本不匹配时能快速失败，避免用户代码触及通道。
    """
    options = [
        ("grpc.max_send_message_length", 16 * 1024 * 1024),
        ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        # 适用于回环通道的合理保活设置；whatsmeow 本身通过
        # 自己的网络连接运行，因此这仅在 sidecar 卡死时起作用。
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


# 用于测试/派发器的助手：在不泄露 proto 类型的情况下构建 Timestamp。
def _now_timestamp() -> Timestamp:  # pragma: no cover
    ts = Timestamp()
    ts.GetCurrentTime()
    return ts
