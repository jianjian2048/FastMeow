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
]

if TYPE_CHECKING:
    pass


# 第一阶段通信协议版本；当 proto 发生不兼容变更时增加。
PROTOCOL_VERSION = 1

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
