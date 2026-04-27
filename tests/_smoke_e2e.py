"""端到端 smoke：通过 FastMeow Python API 操作真实 WhatsApp 账号。

会启动真实的 Go sidecar、打开真实的 gRPC 通道、注册账号、在首次运行时
执行 QR 配对（或在后续运行中重新挂载已有会话），并监听一条入站消息。

手动运行（需要可交互终端用于扫码 + 一部手机）：

    .\\.venv\\Scripts\\python.exe -m tests._smoke_e2e

行为：
    * 首次运行：在 stderr 打印 QR 码（终端渲染器）。请在手机上的
      WhatsApp -> Linked Devices 中扫码。
    * 重新挂载运行：跳过 QR；直接使用 ``./e2e-smoke-sessions/``
      中现有的 sqlite 会话进入 CONNECTED。
    * 一旦连接成功，打印每条收到的 MessageEvent，并对文本私聊回复
      ``echo: <text>``。在一次回复后或 120s 超时后停止，以先到者为准。

清理：
    会话保存在 ``D:\\Srcs\\FastMeow\\e2e-smoke-sessions\\`` 下。
    删除该目录即可在下次运行时强制重新扫码配对。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from fastmeow import (
    ConnectedEvent,
    Ctx,
    F,
    FastMeow,
    MessageEvent,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_log = logging.getLogger("fastmeow.smoke.e2e")


SESSION_DIR = Path(__file__).resolve().parent.parent / "e2e-smoke-sessions"
PAIRING_TIMEOUT = 120.0
ACTIVITY_TIMEOUT = 120.0


async def main() -> int:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[smoke] session dir: {SESSION_DIR}", file=sys.stderr)

    router = Router(name="smoke")
    first_message: asyncio.Future[MessageEvent] = asyncio.get_event_loop().create_future()

    @router.connected()
    async def on_connected(event: ConnectedEvent, ctx: Ctx) -> None:
        print(
            f"[smoke] connected: account={ctx.account_key} jid={ctx.account_jid}",
            file=sys.stderr,
        )

    @router.message(F.is_dm & ~F.from_me)
    async def on_dm(msg: MessageEvent, ctx: Ctx) -> None:
        print(
            f"[smoke] message from {msg.sender_jid}: {msg.text!r}",
            file=sys.stderr,
        )
        if msg.text:
            try:
                result = await ctx.reply(f"echo: {msg.text}")
                print(
                    f"[smoke] replied: server_id={result.message_id}",
                    file=sys.stderr,
                )
            except Exception:
                _log.exception("reply failed")
            if not first_message.done():
                first_message.set_result(msg)

    async with FastMeow(session_dir=SESSION_DIR) as app:
        app.include_router(router)

        handle = app.add_account("smoke-e2e", on_qr="terminal")
        print(
            f"[smoke] add_account returned; state={handle.state} jid={handle.jid!r}",
            file=sys.stderr,
        )

        print(
            f"[smoke] waiting up to {PAIRING_TIMEOUT}s for CONNECTED...",
            file=sys.stderr,
        )
        await handle.ready(timeout=PAIRING_TIMEOUT)
        print(
            f"[smoke] CONNECTED: jid={handle.jid}; send a DM from another phone to test echo.",
            file=sys.stderr,
        )

        try:
            msg = await asyncio.wait_for(first_message, timeout=ACTIVITY_TIMEOUT)
            print(
                f"[smoke] OK: got message + replied. seq={msg.seq}",
                file=sys.stderr,
            )
            return 0
        except TimeoutError:
            print(
                f"[smoke] no inbound message in {ACTIVITY_TIMEOUT}s; "
                "connection itself was OK so smoke is partially green.",
                file=sys.stderr,
            )
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
