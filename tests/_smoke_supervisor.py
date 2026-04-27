"""Sidecar supervisor 的快速交互 smoke。

这不是 pytest 测试（它会启动真实的 Go 二进制并监听真实的 TCP 端口）。
手动运行：

    .\\.venv\\Scripts\\python.exe -m tests._smoke_supervisor
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastmeow._supervisor import Sidecar, SidecarConfig


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="fastmeow-smoke-"))
    try:
        sc = Sidecar(SidecarConfig(session_dir=workdir / "sessions"))
        print("[smoke] starting sidecar...")
        await sc.start()
        print(f"[smoke] pid={sc.pid}")

        # 在等待 ready 时并发打印几条日志。
        async def tail() -> None:
            async for log in sc.logs():
                print(f"[sidecar] event={log.event} fields={log.fields}")
                if log.event in ("listening", "fatal"):
                    return

        tail_task = asyncio.create_task(tail())
        addr = await sc.wait_ready()
        print(f"[smoke] ready at {addr}")
        await asyncio.wait_for(tail_task, timeout=2.0)

        print("[smoke] stopping...")
        rc = await sc.stop()
        print(f"[smoke] exit={rc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
