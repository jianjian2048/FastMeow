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
* ``client``      — 绑定到 ``account_key`` 的 :class:`AccountClient`；用它来发送任意消息。
* ``reply()``     — 针对非常常见的“在同一聊天中回复”情况的便捷方法。
                    **仅**在事件为 :class:`MessageEvent` 时可用；
                    否则会抛出 :class:`ReplyNotAvailableError`，
                    从而使失败立即且显而易见，而不是随后抛出晦涩的 ``AttributeError``。

Ctx 实例由派发器创建，并在处理器返回后销毁。
它们不适合存储起来在处理器退出后使用 — 底层客户端可能随时被拆除。
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
