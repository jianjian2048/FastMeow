# pyright: reportAny=false, reportExplicitAny=false
"""FastMeow 使用的公开、稳定且与框架无关的数据类型。

这些是用户代码接触的对象：处理器参数、公开 API 的返回值、:class:`fastmeow.Ctx` 的字段。
它们刻意与生成的 protobuf 类解耦，以便：

1. 传输格式的变动（更名、新增 oneof）不会破坏用户代码。
2. 对处理器进行类型注解的用户可以获得丰富的 IDE 支持，而无需从 ``fastmeow._generated`` 导入任何内容。
3. 测试可以在不触及 gRPC 的情况下构建模拟对象。

从 protobuf 到公开类型的转换发生在 :mod:`fastmeow._dispatcher` 中。用户代码永远不需要进行反向转换。

上方的 ``reportAny`` / ``reportExplicitAny`` pragma 仅针对本模块：
protobuf 生成的消息类被 basedpyright 视为 ``Any`` 类型，因此任何触及它们的转换辅助函数都会触发警告。
mypy 的严格模式（项目的唯一事实来源）通过 pyproject.toml 中的 ``fastmeow._generated.*`` 覆盖配置正确处理了相同的代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    # 仅在类型检查时引入生成的 stub，以避免 wheel 中 _generated/
    # 子包加载之前（例如开发期间）``import fastmeow`` 触发解析错误。
    from fastmeow._generated.fastmeow.v1 import gateway_pb2 as _pb


__all__ = [
    "Account",
    "AccountState",
    "ChatPresenceEvent",
    "ChatPresenceMedia",
    "ChatPresenceState",
    "ConnectedEvent",
    "DisconnectedEvent",
    "Event",
    "GroupInfo",
    "GroupInfoEvent",
    "GroupParticipant",
    "GroupParticipantAction",
    "GroupParticipantUpdateEvent",
    "GroupParticipantUpdateResult",
    "JoinedGroupEvent",
    "LoggedOutEvent",
    "Media",
    "MediaInfo",
    "MediaKind",
    "MessageEvent",
    "PairSuccessEvent",
    "PresenceEvent",
    "PresenceType",
    "QREvent",
    "ReceiptEvent",
    "ReceiptType",
    "SendResult",
    "UnknownEvent",
]


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class AccountState(StrEnum):
    """sidecar 中单个 WhatsApp 账号的生命周期状态。

    值镜像了 ``fastmeow.v1.AccountState.State``，但使用纯字符串成员，
    以便于 JSON 友好，并能通过日志/持久化进行干净的往返转换。
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
        """将 proto 枚举整数 (``AccountState.State.STATE_*``) 映射到本类。"""
        # 延迟导入以保持此模块在非运行时的 proto 导入隔离。
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        # proto 枚举成员为：STATE_UNSPECIFIED, STATE_UNPAIRED, ...
        # 去掉 STATE_ 前缀并进行查找。
        name = pb.AccountState.State.Name(value)
        suffix = name.removeprefix("STATE_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED


@dataclass(frozen=True, slots=True)
class Account:
    """FastMeow 已知的 WhatsApp 账号。

    ``account_key`` 是用户选择的稳定标识符（例如 ``"alice"``）。
    ``jid`` 是 WhatsApp 分配的 JID；配对前为空字符串。
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
    """附加到每个公开事件的通用元数据。"""

    seq: int
    """由 sidecar 分配的单调递增流序列号。"""

    sidecar_id: str
    """sidecar 实例 ID；在多 sidecar 部署中很有用。"""

    account_key: str
    """此事件所属的 :class:`Account`。"""

    account_jid: str
    """观察到事件时的账号 JID。配对前为空。"""

    observed_at: datetime
    """sidecar 发出事件时的墙上时钟（UTC，包含时区信息）。"""


@dataclass(frozen=True, slots=True)
class MessageEvent(_EventBase):
    """收到的（或来自自身设备镜像的）WhatsApp 消息。"""

    message_id: str
    chat_jid: str
    sender_jid: str
    from_me: bool
    is_group: bool
    text: str
    """消息正文。非文本消息此项为空（Phase 1 仅支持文本）。"""
    reply_to_message_id: str = ""
    """此消息引用的消息 ID；如果不是回复则为空。"""
    timestamp: datetime | None = None
    """WhatsApp 消息时间戳（UTC，包含时区信息）。可能为 None。"""
    caption: str = ""
    """媒体消息附带的文字说明。``text`` 为空时由 ``MediaMessageEvent`` 折叠而来；
    纯文本消息此项始终为空，便于 ``F.text == ...`` 与 ``F.caption == ...`` 区分书写习惯。"""
    media: MediaInfo | None = None
    """入站媒体的不可变描述符。``None`` 表示纯文本消息；非空时 ``text`` 必为空。
    可直接传给 :meth:`fastmeow.AccountClient.download_media` / ``download_media_to``
    完成下载（Phase 4.3 起可用）。"""

    # 便捷谓词 -------------------------------------------------

    @property
    def is_dm(self) -> bool:
        """如果这是 1:1 聊天（即不是群组）则返回 True。"""
        return not self.is_group

    @property
    def is_reply(self) -> bool:
        return bool(self.reply_to_message_id)

    # 媒体便捷谓词（Phase 4.3） ------------------------------------
    # 这些纯属为 ``F.has_media`` / ``F.is_image`` 等魔法过滤器服务：
    # ``F`` 仅做属性读取，于是把判定收口在事件对象本身就能让 filters
    # 引擎保持零结构改动。每个谓词都对 ``media is None`` 做短路，未来
    # 即便消息子类型扩张，纯文本消息也永远是 ``False``。

    @property
    def has_media(self) -> bool:
        """是否携带媒体载荷（与 ``text`` 互斥）。"""
        return self.media is not None

    @property
    def is_image(self) -> bool:
        return self.media is not None and self.media.kind is MediaKind.IMAGE

    @property
    def is_video(self) -> bool:
        return self.media is not None and self.media.kind is MediaKind.VIDEO

    @property
    def is_audio(self) -> bool:
        return self.media is not None and self.media.kind is MediaKind.AUDIO

    @property
    def is_document(self) -> bool:
        return self.media is not None and self.media.kind is MediaKind.DOCUMENT

    @property
    def is_sticker(self) -> bool:
        return self.media is not None and self.media.kind is MediaKind.STICKER


@dataclass(frozen=True, slots=True)
class ConnectedEvent(_EventBase):
    """账号已（重新）连接到 WhatsApp 服务器。"""


@dataclass(frozen=True, slots=True)
class DisconnectedEvent(_EventBase):
    """账号连接已断开。可能会自动重连。"""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class PairSuccessEvent(_EventBase):
    """账号刚刚完成首次二维码配对。"""

    jid: str
    """新分配的 JID。此账号随后的每个事件都将使用此值填充 ``account_jid``。"""
    business_name: str = ""
    platform: str = ""


@dataclass(frozen=True, slots=True)
class LoggedOutEvent(_EventBase):
    """账号已登出（用户撤销，或 sidecar 端登出）。"""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class QREvent(_EventBase):
    """有新的二维码可供用户扫描。

    ``code`` 是原始的 WhatsApp 二维码字符串（形如 ``2@xxxx,yyyy,zzzz``）。
    你可以按需进行渲染；如需在终端渲染，请安装 ``qr`` 额外依赖并使用
    :meth:`render_terminal`。
    """

    code: str
    ttl_seconds: int = 0

    def render_terminal(self) -> str:
        """将二维码渲染为 ASCII 字符图，以便在终端显示。

        需要可选的 ``qr`` 额外依赖 (``pip install fastmeow[qr]``)。
        返回多行字符串。不会直接打印；由调用者决定。
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


# ---------------------------------------------------------------------------
# Groups（Phase 4.1）
# ---------------------------------------------------------------------------


class GroupParticipantAction(StrEnum):
    """``UpdateGroupParticipants`` / ``GroupParticipantUpdateEvent`` 的动作类型。

    值镜像了 proto 嵌套 enum
    ``GroupParticipantUpdateEvent.GroupParticipantAction``，但使用纯字符串
    成员，便于日志/序列化。
    """

    UNSPECIFIED = "UNSPECIFIED"
    ADD = "ADD"
    REMOVE = "REMOVE"
    PROMOTE = "PROMOTE"
    DEMOTE = "DEMOTE"

    @classmethod
    def from_proto(cls, value: int) -> GroupParticipantAction:
        """将 proto 枚举整数（``GROUP_PARTICIPANT_ACTION_*``）映射到本类。"""
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        try:
            name = pb.GroupParticipantUpdateEvent.GroupParticipantAction.Name(value)
        except ValueError:
            # 未知 proto 值（旧客户端 + 新 sidecar 时可能出现），降级为 UNSPECIFIED。
            return cls.UNSPECIFIED
        suffix = name.removeprefix("GROUP_PARTICIPANT_ACTION_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED

    def to_proto(self) -> int:
        """将本枚举映射为 proto 枚举整数。"""
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        return int(
            pb.GroupParticipantUpdateEvent.GroupParticipantAction.Value(
                f"GROUP_PARTICIPANT_ACTION_{self.value}"
            )
        )


@dataclass(frozen=True, slots=True)
class GroupParticipant:
    """群组成员，附带管理员标志。"""

    jid: str
    is_admin: bool = False
    is_super_admin: bool = False

    @classmethod
    def from_proto(cls, msg: _pb.GroupParticipant) -> GroupParticipant:
        return cls(
            jid=msg.jid,
            is_admin=msg.is_admin,
            is_super_admin=msg.is_super_admin,
        )


@dataclass(frozen=True, slots=True)
class GroupInfo:
    """群组的不可变快照。

    ``creation_timestamp`` 由 sidecar 以 ``int64`` 秒为单位发送（0 表示未知）；
    我们将其规整为带时区的 ``datetime | None``。

    ``membership_approval_mode`` 是 sidecar 直接透传的字符串
    （``"on"`` / ``"off"`` / ``""``），原样保留以避免在不熟悉的 WhatsApp
    枚举上做有损转换。
    """

    jid: str
    name: str
    topic: str
    owner_jid: str
    creation_time: datetime | None
    participants: tuple[GroupParticipant, ...]
    is_announce: bool
    is_locked: bool
    is_ephemeral: bool
    ephemeral_duration_seconds: int
    membership_approval_mode: str

    @classmethod
    def from_proto(cls, msg: _pb.GroupInfo) -> GroupInfo:
        ct: datetime | None = None
        if msg.HasField("creation_timestamp"):
            ct = msg.creation_timestamp.ToDatetime(tzinfo=UTC)
        return cls(
            jid=msg.jid,
            name=msg.name,
            topic=msg.topic,
            owner_jid=msg.owner_jid,
            creation_time=ct,
            participants=tuple(GroupParticipant.from_proto(p) for p in msg.participants),
            is_announce=msg.is_announce,
            is_locked=msg.is_locked,
            is_ephemeral=msg.is_ephemeral,
            ephemeral_duration_seconds=msg.ephemeral_duration_seconds,
            membership_approval_mode=msg.membership_approval_mode,
        )


@dataclass(frozen=True, slots=True)
class GroupParticipantUpdateResult:
    """单个成员更新的逐条结果。

    ``error_code`` 为 sidecar 透传的 whatsmeow 数值错误码字符串
    （例如 ``"403"`` / ``"409"``）；``success=True`` 时为空。
    """

    jid: str
    success: bool
    error_code: str = ""
    error_message: str = ""

    @classmethod
    def from_proto(
        cls, msg: _pb.GroupParticipantUpdateResult
    ) -> GroupParticipantUpdateResult:
        return cls(
            jid=msg.jid,
            success=msg.success,
            error_code=msg.error_code,
            error_message=msg.error_message,
        )


# ---------------------------------------------------------------------------
# Group events（Phase 4.1）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JoinedGroupEvent(_EventBase):
    """当前账号刚刚加入或被加入到一个新群组。"""

    group_info: GroupInfo
    join_reason: str = ""
    """加入原因（sidecar 透传，可能为空字符串）。"""


@dataclass(frozen=True, slots=True)
class GroupInfoEvent(_EventBase):
    """已加入群组的元数据发生变化（名称 / 话题 / 设置）。"""

    group_info: GroupInfo


@dataclass(frozen=True, slots=True)
class GroupParticipantUpdateEvent(_EventBase):
    """已加入群组的成员构成发生变化。"""

    group_jid: str
    action: GroupParticipantAction
    participant_jids: tuple[str, ...]
    actor_jid: str = ""
    """触发该变更的成员 JID；可能为空（系统动作）。"""


@dataclass(frozen=True, slots=True)
class UnknownEvent(_EventBase):
    """sidecar 发出但 FastMeow 没有对应类型包装器的事件。
    提供此事件是为了让用户可以检查/记录非预期的流量。
    """

    go_type: str
    """底层 whatsmeow 事件的 Go 类型名称，例如 ``*events.HistorySync``。"""


# ---------------------------------------------------------------------------
# Receipts & Presence（Phase 4.2 软状态事件）
#
# 这三个事件高频且对多数业务无关，因此默认不递送：调用方需在
# 至少一个 router 上注册 on_receipt / on_presence / on_chat_presence
# 处理器；FastMeow 据此 introspect 并向 sidecar 设置
# ``StreamEventsRequest.include_soft_events=True``。
# ---------------------------------------------------------------------------


class ReceiptType(StrEnum):
    """消息回执类型。

    值镜像 ``fastmeow.v1.ReceiptType``，使用纯字符串便于日志/序列化。
    出站方向（:meth:`fastmeow.AccountClient.mark_read` 等）只接受
    ``DELIVERED`` / ``READ`` / ``PLAYED`` / ``SERVER``；UNSPECIFIED
    保留给入站事件未知值的兜底标记位。
    """

    UNSPECIFIED = "UNSPECIFIED"
    DELIVERED = "DELIVERED"
    READ = "READ"
    PLAYED = "PLAYED"
    SERVER = "SERVER"

    @classmethod
    def from_proto(cls, value: int) -> ReceiptType:
        """将 proto 枚举整数（``RECEIPT_TYPE_*``）映射到本类。"""
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        try:
            name = pb.ReceiptType.Name(value)
        except ValueError:
            return cls.UNSPECIFIED
        suffix = name.removeprefix("RECEIPT_TYPE_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED

    def to_proto(self) -> int:
        """将本枚举映射为 proto 枚举整数。"""
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        return int(pb.ReceiptType.Value(f"RECEIPT_TYPE_{self.value}"))


class PresenceType(StrEnum):
    """全局在线状态。仅用于出站
    :meth:`fastmeow.AccountClient.send_presence`；入站
    :class:`PresenceEvent` 单独使用 ``unavailable: bool``。
    """

    UNSPECIFIED = "UNSPECIFIED"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"

    @classmethod
    def from_proto(cls, value: int) -> PresenceType:
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        try:
            name = pb.PresenceType.Name(value)
        except ValueError:
            return cls.UNSPECIFIED
        suffix = name.removeprefix("PRESENCE_TYPE_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED

    def to_proto(self) -> int:
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        return int(pb.PresenceType.Value(f"PRESENCE_TYPE_{self.value}"))


class ChatPresenceState(StrEnum):
    """会话级在线状态（"正在输入" / "已暂停"）。"""

    UNSPECIFIED = "UNSPECIFIED"
    COMPOSING = "COMPOSING"
    PAUSED = "PAUSED"

    @classmethod
    def from_proto(cls, value: int) -> ChatPresenceState:
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        try:
            name = pb.ChatPresenceState.Name(value)
        except ValueError:
            return cls.UNSPECIFIED
        suffix = name.removeprefix("CHAT_PRESENCE_STATE_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED

    def to_proto(self) -> int:
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        return int(pb.ChatPresenceState.Value(f"CHAT_PRESENCE_STATE_{self.value}"))


class ChatPresenceMedia(StrEnum):
    """会话级在线状态附带的媒体类型（文字 / 录音）。

    ``UNSPECIFIED`` 在出站方向被 sidecar 兜底为 ``TEXT`` —— 与
    whatsmeow 「空字符串等同 text」 的约定保持一致。
    """

    UNSPECIFIED = "UNSPECIFIED"
    TEXT = "TEXT"
    AUDIO = "AUDIO"

    @classmethod
    def from_proto(cls, value: int) -> ChatPresenceMedia:
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        try:
            name = pb.ChatPresenceMedia.Name(value)
        except ValueError:
            return cls.UNSPECIFIED
        suffix = name.removeprefix("CHAT_PRESENCE_MEDIA_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED

    def to_proto(self) -> int:
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        return int(pb.ChatPresenceMedia.Value(f"CHAT_PRESENCE_MEDIA_{self.value}"))


@dataclass(frozen=True, slots=True)
class ReceiptEvent(_EventBase):
    """收到一条或多条消息的送达 / 已读 / 已播放回执。

    回执是「软」事件：默认不递送。仅当至少一个 router 注册了
    ``on_receipt`` 处理器时，FastMeow 才会请求 sidecar 转发。
    """

    chat_jid: str
    sender_jid: str
    """触发回执的 WhatsApp 用户 JID（已读消息的人）。"""
    message_ids: tuple[str, ...]
    """一次回执可同时覆盖多条消息（whatsmeow 行为）。"""
    receipt_type: ReceiptType
    timestamp: datetime | None = None
    """WhatsApp 服务器时间戳（UTC，包含时区信息）。可能为 None。"""


@dataclass(frozen=True, slots=True)
class PresenceEvent(_EventBase):
    """联系人的全局在线状态发生变化。

    ``unavailable`` 为 True 表示对方现已离线；False 表示在线。
    ``last_seen`` 仅在对方允许公开「最后在线时间」时被填充。
    """

    from_jid: str
    """状态变化的联系人 JID。"""
    unavailable: bool
    last_seen: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChatPresenceEvent(_EventBase):
    """对方在某一具体会话中正在输入 / 暂停 / 录音。"""

    chat_jid: str
    sender_jid: str
    state: ChatPresenceState
    media: ChatPresenceMedia


# ---------------------------------------------------------------------------
# 媒体（Phase 4.3）
#
# Media 是出站描述符，用户构造后传给 ``AccountClient.send_media`` 等便捷方法；
# MediaInfo 是入站快照，由派发器从 ``MediaMessageEvent`` 折叠而来，挂载到
# ``MessageEvent.media`` 上。两者刻意分离：出站只有 *源 + 元信息*，入站则
# 含完整的下载凭据（direct_path / media_key / file_sha256 / file_enc_sha256）
# 以便后续无状态下载。
# ---------------------------------------------------------------------------


# 出站媒体的合法源类型：
#   * ``bytes`` / ``bytearray`` / ``memoryview`` —— 内存缓冲。
#   * ``str`` / ``os.PathLike[str]`` —— 本地文件路径，按二进制流读取。
# 不接受文件句柄 / 异步迭代器；保持 API 极简，所有传输由 ``_media`` 内部完成。
type MediaSource = bytes | bytearray | memoryview | str | os.PathLike[str]


class MediaKind(StrEnum):
    """媒体载荷的类别。

    值镜像 ``fastmeow.v1.MediaKind``，使用纯字符串便于日志 / 序列化。
    出站方向（:class:`Media`）必须显式声明 ``kind``；``UNSPECIFIED`` 仅作为
    入站未知值的兜底标记位，发送时拒收。
    """

    UNSPECIFIED = "UNSPECIFIED"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    DOCUMENT = "DOCUMENT"
    STICKER = "STICKER"

    @classmethod
    def from_proto(cls, value: int) -> MediaKind:
        """将 proto 枚举整数（``MEDIA_KIND_*``）映射到本类。"""
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        try:
            name = pb.MediaKind.Name(value)
        except ValueError:
            return cls.UNSPECIFIED
        suffix = name.removeprefix("MEDIA_KIND_")
        try:
            return cls(suffix)
        except ValueError:
            return cls.UNSPECIFIED

    def to_proto(self) -> _pb.MediaKind:
        """将本枚举映射为 proto 枚举值。"""
        from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb

        return cast("_pb.MediaKind", pb.MediaKind.Value(f"MEDIA_KIND_{self.value}"))


@dataclass(frozen=True, slots=True)
class Media:
    """出站媒体描述符。

    用户负责声明 ``kind`` 与 ``mime_type``；FastMeow 不做 MIME 嗅探，避免引入
    隐式依赖。``source`` 既可以是已加载的字节缓冲，也可以是文件路径 ——
    路径形式由 :mod:`fastmeow._media` 以二进制流方式增量读取，单文件 O(1) 内存。

    ``thumbnail`` 在 ``IMAGE`` / ``VIDEO`` / ``STICKER`` 上有效，作为对端
    预览图（JPEG）；其他 kind 应保持为空。``voice_note`` 仅 ``AUDIO`` 有意义，
    标记为 PTT（push-to-talk）。
    """

    kind: MediaKind
    source: MediaSource
    mime_type: str
    file_name: str = ""
    """``DOCUMENT`` 推荐填写；其余 kind 留空即可。"""
    caption: str = ""
    """正文说明；``AUDIO`` / ``STICKER`` 上不会被 WhatsApp 显示，建议留空。"""
    thumbnail: bytes = b""
    """对端预览缩略图（JPEG）。仅 ``IMAGE`` / ``VIDEO`` / ``STICKER`` 有效。"""
    voice_note: bool = False
    """仅 ``AUDIO`` 生效；为 True 时标记为语音消息（PTT）。"""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """入站媒体快照（不可变）。

    包含完整的下载凭据，可在任意时刻原样回传给 :meth:`AccountClient.download_media`
    / ``download_media_to``，不需要保留原始 ``MessageEvent`` 引用。
    """

    kind: MediaKind
    mime_type: str
    size: int
    """已声明的明文字节数（来自 wire ``file_length``）。"""
    direct_path: str
    media_key: bytes
    file_sha256: bytes
    file_enc_sha256: bytes
    caption: str = ""
    file_name: str = ""
    thumbnail: bytes = b""
    """对端发来的预览缩略图（JPEG，来自 wire ``thumbnail_jpeg``）。"""
    width: int = 0
    height: int = 0
    duration_seconds: int = 0
    voice_note: bool = False
    animated: bool = False
    """仅 ``STICKER`` 有意义；标记动画贴纸。"""
    url: str = ""

    @classmethod
    def from_proto(cls, msg: _pb.MediaInfo) -> MediaInfo:
        return cls(
            kind=MediaKind.from_proto(msg.kind),
            mime_type=msg.mime_type,
            size=msg.file_length,
            direct_path=msg.direct_path,
            media_key=msg.media_key,
            file_sha256=msg.file_sha256,
            file_enc_sha256=msg.file_enc_sha256,
            caption=msg.caption,
            file_name=msg.file_name,
            thumbnail=msg.thumbnail_jpeg,
            width=msg.width,
            height=msg.height,
            duration_seconds=msg.duration_seconds,
            voice_note=msg.voice_note,
            animated=msg.animated,
            url=msg.url,
        )


# ``Event`` 是所有公开事件的联合类型。处理器可以声明
# ``event: Event`` 来接受任何事件，或使用具体的子类型。
Event = (
    MessageEvent
    | ConnectedEvent
    | DisconnectedEvent
    | PairSuccessEvent
    | LoggedOutEvent
    | QREvent
    | JoinedGroupEvent
    | GroupInfoEvent
    | GroupParticipantUpdateEvent
    | ReceiptEvent
    | PresenceEvent
    | ChatPresenceEvent
    | UnknownEvent
)


# ---------------------------------------------------------------------------
# RPC results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SendResult:
    """成功发送外发消息的结果。"""

    message_id: str
    """WhatsApp 分配的消息 ID；接收者将看到相同的值。"""

    server_timestamp: datetime
    """WhatsApp 服务器时间戳（UTC，包含时区信息）。"""

    deduped: bool
    """如果 sidecar 针对相同的 ``client_msg_id`` 返回了之前缓存的结果
    （在 5 分钟 / 1 万条目的 LRU 窗口内），而不是实际再次发送，则为 True。"""

    @classmethod
    def from_proto(
        cls,
        msg: _pb.SendMessageResponse
        | _pb.SendReactionResponse
        | _pb.SendEditResponse
        | _pb.SendRevokeResponse,
    ) -> SendResult:
        ts = msg.server_timestamp.ToDatetime(tzinfo=UTC)
        return cls(
            message_id=msg.message_id,
            server_timestamp=ts,
            deduped=msg.deduped,
        )


# ---------------------------------------------------------------------------
# 内部辅助函数（不是公开 API 的一部分，但在此定义以便
# 派发器只需一次导入即可构建公开事件）。
# ---------------------------------------------------------------------------


def _ts(msg: _pb.StreamEventsResponse) -> datetime:
    """提取 observed_at 为 UTC 且包含时区信息的 datetime。"""
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
    """将传输中的 ``StreamEventsResponse`` 转换为公开的 :data:`Event`。

    如果响应不包含已识别的 oneof 负载（例如纯心跳包），则返回 ``None``。
    派发器会静默丢弃 ``None``。
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
    if which == "joined_group":
        jg = msg.joined_group
        return JoinedGroupEvent(
            **base,
            group_info=GroupInfo.from_proto(jg.group_info),
            join_reason=jg.join_reason,
        )
    if which == "group_info":
        gi = msg.group_info
        return GroupInfoEvent(
            **base,
            group_info=GroupInfo.from_proto(gi.group_info),
        )
    if which == "group_participant_update":
        gp = msg.group_participant_update
        return GroupParticipantUpdateEvent(
            **base,
            group_jid=gp.group_jid,
            action=GroupParticipantAction.from_proto(gp.action),
            participant_jids=tuple(gp.participant_jids),
            actor_jid=gp.actor_jid,
        )
    if which == "receipt":
        r = msg.receipt
        rts: datetime | None = None
        if r.HasField("timestamp"):
            rts = r.timestamp.ToDatetime(tzinfo=UTC)
        return ReceiptEvent(
            **base,
            chat_jid=r.chat_jid,
            sender_jid=r.sender_jid,
            message_ids=tuple(r.message_ids),
            receipt_type=ReceiptType.from_proto(r.receipt_type),
            timestamp=rts,
        )
    if which == "presence":
        pr = msg.presence
        ls: datetime | None = None
        if pr.HasField("last_seen"):
            ls = pr.last_seen.ToDatetime(tzinfo=UTC)
        return PresenceEvent(
            **base,
            from_jid=pr.from_jid,
            unavailable=pr.unavailable,
            last_seen=ls,
        )
    if which == "chat_presence":
        cp = msg.chat_presence
        return ChatPresenceEvent(
            **base,
            chat_jid=cp.chat_jid,
            sender_jid=cp.sender_jid,
            state=ChatPresenceState.from_proto(cp.state),
            media=ChatPresenceMedia.from_proto(cp.media),
        )
    if which == "media_message":
        mm = msg.media_message
        mts: datetime | None = None
        if mm.HasField("timestamp"):
            mts = mm.timestamp.ToDatetime(tzinfo=UTC)
        return MessageEvent(
            **base,
            message_id=mm.message_id,
            chat_jid=mm.chat_jid,
            sender_jid=mm.sender_jid,
            from_me=mm.from_me,
            is_group=mm.is_group,
            text="",
            reply_to_message_id=mm.reply_to_message_id,
            timestamp=mts,
            caption=mm.caption,
            media=MediaInfo.from_proto(mm.media),
        )
    return None


# 为比 ruff 更严格的工具在 TYPE_CHECKING 下消除未使用导入警告。
_ = field
