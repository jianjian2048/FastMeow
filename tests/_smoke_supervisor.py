"""Quick interactive smoke for the Sidecar supervisor.

Not a pytest test (it spawns the real Go binary and listens on a real
TCP port). Run manually:

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

        # Concurrently print a few log lines while waiting for ready.
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
