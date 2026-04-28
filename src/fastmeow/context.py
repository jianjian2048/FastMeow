"""传递给用户处理器的每个事件的上下文对象。

FastMeow 中的处理器具有以下签名之一：:

    @router.message()
    async def echo(msg: MessageEvent, ctx: Ctx) -> None: ...

    @router.message()
    async def shorthand(msg: MessageEvent) -> None: ...

    @router.connected()
    async def on_up(ctx: Ctx) -> None: ...

:class:`Ctx` 封装了处理器*针对此特定事件*可能需要的所有内容，
而无需处理器记住全局变量或从应用对象中搜寻状态：

* ``account_key`` — 派发此事件的账号。
* ``account_jid`` — 观察到事件时该账号的 JID。
* ``event``       — 公开事件对象（当处理器同时接受两个参数时，与绑定到 ``msg`` 的对象相同）。
* ``client``      — 绑定到 ``account_key`` 的 :class:`AccountClient`；
                    用它来发送任意消息或执行群组管理操作。
* ``reply()``     — 针对非常常见的“在同一聊天中回复”情况的便捷方法。
                    **仅**在事件为 :class:`MessageEvent` 时可用；
                    否则会抛出 :class:`ReplyNotAvailableError`，
                    从而使失败立即且显而易见，而不是随后抛出晦涩的 ``AttributeError``。

Ctx 实例由派发器创建，并在处理器返回后销毁。
它们不适合存储起来在处理器退出后使用 — 底层客户端可能随时被拆除。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fastmeow.exceptions import ReplyNotAvailableError
from fastmeow.types import (
    ChatPresenceMedia,
    ChatPresenceState,
    Event,
    GroupInfo,
    GroupParticipantUpdateResult,
    Media,
    MediaInfo,
    MediaSource,
    MessageEvent,
    PresenceType,
    ReceiptType,
    SendResult,
)

if TYPE_CHECKING:
    pass


__all__ = ["AccountClient", "Ctx"]


class AccountClient(Protocol):
    """:attr:`Ctx.client` 上暴露的账号作用域 RPC 接口。

    定义为 ``Protocol``，以便派发器无需了解 :mod:`fastmeow._transport` 中的具体 gRPC 实现，
    同时也方便测试在不触及网络的情况下替换为模拟对象。
    """

    @property
    def account_key(self) -> str: ...

    @property
    def jid(self) -> str:
        """此账号当前的 JID；如果未配对则为空字符串。"""
        ...

    async def send_text(
        self,
        to_jid: str,
        body: str,
        *,
        client_msg_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        """发送纯文本消息。

        ``client_msg_id`` 用于幂等性。如果不传递，FastMeow 将生成一个 UUIDv4。
        在 sidecar 的去重窗口（5 分钟 / 1 万条目）内传递相同的 ID，
        将返回先前的 :class:`SendResult` 并设置 ``deduped=True``，而不是重新发送。
        """
        ...

    # -- 群组（Phase 4.1） ---------------------------------------------

    async def list_groups(self) -> tuple[GroupInfo, ...]:
        """列出此账号已加入的所有群组。"""
        ...

    async def get_group_info(self, group_jid: str) -> GroupInfo:
        """获取群组的元数据快照。"""
        ...

    async def preview_group_invite(self, invite_link_or_code: str) -> GroupInfo:
        """预览邀请链接（或裸邀请码）背后的群组而不加入。

        接受完整 URL（``https://chat.whatsapp.com/<code>``）或裸邀请码；
        sidecar 自行归一化。
        """
        ...

    async def join_group(self, invite_link_or_code: str) -> str:
        """通过邀请链接 / 邀请码加入群组，返回群组 JID。"""
        ...

    async def leave_group(self, group_jid: str) -> None:
        """退出群组。"""
        ...

    async def create_group(
        self, name: str, participants: Sequence[str] = ()
    ) -> GroupInfo:
        """创建新群组并初始化成员（不含创建者；creator 自动加入）。"""
        ...

    async def set_group_name(self, group_jid: str, name: str) -> GroupInfo: ...

    async def set_group_topic(self, group_jid: str, topic: str) -> GroupInfo: ...

    async def set_group_announce(
        self, group_jid: str, announce: bool
    ) -> GroupInfo:
        """设置群组是否为“仅管理员发言”模式。"""
        ...

    async def set_group_locked(self, group_jid: str, locked: bool) -> GroupInfo:
        """设置群组是否锁定群信息编辑（仅管理员可改名 / 改头像 / 改话题）。"""
        ...

    async def add_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]: ...

    async def remove_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]: ...

    async def promote_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]: ...

    async def demote_group_participants(
        self, group_jid: str, jids: Sequence[str]
    ) -> tuple[GroupParticipantUpdateResult, ...]: ...

    async def get_group_invite_link(
        self, group_jid: str, *, revoke: bool = False
    ) -> str:
        """获取群组邀请链接。``revoke=True`` 时撤销旧链接并生成新链接。"""
        ...

    # -- 回执 / 在线状态（Phase 4.2） --------------------------------

    async def mark_read(
        self,
        chat_jid: str,
        sender_jid: str,
        message_ids: Sequence[str],
        *,
        receipt_type: ReceiptType = ReceiptType.READ,
        read_at: datetime | None = None,
    ) -> None:
        """对一批消息发送已读 / 已播放回执。

        ``sender_jid`` 在单聊中等同于 ``chat_jid``，在群聊中是消息原作者。
        ``read_at=None`` 时由 sidecar 使用当前时间。
        """
        ...

    async def send_presence(self, presence: PresenceType) -> None:
        """发送账号级在线状态（``AVAILABLE`` / ``UNAVAILABLE``）。

        通常在登录后 / 应用切回前台时调用一次 ``AVAILABLE``。
        """
        ...

    async def send_chat_presence(
        self,
        chat_jid: str,
        state: ChatPresenceState,
        *,
        media: ChatPresenceMedia = ChatPresenceMedia.TEXT,
    ) -> None:
        """发送会话级输入状态（"正在输入" / "已暂停"等）。

        ``state=COMPOSING`` 时通常配合 ``media=TEXT`` 或 ``AUDIO``；
        ``state=PAUSED`` 时 ``media`` 字段被服务器忽略，但仍需提供。
        """
        ...

    async def subscribe_presence(self, jid: str) -> None:
        """订阅指定联系人的在线状态推送。

        订阅后 sidecar 会以 :class:`PresenceEvent` 形式异步推送对方的
        在线 / 最后一次在线时间变化。需先注册 ``@router.on_presence()`` 处理器
        才能收到事件。
        """
        ...

    # -- 媒体（Phase 4.3） ---------------------------------------------
    # 设计要点：
    # * 类型化的 send_image / send_video / send_audio / send_document /
    #   send_sticker 是“便捷外壳”——它们各自构造合适的 :class:`Media`
    #   并最终走到 :meth:`send_media`。这使得 SDK 用户无需手工记忆
    #   ``MediaKind`` 与 mime/sticker/voice 的细节，符合 plan §2.2 的
    #   显式 UX 偏好。
    # * 通用 :meth:`send_media` 仍保留为高级逃生口（自定义 mime、
    #   传 ``Media`` 已构造完成等场景）。

    async def send_media(
        self,
        to_jid: str,
        media: Media,
        *,
        client_msg_id: str | None = None,
        quoted_message_id: str | None = None,
    ) -> SendResult:
        """发送任意 :class:`Media`；通用底层方法。

        ``client_msg_id`` 用于上传幂等；不传时由 SDK 自动生成 UUIDv4。
        """
        ...

    async def send_image(
        self,
        to_jid: str,
        source: MediaSource,
        *,
        caption: str = "",
        mime_type: str | None = None,
        thumbnail: bytes | None = None,
        client_msg_id: str | None = None,
        quoted_message_id: str | None = None,
    ) -> SendResult:
        """发送图片消息。

        ``source`` 接受 ``bytes`` / ``bytearray`` / ``memoryview`` /
        路径字符串 / ``os.PathLike``；从路径读取时会启用线程池避免阻塞事件循环。
        ``mime_type`` 不传时根据扩展名推断（路径输入），bytes 输入未指定时
        默认 ``image/jpeg``。
        """
        ...

    async def send_video(
        self,
        to_jid: str,
        source: MediaSource,
        *,
        caption: str = "",
        mime_type: str | None = None,
        thumbnail: bytes | None = None,
        client_msg_id: str | None = None,
        quoted_message_id: str | None = None,
    ) -> SendResult:
        """发送视频消息。常规 mime 默认为 ``video/mp4``。"""
        ...

    async def send_audio(
        self,
        to_jid: str,
        source: MediaSource,
        *,
        mime_type: str | None = None,
        voice_note: bool = False,
        client_msg_id: str | None = None,
        quoted_message_id: str | None = None,
    ) -> SendResult:
        """发送音频消息。

        ``voice_note=True`` 走 PTT（push-to-talk）通道；mime 默认为
        ``audio/ogg; codecs=opus``（语音条）或 ``audio/mpeg``（普通音频）。
        """
        ...

    async def send_document(
        self,
        to_jid: str,
        source: MediaSource,
        *,
        file_name: str | None = None,
        caption: str = "",
        mime_type: str | None = None,
        client_msg_id: str | None = None,
        quoted_message_id: str | None = None,
    ) -> SendResult:
        """发送文档。``file_name`` 缺省时用路径 basename；bytes 输入必须显式指定。"""
        ...

    async def send_sticker(
        self,
        to_jid: str,
        source: MediaSource,
        *,
        mime_type: str | None = None,
        client_msg_id: str | None = None,
        quoted_message_id: str | None = None,
    ) -> SendResult:
        """发送贴纸（缺省 mime 为 ``image/webp``）。"""
        ...

    async def download_media(self, media: MediaInfo) -> bytes:
        """把 ``MessageEvent.media`` 描述的入站媒体下载到内存。

        小到中等尺寸适用；大文件优先用 :meth:`download_media_to`。
        """
        ...

    async def download_media_to(
        self, media: MediaInfo, path: str | Path
    ) -> Path:
        """流式下载到 ``path`` 并返回最终路径。父目录必须已存在。"""
        ...


@dataclass(frozen=True, slots=True)
class Ctx:
    """分发给用户处理器的每个事件的上下文。

    Args:
        account_key: 账号的稳定用户选择 ID。
        account_jid: 此事件发生时的 WhatsApp JID；在配对前的短暂窗口内可能为空。
        event: 正在派发的公开事件。
        client: 账号作用域的 RPC 接口；参见 :class:`AccountClient`。

    响应 :class:`MessageEvent` 时请使用 :meth:`reply`。
    对于任何其他外发流量（广播、跨聊天回复、定时消息等），请使用 :meth:`send`。
    群组管理操作请使用 :attr:`client`（例如 ``ctx.client.set_group_name(...)``）。
    """

    account_key: str
    account_jid: str
    event: Event
    client: AccountClient

    # -- 便捷方法 -------------------------------------------------------

    async def reply(
        self,
        text: str,
        *,
        quoted: bool = True,
        client_msg_id: str | None = None,
    ) -> SendResult:
        """在与收到的消息相同的聊天中进行回复。

        ``quoted=True``（默认值）会将收到的 ``message_id`` 作为引用附加。
        设置 ``quoted=False`` 则进行普通的后续回复。

        Raises:
            ReplyNotAvailableError: 如果此 Ctx 是为非消息事件（例如 ``ConnectedEvent``）构建的。
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
        """使用此上下文的账号向任意 JID 发送文本消息。

        等同于 ``ctx.client.send_text(to_jid, text, ...)``，
        但为了对称性，其拼写方式与 :meth:`reply` 相同，
        这样处理器在“在此处回复”和“通知其他人”之间切换时，就无需混合两种调用风格。
        """
        return await self.client.send_text(
            to_jid,
            text,
            client_msg_id=client_msg_id,
        )

    # -- 媒体便捷方法（Phase 4.3 Batch 5） ----------------------------------
    # ``reply_*`` 系列与 :meth:`reply` 形状对齐：仅 :class:`MessageEvent`
    # 上下文允许调用，缺省自动引用源消息（quoted=True 行为内置），底层一律
    # 走 ``ctx.client.send_<kind>``。这保证了 ``ctx.reply()`` 与
    # ``ctx.reply_image()`` 在语义上完全可互换 —— 用户不必记住两个心智模型。
    #
    # 下载侧（:meth:`download_media` / :meth:`download_media_to`）则不做事件类型
    # 守卫：插件代码常在非消息事件里下载先前缓存的 :class:`MediaInfo`（例如
    # 后台任务），而上下文本身只是 :class:`AccountClient` 的薄包装。

    def _require_message_event(self, op: str) -> MessageEvent:
        if not isinstance(self.event, MessageEvent):
            raise ReplyNotAvailableError(
                f"ctx.{op}() is only available for MessageEvent handlers; "
                f"this ctx wraps {type(self.event).__name__}. "
                f"Use ctx.client.send_<kind>(jid, ...) instead."
            )
        return self.event

    async def reply_image(
        self,
        source: MediaSource,
        *,
        caption: str = "",
        mime_type: str | None = None,
        thumbnail: bytes | None = None,
        client_msg_id: str | None = None,
        quoted: bool = True,
    ) -> SendResult:
        """以图片回复源消息。``quoted=True`` 默认引用源消息。"""
        msg = self._require_message_event("reply_image")
        return await self.client.send_image(
            msg.chat_jid,
            source,
            caption=caption,
            mime_type=mime_type,
            thumbnail=thumbnail,
            client_msg_id=client_msg_id,
            quoted_message_id=msg.message_id if quoted else None,
        )

    async def reply_video(
        self,
        source: MediaSource,
        *,
        caption: str = "",
        mime_type: str | None = None,
        thumbnail: bytes | None = None,
        client_msg_id: str | None = None,
        quoted: bool = True,
    ) -> SendResult:
        """以视频回复源消息。"""
        msg = self._require_message_event("reply_video")
        return await self.client.send_video(
            msg.chat_jid,
            source,
            caption=caption,
            mime_type=mime_type,
            thumbnail=thumbnail,
            client_msg_id=client_msg_id,
            quoted_message_id=msg.message_id if quoted else None,
        )

    async def reply_audio(
        self,
        source: MediaSource,
        *,
        mime_type: str | None = None,
        voice_note: bool = False,
        client_msg_id: str | None = None,
        quoted: bool = True,
    ) -> SendResult:
        """以音频回复源消息；``voice_note=True`` 走 PTT 通道。"""
        msg = self._require_message_event("reply_audio")
        return await self.client.send_audio(
            msg.chat_jid,
            source,
            mime_type=mime_type,
            voice_note=voice_note,
            client_msg_id=client_msg_id,
            quoted_message_id=msg.message_id if quoted else None,
        )

    async def reply_document(
        self,
        source: MediaSource,
        *,
        file_name: str | None = None,
        caption: str = "",
        mime_type: str | None = None,
        client_msg_id: str | None = None,
        quoted: bool = True,
    ) -> SendResult:
        """以文档回复源消息。bytes 输入需显式指定 ``file_name``。"""
        msg = self._require_message_event("reply_document")
        return await self.client.send_document(
            msg.chat_jid,
            source,
            file_name=file_name,
            caption=caption,
            mime_type=mime_type,
            client_msg_id=client_msg_id,
            quoted_message_id=msg.message_id if quoted else None,
        )

    async def reply_sticker(
        self,
        source: MediaSource,
        *,
        mime_type: str | None = None,
        client_msg_id: str | None = None,
        quoted: bool = True,
    ) -> SendResult:
        """以贴纸回复源消息（缺省 mime ``image/webp``）。"""
        msg = self._require_message_event("reply_sticker")
        return await self.client.send_sticker(
            msg.chat_jid,
            source,
            mime_type=mime_type,
            client_msg_id=client_msg_id,
            quoted_message_id=msg.message_id if quoted else None,
        )

    async def download_media(self, media: MediaInfo) -> bytes:
        """下载入站媒体到内存；任意上下文可用。"""
        return await self.client.download_media(media)

    async def download_media_to(
        self, media: MediaInfo, path: str | Path
    ) -> Path:
        """流式下载入站媒体到 ``path``；父目录必须已存在。"""
        return await self.client.download_media_to(media, path)

    # -- 内省辅助属性 --------------------------------------------

    @property
    def is_message(self) -> bool:
        return isinstance(self.event, MessageEvent)

    @property
    def message(self) -> MessageEvent:
        """返回缩小为 :class:`MessageEvent` 类型的 ``self.event``。

        为偏好 ``ctx.message.text`` 而非 isinstance 检查的处理器提供便利。
        如果事件不是消息，则抛出 :class:`ReplyNotAvailableError`
        （与 :meth:`reply` 属于同一异常族）— 这保持了失败模式的一致性。
        """
        if not isinstance(self.event, MessageEvent):
            raise ReplyNotAvailableError(
                f"ctx.message is only valid for MessageEvent contexts; "
                f"this ctx wraps {type(self.event).__name__}."
            )
        return self.event
