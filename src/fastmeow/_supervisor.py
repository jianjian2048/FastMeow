"""监督嵌入的 Go sidecar 进程。

sidecar 是一个运行 whatsmeow.Client 实例并暴露本地 gRPC 服务的长期子进程。
监督器是 FastMeow 中 *唯一* 负责查找、启动、监控及优雅关闭该进程的组件。

职责：
    1. 解析 sidecar 二进制路径（打包在 wheel 中、开发目录中，或通过 ``FASTMEOW_SIDECAR_BIN`` 覆盖）。
    2. 使用规范的 CLI 参数启动进程（``--listen``、``--session-dir``、``--sidecar-id``、``--auth-token``）。
    3. 逐行读取 ``stderr`` 作为 JSON 日志记录，并将其分发给订阅者（SDK 日志记录器、``starting/listening/fatal`` 生命周期门控）。
    4. 阻塞 ``wait_ready()`` 直到 sidecar 输出 ``{"event": "listening", "addr": "<host:port>"}``，
       以便调用方知道要拨号的由操作系统选择的 TCP 端口。
    5. 跨平台的优雅关闭：
         * POSIX: 发送 ``SIGTERM`` -> 等待 ``grace`` 秒 -> 发送 ``SIGKILL``。
         * Windows: 向进程组发送 ``CTRL_BREAK_EVENT``，若在宽限期内未退出则调用 ``TerminateProcess``。
       sidecar 在 POSIX 上已运行 ``signal.NotifyContext(SIGINT, SIGTERM)``，在 Windows 上使用 Go 运行时
       处理 ``CTRL_BREAK``；两者都会转换为网关内部相同的协调关闭路径（``GracefulStop`` -> 关闭总线）。

监督器暴露了一个事件源（``stderr_log`` 异步迭代器）和一个就绪原语（``wait_ready``）。
更上层（传输层、派发器、app）无需重新解析 stderr 即可直接使用这些功能。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import (
    SidecarBinaryNotFoundError,
    SidecarCrashedError,
    SidecarStartupError,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SidecarLog:
    """来自 sidecar stderr 的单行结构化日志输出。

    sidecar 始终写入 JSON 对象（Go 中的 ``encoding/json``）；如果某行解析失败，
    我们仍会将其作为 ``raw`` 暴露，以便用户进行调试。
    """

    event: str
    """逻辑事件名称，例如 ``"starting" | "listening" | "shutdown"``。"""

    fields: dict[str, Any]
    """JSON 记录中的所有其他顶层键。"""

    raw: str
    """原始 stderr 行（不含换行符）。"""


@dataclass
class SidecarConfig:
    """用户可调节的启动参数。"""

    session_dir: Path
    """包含 ``main.sqlite`` 的目录；如果不存在，将由 sidecar 创建。"""

    sidecar_id: str = "default"
    """在事件/日志中回显的标识符；与第一阶段的默认值匹配。"""

    listen: str = "tcp://127.0.0.1:0"
    """监听规范；``:0`` 表示由操作系统选择可用端口（推荐）。"""

    auth_token: str = ""
    """为下一阶段预留；原样发送至 ``--auth-token``。"""

    binary_path: Path | None = None
    """sidecar 二进制文件位置的覆盖路径。``None`` 表示自动解析。"""

    ready_timeout: float = 15.0
    """启动后等待 ``listening`` 事件的最大秒数。"""

    stop_grace: float = 10.0
    """发送优雅关闭信号与强制终止之间的间隔秒数。"""


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

# 在 wheel 包中，我们将二进制文件放在 ``fastmeow/_bin/<exe>``。在开发树中
# （尚未构建 wheel），第一阶段的构建会将其放在 ``<repo>/bin/`` 目录下。
_BINARY_NAMES = ("fastmeow-sidecar.exe" if sys.platform == "win32" else "fastmeow-sidecar",)


def _resolve_binary(override: Path | None) -> Path:
    """查找 sidecar 可执行文件。

    顺序：
        1. 显式的 ``override`` 参数。
        2. ``FASTMEOW_SIDECAR_BIN`` 环境变量。
        3. 打包的 wheel 布局：``<package_root>/_bin/<exe>``。
        4. 开发环境布局：``<repo_root>/bin/<exe>``。

    如果以上路径均未解析到可执行文件，则抛出 ``SidecarBinaryNotFoundError``。
    """
    candidates: list[Path] = []
    if override is not None:
        candidates.append(override)
    env_path = os.environ.get("FASTMEOW_SIDECAR_BIN")
    if env_path:
        candidates.append(Path(env_path))

    pkg_bin_dir = Path(__file__).resolve().parent / "_bin"
    for name in _BINARY_NAMES:
        candidates.append(pkg_bin_dir / name)

    # 开发树：src/fastmeow/_supervisor.py -> 仓库根目录是 parents[2]
    repo_bin_dir = Path(__file__).resolve().parents[2] / "bin"
    for name in _BINARY_NAMES:
        candidates.append(repo_bin_dir / name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise SidecarBinaryNotFoundError(
        "Could not locate the fastmeow-sidecar executable. Looked in: "
        + ", ".join(str(c) for c in candidates)
    )


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


@dataclass
class _SupervisorState:
    """随 stderr 读取而填充的可变状态。"""

    bound_addr: str | None = None
    fatal: SidecarLog | None = None
    starting: SidecarLog | None = None
    log_subscribers: list[asyncio.Queue[SidecarLog | None]] = field(default_factory=list)


class Sidecar:
    """Go sidecar 子进程的异步封装。

    典型用法::

        sc = Sidecar(SidecarConfig(session_dir=Path("./sessions")))
        await sc.start()
        addr = await sc.wait_ready()        # "127.0.0.1:53412"
        ...
        await sc.stop()

    并发约定：
        * 在调用任何其他方法前，必须且只能调用一次 ``start``。
        * 多个任务可以并发地对 ``wait_ready`` 进行 await。
        * ``stop`` 是幂等的。
        * 每次迭代 ``logs()`` 都会返回一个 *新的* 订阅；所有订阅者都从一个共享的生产者扇出。
    """

    def __init__(self, config: SidecarConfig) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[int] | None = None
        self._ready_event = asyncio.Event()
        self._exited_event = asyncio.Event()
        self._state = _SupervisorState()
        self._stop_lock = asyncio.Lock()
        self._stopped = False

    # -- 公开 API ---------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    @property
    def bound_addr(self) -> str | None:
        """来自 ``listening`` 事件的 ``host:port``，或者为 ``None``。"""
        return self._state.bound_addr

    async def start(self) -> None:
        """启动 sidecar 进程并开始读取其 stderr。

        在操作系统向我们返回子进程 PID 后立即返回；就绪状态是一个单独的门控（参见 ``wait_ready``）。
        """
        if self._proc is not None:
            raise SidecarStartupError("sidecar already started")

        binary = _resolve_binary(self._config.binary_path)
        self._config.session_dir.mkdir(parents=True, exist_ok=True)

        argv: list[str] = [
            str(binary),
            "--listen",
            self._config.listen,
            "--session-dir",
            str(self._config.session_dir),
            "--sidecar-id",
            self._config.sidecar_id,
        ]
        if self._config.auth_token:
            argv += ["--auth-token", self._config.auth_token]

        # 在 Windows 上，我们必须将子进程放入独立的进程组，以便我们可以
        # 发送 CTRL_BREAK_EVENT 而不至于杀掉我们自己的进程。
        # 在 POSIX 上，我们也需要一个新进程组，以便 SIGTERM 只影响 sidecar 进程树。
        creationflags = 0
        start_new_session = False
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined,unused-ignore]
        else:
            start_new_session = True

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except OSError as exc:
            raise SidecarStartupError(f"failed to spawn sidecar {binary}: {exc}") from exc

        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="fastmeow-sidecar-stderr"
        )
        self._wait_task = asyncio.create_task(self._wait_exit(), name="fastmeow-sidecar-wait")

    async def wait_ready(self, timeout: float | None = None) -> str:
        """阻塞直到 sidecar 发出 ``listening`` 事件并返回其地址。

        如果进程在就绪前发出 ``fatal`` 事件或退出，或者如果超时
        （默认为 ``config.ready_timeout``），则抛出 ``SidecarStartupError``。
        """
        if self._proc is None:
            raise SidecarStartupError("sidecar not started")

        deadline = timeout if timeout is not None else self._config.ready_timeout

        ready_wait = asyncio.create_task(self._ready_event.wait())
        exited_wait = asyncio.create_task(self._exited_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_wait, exited_wait},
                timeout=deadline,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (ready_wait, exited_wait):
                if not task.done():
                    task.cancel()

        if not done:
            raise SidecarStartupError(f"sidecar did not become ready within {deadline:.1f}s")

        if self._ready_event.is_set():
            assert self._state.bound_addr is not None
            return self._state.bound_addr

        # 在就绪前退出。显示我们拥有的最有价值的错误信息。
        fatal = self._state.fatal
        rc = self.returncode
        if fatal is not None:
            raise SidecarStartupError(
                f"sidecar exited before ready (rc={rc}): {fatal.event} {fatal.fields}"
            )
        raise SidecarStartupError(f"sidecar exited before ready (rc={rc})")

    async def stop(self, grace: float | None = None) -> int:
        """优雅地终止 sidecar 并返回其退出码。

        幂等：后续调用将返回缓存的退出码。
        """
        async with self._stop_lock:
            if self._stopped:
                return self.returncode if self.returncode is not None else 0
            self._stopped = True

            if self._proc is None:
                return 0

            grace_s = grace if grace is not None else self._config.stop_grace

            if self._proc.returncode is None:
                self._send_stop_signal()

                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=grace_s)
                except TimeoutError:
                    # 宽限期已过 -> 强制终止。
                    with contextlib.suppress(ProcessLookupError):
                        self._proc.kill()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                    except TimeoutError as exc:
                        raise SidecarCrashedError(
                            "sidecar did not exit after SIGKILL/TerminateProcess"
                        ) from exc

            # 清理辅助任务以防泄露。
            for task in (self._stderr_task, self._wait_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

            # 通知任何正在运行的日志订阅者。
            for q in self._state.log_subscribers:
                q.put_nowait(None)

            rc = self._proc.returncode
            return rc if rc is not None else -1

    async def logs(self) -> AsyncIterator[SidecarLog]:
        """订阅结构化的 stderr 记录。

        每次调用都返回一个独立的异步迭代器。当 sidecar 停止发送时
        （进程退出且队列排空），迭代器结束。
        """
        queue: asyncio.Queue[SidecarLog | None] = asyncio.Queue()
        self._state.log_subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            with contextlib.suppress(ValueError):
                self._state.log_subscribers.remove(queue)

    # -- 内部方法 ----------------------------------------------------------

    def _send_stop_signal(self) -> None:
        """尽力发送能映射至 whatsmeow 关闭流程的优雅信号。"""
        assert self._proc is not None
        try:
            if sys.platform == "win32":
                # CTRL_BREAK_EVENT 是 Windows 唯一能可靠地传递给新进程组中
                # 子进程的信号；Go 运行时会捕获它并运行我们的 SIGTERM 处理器。
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined,unused-ignore]
            else:
                self._proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            # 进程已消失 —— 在关闭时的竞态条件下这不属于错误。
            pass

    async def _drain_stderr(self) -> None:
        """逐行读取 stderr，解析 JSON，并分发给订阅者。"""
        assert self._proc is not None
        assert self._proc.stderr is not None
        stream = self._proc.stderr
        try:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    return
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                record = self._parse_line(line)
                self._handle_record(record)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            # 绝不让 stderr 解析器的 bug 拖垮事件循环；
            # 监督器的约定是“尽力而为”的读取。
            return

    @staticmethod
    def _parse_line(line: str) -> SidecarLog:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                event = str(obj.get("event", "log"))
                fields = {k: v for k, v in obj.items() if k != "event"}
                return SidecarLog(event=event, fields=fields, raw=line)
        except json.JSONDecodeError:
            pass
        # whatsmeow 自带的日志记录器也会向 stderr 写入自由格式的文本；
        # 我们仍将其作为合成的 "log" 事件暴露。
        return SidecarLog(event="log", fields={}, raw=line)

    def _handle_record(self, record: SidecarLog) -> None:
        if record.event == "listening":
            addr = record.fields.get("addr")
            if isinstance(addr, str) and addr:
                # sidecar 打印由操作系统实际选择的 host:port。
                # 预防性地移除任何 IPv6 区域 (zone) 或协议前缀。
                self._state.bound_addr = addr
                self._ready_event.set()
        elif record.event == "starting":
            self._state.starting = record
        elif record.event == "fatal":
            self._state.fatal = record

        for q in list(self._state.log_subscribers):
            q.put_nowait(record)

    async def _wait_exit(self) -> int:
        """监控进程退出并解除对就绪/日志等待者的阻塞。"""
        assert self._proc is not None
        rc = await self._proc.wait()
        self._exited_event.set()
        # 确保 stderr 读取在订阅者看到 EOF 前完成刷新。
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        for q in list(self._state.log_subscribers):
            q.put_nowait(None)
        return rc
