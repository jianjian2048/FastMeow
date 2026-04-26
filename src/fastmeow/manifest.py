"""账号 manifest 清单：映射 ``account_key`` -> ``jid`` 的唯一事实来源。

Go sidecar 刻意设计为不存储用户选择的账号 key；它只知道 JID（存储在它的 sqlite 会话存储中），
但**不**持久化人类可读的 ``account_key`` -> ``jid`` 映射。
在 Python 端保留此映射服务于两个目标：

1. **重新配对时的稳定标识符。** 当用户登出并重新配对 ``"alice"`` 时，JID 会改变，
   但用户代码在所有地方依然可以继续使用 ``"alice"``。
2. **崩溃安全的注册表。** 如果 sidecar 在会话中途崩溃并重启，FastMeow 需要知道哪些账号需要重新连接。
   manifest 清单会在启动时进行回放。

并发模型
-----------------
单个 :class:`Manifest` 实例由一个 :class:`fastmeow.FastMeow` 应用拥有。
所有变更操作都通过受 :class:`asyncio.Lock` 保护的 ``async`` 方法进行，
因此来自用户代码的并发 ``add_account`` 调用是安全的。

跨进程安全：磁盘文件采用原子写入方式（临时文件 + ``os.replace``），
并受到尽力而为的排他性文件锁保护（Windows 上使用 ``msvcrt``，POSIX 上使用 ``fcntl``）。
**不支持**两个 FastMeow 进程共享同一个 ``session-dir``（底层的 sqlite 会话存储也不支持），
但该锁可以防止最常见的误操作：无意中从两个终端启动两次应用。

文件格式
-----------
使用带有包装对象的 JSON，以便我们以后可以在不破坏解析器的情况下添加字段：

.. code-block:: json

    {
      "version": 1,
      "accounts": {
        "alice": {"jid": "1234567890:7@s.whatsapp.net"},
        "bob":   {"jid": ""}
      }
    }

空的 ``jid`` 字符串表示“已注册但尚未配对”。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from fastmeow.exceptions import ManifestError

log = logging.getLogger(__name__)

_MANIFEST_VERSION = 1
_FILENAME = "accounts.json"
_LOCKFILE = "accounts.lock"


__all__ = ["Manifest", "ManifestEntry"]


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """manifest 清单中的一行。"""

    account_key: str
    jid: str
    """空字符串表示账号已注册但未配对。"""

    @property
    def is_paired(self) -> bool:
        return bool(self.jid)


# ---------------------------------------------------------------------------
# 跨平台排他性文件锁
# ---------------------------------------------------------------------------


class _FileLock:
    """对哨兵文件的尽力而为排他锁。

    仅用于检测“另一个 FastMeow 进程已在使用此 session-dir”。
    在上下文退出时释放。永远不会阻塞：发生争用时，我们会立即抛出带有友好提示的异常。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        # 打开或创建。我们在 Manifest 的生命周期内持有该句柄；关闭它会释放 OS 层级的锁。
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+b")  # noqa: SIM115 — 在 release() 中关闭
        try:
            if sys.platform == "win32":
                import msvcrt

                # 在偏移量 0 处锁定 1 字节；LK_NBLCK = 非阻塞。
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise ManifestError(
                f"Another FastMeow process appears to be using session "
                f"directory {self._path.parent} (failed to acquire "
                f"exclusive lock on {self._path.name}). "
                f"Stop the other process or pick a different session_dir."
            ) from exc

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    # 已释放或从未获取；忽略。
                    pass
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class Manifest:
    """内存 + 磁盘上的 account_key -> jid 注册表。

    通过 :meth:`open`（异步）进行构造，该方法会获取文件锁并加载任何预先存在的 JSON。
    作为异步上下文管理器使用，或在关闭时显式调用 :meth:`close`。
    """

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir
        self._path = session_dir / _FILENAME
        self._lock_path = session_dir / _LOCKFILE
        self._file_lock = _FileLock(self._lock_path)
        # 映射 account_key -> jid (jid 可能是空字符串)。
        self._entries: dict[str, str] = {}
        self._mutex = asyncio.Lock()
        self._opened = False

    # -- 生命周期 ---------------------------------------------------------

    @classmethod
    async def open(cls, session_dir: Path | str) -> Manifest:
        """获取跨进程锁并从磁盘加载 manifest 清单。"""
        m = cls(Path(session_dir))
        await asyncio.to_thread(m._open_sync)
        return m

    def _open_sync(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._file_lock.acquire()
        try:
            self._entries = self._load_from_disk()
        except Exception:
            # 如果加载失败，释放锁，以免留下僵尸哨兵文件。
            self._file_lock.release()
            raise
        self._opened = True

    async def close(self) -> None:
        if not self._opened:
            return
        await asyncio.to_thread(self._file_lock.release)
        self._opened = False

    async def __aenter__(self) -> Manifest:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- 读取 API（无需锁定：CPython 中字典读取是原子的，
    #    且我们不通过这些 API 暴露变更操作） ---------------------

    def __contains__(self, account_key: object) -> bool:
        return isinstance(account_key, str) and account_key in self._entries

    def __iter__(self) -> Iterator[ManifestEntry]:
        for key, jid in self._entries.items():
            yield ManifestEntry(account_key=key, jid=jid)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, account_key: str) -> ManifestEntry | None:
        jid = self._entries.get(account_key)
        if jid is None:
            return None
        return ManifestEntry(account_key=account_key, jid=jid)

    def jid_for(self, account_key: str) -> str:
        """返回 ``account_key`` 的 JID，如果未配对则返回空字符串。

        如果 key 未知，抛出 :class:`KeyError`。如果不确定，请先使用 ``in`` 检查。
        """
        return self._entries[account_key]

    def keys(self) -> list[str]:
        return list(self._entries.keys())

    # -- 变更 API -----------------------------------------------------

    async def register(self, account_key: str, *, jid: str = "") -> ManifestEntry:
        """注册新账号，或者如果账号已存在且 JID 相同，则不执行任何操作。

        如果账号已存在但带有*不同*的非空 JID，则抛出 :class:`ManifestError`
        而不是静默覆盖 — sidecar 的会话存储是 JID 的权威来源，我们永远不想与之脱节。
        """
        async with self._mutex:
            self._require_open()
            existing = self._entries.get(account_key)
            if existing is not None:
                # 允许升级 "" -> 真实 JID (配对后流程)，
                # 但永远不要用不同的 JID 覆盖非空 JID。
                if existing and jid and existing != jid:
                    raise ManifestError(
                        f"account_key {account_key!r} already mapped to "
                        f"jid {existing!r}; refusing to overwrite with "
                        f"{jid!r}"
                    )
                if existing == jid or not jid:
                    return ManifestEntry(account_key=account_key, jid=existing)
            self._entries[account_key] = jid
            await asyncio.to_thread(self._write_atomic, dict(self._entries))
            return ManifestEntry(account_key=account_key, jid=jid)

    async def update_jid(self, account_key: str, jid: str) -> ManifestEntry:
        """将 JID 绑定到已注册的 account_key。

        当 ``PairSuccessEvent`` 到达时由派发器调用。
        如果 JID 已正确，则为幂等。如果发生脱节，则抛出 :class:`ManifestError`。
        """
        async with self._mutex:
            self._require_open()
            existing = self._entries.get(account_key)
            if existing is None:
                raise ManifestError(f"cannot update_jid for unknown account_key {account_key!r}")
            if existing and existing != jid:
                raise ManifestError(
                    f"account_key {account_key!r} already paired to "
                    f"{existing!r}; sidecar reported new jid {jid!r}. "
                    f"This usually means the underlying session store "
                    f"was deleted out from under us."
                )
            if existing == jid:
                return ManifestEntry(account_key=account_key, jid=jid)
            self._entries[account_key] = jid
            await asyncio.to_thread(self._write_atomic, dict(self._entries))
            return ManifestEntry(account_key=account_key, jid=jid)

    async def remove(self, account_key: str) -> None:
        """遗忘账号。如果 key 未知则记录警告，但不抛出异常 — 删除操作在设计上是幂等的。
        """
        async with self._mutex:
            self._require_open()
            if account_key not in self._entries:
                log.warning("manifest.remove: unknown account_key %r", account_key)
                return
            del self._entries[account_key]
            await asyncio.to_thread(self._write_atomic, dict(self._entries))

    # -- internals ---------------------------------------------------------

    def _require_open(self) -> None:
        if not self._opened:
            raise ManifestError("manifest is closed")

    def _load_from_disk(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestError(f"failed to read manifest {self._path}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest {self._path} is corrupt (invalid JSON): {exc}") from exc
        return self._parse(data)

    @staticmethod
    def _parse(data: Any) -> dict[str, str]:
        if not isinstance(data, dict):
            raise ManifestError("manifest root must be a JSON object")
        version = data.get("version")
        if version != _MANIFEST_VERSION:
            raise ManifestError(
                f"manifest version {version!r} is not supported by this "
                f"FastMeow build (expected {_MANIFEST_VERSION}). Refusing "
                f"to start to avoid silent data loss."
            )
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            raise ManifestError("manifest 'accounts' must be a JSON object")
        out: dict[str, str] = {}
        for key, val in accounts.items():
            if not isinstance(key, str) or not key:
                raise ManifestError(f"manifest account key must be non-empty string, got {key!r}")
            if not isinstance(val, dict):
                raise ManifestError(
                    f"manifest entry for {key!r} must be an object, got {type(val).__name__}"
                )
            jid = val.get("jid", "")
            if not isinstance(jid, str):
                raise ManifestError(
                    f"manifest jid for {key!r} must be a string, got {type(jid).__name__}"
                )
            out[key] = jid
        return out

    def _write_atomic(self, snapshot: dict[str, str]) -> None:
        """原子地将 ``snapshot`` 写入磁盘。

        使用“同目录下的临时文件 + ``os.replace``”方式，使得读取者看到的要么是旧文件，
        要么是新文件，永远不会是写了一半的文件。
        """
        payload: dict[str, Any] = {
            "version": _MANIFEST_VERSION,
            "accounts": {key: {"jid": jid} for key, jid in sorted(snapshot.items())},
        }
        body = json.dumps(payload, indent=2, sort_keys=False) + "\n"

        # NamedTemporaryFile on Windows can't be reopened, so we manage
        # the tmp file by hand.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".accounts.", suffix=".tmp", dir=str(self._session_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            # Best-effort cleanup; swallow secondary errors.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
