# FastMeow
[English](README.md) | [简体中文](README.zh-CN.md)

Pythonic、原生异步的 WhatsApp 自动化 SDK，由内嵌的 whatsmeow Go sidecar 边车进程驱动。

## 为什么选择 FastMeow
- **零 CGO 工具链**：基于 `CGO_ENABLED=0` 构建，无需在机器上安装 C 编译器或 Go 环境。
- **Sidecar 架构**：将专门的 Go 二进制文件（whatsmeow）作为受管子进程运行，通过高速 gRPC 进行通信。
- **Pythonic API**：受 FastStream 和 aiogram 启发的现代 async/await 设计，支持魔法过滤器和可挂载的路由器。
- **性能**：单机可同时处理 100 多个并发账号。
- **稳定性**：不同于直接的 CGO 绑定，sidecar 进程将 Go 运行时与 Python 解释器隔离。

## 状态
- **Alpha**：项目处于早期开发阶段。
- **阶段 1+2+3**：Go sidecar、Python SDK、多平台发布流水线均已发布并经过验证。
- **阶段 4.1 ✅**：群组管理 —— 9 个 RPC 与 3 个事件。
- **阶段 4.2 ✅**：回执与在线状态 —— 4 个 RPC 与 3 个事件，软事件按需自动开关。
- **CI 已验证**：多平台 wheel 包已通过自动化流水线测试（mypy strict、ruff、186 项测试）。
- **分发**：已发布到 [PyPI](https://pypi.org/project/fastmeow/)（最新版本：`0.2.1`）。

## 安装
```bash
pip install fastmeow
```

支持的平台：Linux (x86_64)、macOS (arm64 12.0+) 和 Windows (x86_64)。需要 Python 3.12+。

## 快速入门
以下示例实现了一个基础的 echo 机器人。第一次运行时，它会在终端打印二维码用于配对。后续运行将复用存储在 `./sessions/` 目录中的会话。

```python
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastmeow import (
    ConnectedEvent,
    Ctx,
    F,
    FastMeow,
    MessageEvent,
    Router,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

router = Router(name="echo")


@router.connected()
async def announce_online(event: ConnectedEvent, ctx: Ctx) -> None:
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message(F.text == "ping")
async def pong(msg: MessageEvent, ctx: Ctx) -> None:
    await ctx.reply("pong")


@router.message(F.is_dm & ~F.from_me)
async def echo(msg: MessageEvent, ctx: Ctx) -> None:
    if not msg.text:
        return
    await ctx.reply(f"echo: {msg.text}")


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")
        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")
        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
```

## 架构
FastMeow 使用 sidecar 边车进程模式将基于 Go 的 `whatsmeow` 库与 Python 连接。

```
┌──────────────────────────────────────────────────────────────────┐
│ 用户 Python 代码                                                 │
│   router = Router(); @router.message(F.text == "ping") ...       │
└─────────────────────────────┬────────────────────────────────────┘
                              │ FastMeow SDK (async, Python 3.12+)
                              │   - 路由器 / 过滤器 / 上下文（Ctx）
                              │   - AccountHandle, 多账号支持
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ 内嵌 Go sidecar  (cmd/fastmeow-sidecar, CGO_ENABLED=0)           │
│   - 每个 FastMeow 应用实例对应一个进程                           │
│   - 本地 gRPC，auth-token 握手                                   │
│   - 封装 go.mau.fi/whatsmeow                                     │
│   - 会话存储在 ./sessions/<account_key>/                         │
└─────────────────────────────┬────────────────────────────────────┘
                              │ WhatsApp Web 协议
                              ▼
                       WhatsApp 服务器
```

这种方法消除了对 Docker 或本地 Go 工具链的需求。sidecar 二进制文件嵌入在平台特定的 Python wheel 包中，并在运行时自动发现。

## 概念

### FastMeow 应用
主要入口点，作为异步上下文管理器使用，用于管理 sidecar 进程和账号连接的生命周期。

### 路由器 (Router)
用于组织处理器。路由器可以挂载到主应用或其他路由器中，从而实现模块化的机器人设计。

### F 魔法过滤器
用于事件过滤的强大且可组合的工具。支持逻辑运算符如 `&` (与)、`|` (或) 和 `~` (非)。过滤器按顺序求值，第一个匹配成功的胜出。

### 上下文（Ctx）
传递给每个处理器的上下文对象。它携带了 `account_key`、`account_jid` 以及像 `ctx.reply()` 这样用于快速回复的辅助方法。

### AccountHandle
由 `app.add_account()` 返回。它提供了检查连接状态 (`.ready()`) 和检索账号信息（如 JID）的方法。

### 多账号支持
FastMeow 支持在单个应用实例中管理多个 WhatsApp 账号。每个账号在各自隔离的会话目录中运行。

### 二维码配对
在 `add_account` 中设置 `on_qr="terminal"` 以打印用于初始配对的二维码。会话数据持久化在本地，以便在后续启动时自动重连。

## 多账号示例
使用单个路由器管理多个身份：

```python
async with FastMeow(session_dir=session_dir) as app:
    app.include_router(router)
    
    # Alice 和 Bob 共享相同的消息处理器
    alice = app.add_account("alice", on_qr="terminal")
    bob = app.add_account("bob", on_qr="terminal")
    
    await asyncio.gather(alice.ready(), bob.ready())
    await app.run_forever()
```
在处理器内部，`ctx.account_key` 将根据哪个账号接收到事件而成为 "alice" 或 "bob"。

## 公开 API 一览
`fastmeow` 的顶级导出项：

- **核心 (Core)**：`FastMeow`, `AccountHandle`
- **路由 (Routing)**：`Router`, `SkipHandler`, `F`, `Filter`, `FilterResult`
- **上下文 (Context)**：`Ctx`, `AccountClient`
- **事件 (Events)**：`Event`, `MessageEvent`, `ConnectedEvent`, `DisconnectedEvent`, `QREvent`, `PairSuccessEvent`, `LoggedOutEvent`, `UnknownEvent`, `GroupInfoEvent`, `JoinedGroupEvent`, `GroupParticipantUpdateEvent`, `ReceiptEvent`, `PresenceEvent`, `ChatPresenceEvent`
- **领域 (Domain)**：`Account`, `AccountState`, `SendResult`, `GroupInfo`, `GroupParticipant`, `GroupParticipantAction`, `GroupParticipantUpdateResult`, `ReceiptType`, `PresenceType`, `ChatPresenceState`, `ChatPresenceMedia`
- **异常 (Exceptions)**：`FastMeowError`, `ConfigurationError`, `AccountError`, `AccountAlreadyExistsError`, `AccountNotFoundError`, `MessagingError`, `MessageSendError`, `InvalidJIDError`, `ReplyNotAvailableError`, `PairingFailedError`, `PairingTimeoutError`, `SidecarError`, `SidecarStartupError`, `SidecarCrashedError`, `SidecarBinaryNotFoundError`, `TransportError`, `ManifestError`, `DispatchError`, `BackpressureError`, `HandlerSignatureError`, `GroupError`, `GroupNotFoundError`, `NotInGroupError`, `NotGroupAdminError`, `InviteLinkInvalidError`, `InviteLinkRevokedError`

`AccountClient`（通过 `handle.client` 或 `ctx.client` 获取）提供：

- **消息**：`send_text`、`send_reply`、`mark_read`
- **在线状态**：`send_presence`、`send_chat_presence`、`subscribe_presence`
- **群组（只读）**：`list_groups`、`get_group_info`、`preview_group_invite`
- **群组（写）**：`create_group`、`set_group_name`、`set_group_topic`、`set_group_announce`、`set_group_locked`、`add_group_participants`、`remove_group_participants`、`promote_group_participants`、`demote_group_participants`、`get_group_invite_link`、`join_group`、`leave_group`

## 支持的平台
FastMeow 为以下平台提供预编译的 sidecar 二进制文件：
- **Linux**：x86_64 (manylinux2014)
- **macOS**：arm64 (12.0+)
- **Windows**：x86_64

要求：Python 3.12+。

## 路线图
- **阶段 3 ✅**：发布流水线和多平台 wheel 包自动化。
- **v0.1.0 ✅**：首次发布到 PyPI。
- **阶段 4.1 ✅**：群组管理。
- **阶段 4.2 ✅**：回执与在线状态（输入指示、已读回执）。
- **v0.2.1 ✅**：阶段 4.1 + 4.2 已发布到 PyPI。
- **下一步**：
  - 阶段 4.3：媒体消息（图片、视频、音频、文档、贴纸）。
- **远期计划**：
  - 高级会话管理 UI。
  - Worker / Broker 部署拓扑。

## 开发
设置本地开发环境：
```bash
git clone https://github.com/jianjian2048/fastmeow
cd fastmeow
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src/fastmeow
```

## 许可证
基于 MIT 许可证分发。详见 `LICENSE`。

## 致谢
- [whatsmeow](https://github.com/tulir/whatsmeow) (作者 tulir)：驱动 sidecar 的底层 Go 库。
- [neonize](https://github.com/krypton-byte/neonize)：在 Python 中封装 whatsmeow 的灵感来源。
