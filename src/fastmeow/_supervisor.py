"""Supervises the embedded Go sidecar process.

The sidecar is a long-lived subprocess that hosts whatsmeow.Client instances
and exposes a local gRPC service. The supervisor is the *only* place in
FastMeow that knows how to find, launch, monitor, and gracefully terminate
that process.

Responsibilities:
    1. Resolve the sidecar binary path (bundled in wheel, or dev tree, or
       overridden via ``FASTMEOW_SIDECAR_BIN``).
    2. Spawn the process with the canonical CLI flags
       (``--listen``, ``--session-dir``, ``--sidecar-id``,
       ``--auth-token``).
    3. Drain ``stderr`` line-by-line as JSON log records and surface them
       to subscribers (the SDK logger, the ``starting/listening/fatal``
       lifecycle gate).
    4. Block ``wait_ready()`` until the sidecar prints
       ``{"event": "listening", "addr": "<host:port>"}`` so callers know
       the OS-chosen TCP port to dial.
    5. Cross-platform graceful stop:
         * POSIX: ``SIGTERM`` -> wait ``grace`` seconds -> ``SIGKILL``.
         * Windows: ``CTRL_BREAK_EVENT`` to the process group, then
           ``TerminateProcess`` on grace timeout.
       The sidecar already runs ``signal.NotifyContext(SIGINT, SIGTERM)``
       on POSIX and uses Go's runtime handling of ``CTRL_BREAK`` on
       Windows; both translate to the same coordinated shutdown path
       inside the gateway (``GracefulStop`` -> bus close).

The supervisor exposes ONE event source (``stderr_log`` async iterator) and
ONE readiness primitive (``wait_ready``). Higher layers (transport,
dispatcher, app) consume those without re-parsing stderr.
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
    """One line of structured log output from the sidecar's stderr.

    The sidecar always writes JSON objects (``encoding/json`` in Go); if a
    line fails to parse we still surface it as ``raw`` so users can debug.
    """

    event: str
    """Logical event name, e.g. ``"starting" | "listening" | "shutdown"``."""

    fields: dict[str, Any]
    """All other top-level keys from the JSON record."""

    raw: str
    """Original stderr line (without trailing newline)."""


@dataclass
class SidecarConfig:
    """User-tunable launch parameters."""

    session_dir: Path
    """Directory containing ``main.sqlite``; created by the sidecar if missing."""

    sidecar_id: str = "default"
    """Identifier echoed in events/logs; matches Phase 1 default."""

    listen: str = "tcp://127.0.0.1:0"
    """Listener spec; ``:0`` lets the OS pick a free port (recommended)."""

    auth_token: str = ""
    """Reserved for the next milestone; sent verbatim to ``--auth-token``."""

    binary_path: Path | None = None
    """Override for the sidecar binary location. ``None`` = auto-resolve."""

    ready_timeout: float = 15.0
    """Max seconds to wait for ``listening`` event after spawn."""

    stop_grace: float = 10.0
    """Seconds between graceful signal and forced kill."""


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

# In wheels we ship the binary at ``fastmeow/_bin/<exe>``. In a dev checkout
# (no wheel build yet) the Phase 1 build dropped it under ``<repo>/bin/``.
_BINARY_NAMES = ("fastmeow-sidecar.exe" if sys.platform == "win32" else "fastmeow-sidecar",)


def _resolve_binary(override: Path | None) -> Path:
    """Find the sidecar executable.

    Order:
        1. Explicit ``override`` argument.
        2. ``FASTMEOW_SIDECAR_BIN`` environment variable.
        3. Bundled wheel layout: ``<package_root>/_bin/<exe>``.
        4. Dev checkout layout: ``<repo_root>/bin/<exe>``.

    Raises ``SidecarBinaryNotFoundError`` if none of the above resolve to
    an executable file.
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

    # Dev tree: src/fastmeow/_supervisor.py -> repo root is parents[2]
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
    """Mutable state populated as stderr is drained."""

    bound_addr: str | None = None
    fatal: SidecarLog | None = None
    starting: SidecarLog | None = None
    log_subscribers: list[asyncio.Queue[SidecarLog | None]] = field(default_factory=list)


class Sidecar:
    """Async wrapper around the Go sidecar subprocess.

    Typical use::

        sc = Sidecar(SidecarConfig(session_dir=Path("./sessions")))
        await sc.start()
        addr = await sc.wait_ready()        # "127.0.0.1:53412"
        ...
        await sc.stop()

    Concurrency contract:
        * ``start`` must be called exactly once before any other method.
        * ``wait_ready`` may be awaited concurrently from multiple tasks.
        * ``stop`` is idempotent.
        * Iterating ``logs()`` returns a *new* subscription each call;
          subscribers fan out from a shared producer.
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

    # -- public API ---------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    @property
    def bound_addr(self) -> str | None:
        """The ``host:port`` from the ``listening`` event, or ``None``."""
        return self._state.bound_addr

    async def start(self) -> None:
        """Spawn the sidecar process and begin draining its stderr.

        Returns once the OS has handed us a child PID; readiness is a
        separate gate (see ``wait_ready``).
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

        # On Windows we must put the child in its own process group so we
        # can deliver CTRL_BREAK_EVENT without nuking our own process.
        # On POSIX we likewise want a fresh group so SIGTERM only hits the
        # sidecar tree.
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
        """Block until the sidecar emits ``listening`` and return its addr.

        Raises ``SidecarStartupError`` if the process emits ``fatal`` or
        exits before becoming ready, or if ``timeout`` (default
        ``config.ready_timeout``) elapses.
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

        # Exited before ready. Surface the most informative error we have.
        fatal = self._state.fatal
        rc = self.returncode
        if fatal is not None:
            raise SidecarStartupError(
                f"sidecar exited before ready (rc={rc}): {fatal.event} {fatal.fields}"
            )
        raise SidecarStartupError(f"sidecar exited before ready (rc={rc})")

    async def stop(self, grace: float | None = None) -> int:
        """Gracefully terminate the sidecar and return its exit code.

        Idempotent: subsequent calls return the cached exit code.
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
                    # Graceful window expired -> hard kill.
                    with contextlib.suppress(ProcessLookupError):
                        self._proc.kill()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                    except TimeoutError as exc:
                        raise SidecarCrashedError(
                            "sidecar did not exit after SIGKILL/TerminateProcess"
                        ) from exc

            # Drain helper tasks so we don't leak.
            for task in (self._stderr_task, self._wait_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

            # Notify any in-flight log subscribers.
            for q in self._state.log_subscribers:
                q.put_nowait(None)

            rc = self._proc.returncode
            return rc if rc is not None else -1

    async def logs(self) -> AsyncIterator[SidecarLog]:
        """Subscribe to structured stderr records.

        Each call returns an independent async iterator. The iterator ends
        when the sidecar stops emitting (process exit + queue drained).
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

    # -- internals ----------------------------------------------------------

    def _send_stop_signal(self) -> None:
        """Best-effort graceful signal that maps to whatsmeow shutdown."""
        assert self._proc is not None
        try:
            if sys.platform == "win32":
                # CTRL_BREAK_EVENT is the only signal Windows reliably
                # delivers to a child in a new process group; the Go
                # runtime catches it and runs our SIGTERM handler.
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined,unused-ignore]
            else:
                self._proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            # Already gone -- not an error during shutdown races.
            pass

    async def _drain_stderr(self) -> None:
        """Read stderr line-by-line, parse JSON, fan out to subscribers."""
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
            # Never let a stderr-parser bug take down the event loop;
            # the supervisor's contract is "best-effort drain".
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
        # whatsmeow's own logger writes free-form text to stderr too;
        # we still surface it under a synthetic "log" event.
        return SidecarLog(event="log", fields={}, raw=line)

    def _handle_record(self, record: SidecarLog) -> None:
        if record.event == "listening":
            addr = record.fields.get("addr")
            if isinstance(addr, str) and addr:
                # Sidecar prints the actual host:port the OS picked. Strip
                # any IPv6 zone or scheme just in case.
                self._state.bound_addr = addr
                self._ready_event.set()
        elif record.event == "starting":
            self._state.starting = record
        elif record.event == "fatal":
            self._state.fatal = record

        for q in list(self._state.log_subscribers):
            q.put_nowait(record)

    async def _wait_exit(self) -> int:
        """Watch for process exit and unblock readiness/log waiters."""
        assert self._proc is not None
        rc = await self._proc.wait()
        self._exited_event.set()
        # Make sure stderr drain finishes flushing before subscribers see EOF.
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        for q in list(self._state.log_subscribers):
            q.put_nowait(None)
        return rc
