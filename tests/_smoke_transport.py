"""supervisor + transport 握手的端到端 smoke。

会启动真实的 Go sidecar、打开 gRPC 通道、调用 Ping、验证协议版本，
并干净地关闭所有组件。

手动运行：
    .\\.venv\\Scripts\\python.exe -m tests._smoke_transport
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastmeow._supervisor import Sidecar, SidecarConfig
from fastmeow._transport import connect


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="fastmeow-smoke-"))
    try:
        sc = Sidecar(SidecarConfig(session_dir=workdir / "sessions"))
        await sc.start()
        addr = await sc.wait_ready()
        print(f"[smoke] sidecar ready at {addr}")

        transport = await connect(addr)
        print(
            f"[smoke] handshake OK: proto={transport.protocol_version} "
            f"sidecar={transport.sidecar_version} "
            f"whatsmeow={transport.whatsmeow_version} "
            f"id={transport.sidecar_id}"
        )

        # 在一个新 key 上尝试 EnsureAccount（会创建未配对设备）
        state, created = await transport.ensure_account(
            account_key="smoke-test", display_name="Smoke"
        )
        print(f"[smoke] ensure_account: state={state} created={created}")

        await transport.close()
        rc = await sc.stop()
        print(f"[smoke] sidecar exit={rc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
