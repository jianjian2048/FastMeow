"""FastMeow 的公开异常层级。

所有 FastMeow 错误均派生自 :class:`FastMeowError`。
捕获这一个类型就足以处理库抛出的所有异常。
"""

from __future__ import annotations


class FastMeowError(Exception):
    """所有 FastMeow 抛出的异常的基类。"""


# ---------------------------------------------------------------------------
# 配置 / 启动
# ---------------------------------------------------------------------------


class ConfigurationError(FastMeowError):
    """当 :class:`fastmeow.FastMeow` 应用配置错误时抛出。

    示例：重复的 ``account_key``、缺少必需参数、无效的 ``on_qr`` 选择器。
    """


class SidecarBinaryNotFoundError(FastMeowError):
    """当找不到内置的 Go sidecar 二进制文件时抛出。

    Wheel 包中包含 ``fastmeow/_bin/<os>-<arch>/fastmeow-sidecar(.exe)``。
    在开发模式下，我们会回退到相对于项目根目录的 ``./bin/fastmeow-sidecar(.exe)``。
    """


class ManifestError(ConfigurationError):
    """当磁盘上的账号 manifest 清单损坏、被另一个进程锁定或与 sidecar 的会话存储不一致时抛出。

    是 :class:`ConfigurationError` 的子类，因为所有诱因都是安装时或持久化状态问题，而非瞬时的运行时故障。
    """


# ---------------------------------------------------------------------------
# sidecar 生命周期
# ---------------------------------------------------------------------------


class SidecarError(FastMeowError):
    """sidecar 进程 / 传输失败的基类。"""


class SidecarStartupError(SidecarError):
    """sidecar 进程在启动期间退出或拒绝连接。"""


class SidecarCrashedError(SidecarError):
    """sidecar 进程在运行期间意外终止。"""


class TransportError(SidecarError):
    """gRPC 通道 / 流层面的失败。"""


# ---------------------------------------------------------------------------
# 账号 / 配对
# ---------------------------------------------------------------------------


class AccountError(FastMeowError):
    """账号管理失败的基类。"""


class AccountAlreadyExistsError(AccountError):
    """当使用相同的 key 两次调用 ``add_account`` 时抛出。"""


class AccountNotFoundError(AccountError):
    """当操作引用了未知的 ``account_key`` 时抛出。"""


class PairingTimeoutError(AccountError):
    """用户未在配置的超时时间内扫描二维码。"""


class PairingFailedError(AccountError):
    """WhatsApp 拒绝配对（通常是二维码过期或达到设备限制）。"""


# ---------------------------------------------------------------------------
# 消息发送
# ---------------------------------------------------------------------------


class MessagingError(FastMeowError):
    """外发消息失败的基类。"""


class InvalidJIDError(MessagingError):
    """提供的 JID 无法解析或属于不受支持的服务器。"""


class MessageSendError(MessagingError):
    """sidecar 拒绝交付外发消息。"""


# ---------------------------------------------------------------------------
# 处理器派发
# ---------------------------------------------------------------------------


class DispatchError(FastMeowError):
    """处理器注册 / 派发失败的基类。"""


class HandlerSignatureError(DispatchError):
    """处理器声明了 FastMeow 无法注入的参数。

    Phase 1 支持的参数名称 / 注解包括：``msg`` / ``event``（任何具体的事件类）、
    ``ctx`` (:class:`fastmeow.Ctx`)、``qr`` (:class:`fastmeow.QREvent`) 以及
    ``match``（正则匹配对象，在正则过滤器外为 ``None``）。
    未知的参数会导致注册立即失败。
    """


class ReplyNotAvailableError(DispatchError):
    """在 :class:`MessageEvent` 处理器之外访问了 ``ctx.reply``。

    ``reply`` 是一个自动定位到入站聊天的便捷方法。
    其他事件类型（Connected, Disconnected, QR 等）没有入站聊天，
    因此 ``reply`` 被刻意设计为不可用。请改用 ``ctx.client.send_text(jid, text)``。
    """


class BackpressureError(DispatchError):
    """当单个账号的事件队列溢出时抛出。

    在 0.1.0 版本中，每个流式事件都是关键的（message, qr, pair_success,
    connected, disconnected, logged_out），因此静默丢弃旧事件会导致用户状态脱节。
    派发器改为采取尽早失败策略：它会记录详细日志、设置全局停止标志，
    并在受影响的路径上抛出此异常，使应用显式停止。一旦引入非关键事件类，可能会恢复选择性丢弃机制。
    """


class ContextOperationNotAvailableError(DispatchError):
    """在不适用的事件上下文中调用了仅对特定事件类型可用的 ``Ctx`` 方法。

    例如 ``ctx.send_typing()`` / ``ctx.mark_read()`` / 群组捷径方法只在
    :class:`MessageEvent` 派发上下文中可用；在 Connected / QR / Disconnected
    等派发中调用应该立即失败而非访问 ``ctx.client``，以便用户尽早看到错误。

    此异常与 :class:`ReplyNotAvailableError` 同级（都是 ``DispatchError``
    子类）；区别仅在于错误信息更通用，覆盖第 4 阶段引入的所有上下文受限方法。
    """


# ---------------------------------------------------------------------------
# 群组（Phase 4.1）
# ---------------------------------------------------------------------------


class GroupError(FastMeowError):
    """群组操作失败的基类。

    所有 9 个群组 RPC（list/get/preview/join/leave/create/update_settings/
    update_participants/get_invite_link）的失败都最终落到此类或其子类。
    sidecar 用 gRPC 状态码区分细类，Python 端根据 ``op`` + status code
    + detail 关键词进一步细化（参见 ``_transport._translate``）。
    """


class InviteLinkInvalidError(GroupError):
    """邀请链接 / 邀请码格式非法或不被 WhatsApp 接受。

    对应 sidecar ``ErrInviteLinkInvalid`` → ``INVALID_ARGUMENT`` + detail
    中含 "invite"。
    """


class InviteLinkRevokedError(GroupError):
    """邀请链接已被群管理员撤销 / 重置。

    对应 sidecar ``ErrInviteLinkRevoked`` → ``NOT_FOUND`` + detail 中含
    "invite"。
    """


class GroupPermissionError(GroupError):
    """当前账号无权执行该群组操作。

    例如非管理员尝试改群名 / 加成员，或被踢出群后尝试操作。对应 sidecar
    ``ErrNotInGroup`` / ``ErrGroupInviteLinkUnauthorized`` →
    ``PERMISSION_DENIED``。
    """
