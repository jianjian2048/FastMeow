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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 避免在模块导入时导入生成的 stub，以便在 wheel 的 _generated/
    # 子包构建之前（例如早期开发期间），``import fastmeow`` 仍能正常工作。
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

    # 便捷谓词 -------------------------------------------------

    @property
    def is_dm(self) -> bool:
        """如果这是 1:1 聊天（即不是群组）则返回 True。"""
        return not self.is_group

    @property
    def is_reply(self) -> bool:
        return bool(self.reply_to_message_id)


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


@dataclass(frozen=True, slots=True)
class UnknownEvent(_EventBase):
    """sidecar 发出但 FastMeow 没有对应类型包装器的事件。
    提供此事件是为了让用户可以检查/记录非预期的流量。
    """

    go_type: str
    """底层 whatsmeow 事件的 Go 类型名称，例如 ``*events.HistorySync``。"""


# ``Event`` 是所有公开事件的联合类型。处理器可以声明
# ``event: Event`` 来接受任何事件，或使用具体的子类型。
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
    """成功发送外发消息的结果。"""

    message_id: str
    """WhatsApp 分配的消息 ID；接收者将看到相同的值。"""

    server_timestamp: datetime
    """WhatsApp 服务器时间戳（UTC，包含时区信息）。"""

    deduped: bool
    """如果 sidecar 针对相同的 ``client_msg_id`` 返回了之前缓存的结果
    （在 5 分钟 / 1 万条目的 LRU 窗口内），而不是实际再次发送，则为 True。"""

    @classmethod
    def from_proto(cls, msg: _pb.SendMessageResponse) -> SendResult:
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
    return None


# 为比 ruff 更严格的工具在 TYPE_CHECKING 下消除未使用导入警告。
_ = field
