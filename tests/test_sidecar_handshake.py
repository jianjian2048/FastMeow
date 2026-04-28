"""sidecar 与 Python 客户端协议版本握手回归测试。

这是 ``v0.3.1`` 引入的硬性测试，用于阻止 Go ``protocolVersion`` 与 Python
``PROTOCOL_VERSION`` 再次出现版本漂移（``v0.3.0`` 因为只升 Python 端导致首
次连接即抛 ``SidecarStartupError``）。

不同于 ``tests/_smoke_transport.py`` 这类手动 smoke 脚本，此测试在
``pytest`` 默认运行集中执行；如果本地没有可用的 ``fastmeow-sidecar`` 二进
制（``src/fastmeow/_bin/`` 或 ``FASTMEOW_SIDECAR_BIN`` 指向的位置），整个
模块会被 ``pytest.skip``。CI 的 release workflow 会在打 wheel 前 ``go
build`` 出该二进制，这里能跑到的地方一定能验真。

覆盖的不变量：
    1. 正常握手——``connect()`` 用当前 ``PROTOCOL_VERSION`` 调用 ``Ping``，
       ``transport.protocol_version`` 必须等于 ``PROTOCOL_VERSION``。
    2. ``client < server``——传一个低于 ``PROTOCOL_VERSION`` 的版本，
       ``connect()`` 必须抛 ``SidecarStartupError`` 或 ``TransportError``，
       并且错误消息要包含 ``mismatch``。
    3. ``client > server``——同 (2)，传一个高于 ``PROTOCOL_VERSION`` 的版本。
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fastmeow._supervisor import Sidecar, SidecarConfig
from fastmeow._transport import PROTOCOL_VERSION, connect
from fastmeow.exceptions import SidecarStartupError, TransportError

# ---------------------------------------------------------------------------
# 二进制发现 / module-level skip
# ---------------------------------------------------------------------------


def _binary_name() -> str:
    return "fastmeow-sidecar.exe" if sys.platform == "win32" else "fastmeow-sidecar"


def _resolve_binary() -> Path | None:
    """复刻 ``_supervisor._resolve_binary`` 的查找顺序但不抛异常。"""
    override = os.environ.get("FASTMEOW_SIDECAR_BIN", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None

    name = _binary_name()
    pkg_bin = Path(__file__).resolve().parents[1] / "src" / "fastmeow" / "_bin" / name
    if pkg_bin.is_file():
        return pkg_bin

    repo_bin = Path(__file__).resolve().parents[1] / "bin" / name
    if repo_bin.is_file():
        return repo_bin
    return None


_BINARY = _resolve_binary()
if _BINARY is None:
    pytest.skip(
        f"sidecar binary not found; build with `go build -trimpath -ldflags=\"-s -w\" -o "
        f"src/fastmeow/_bin/{_binary_name()} ./cmd/fastmeow-sidecar` "
        f"or set FASTMEOW_SIDECAR_BIN to point at one",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def running_sidecar(tmp_path: Path) -> AsyncIterator[str]:
    """启动真实 sidecar，yield ``host:port``，结束后干净停掉。"""
    workdir = tmp_path / "sessions"
    workdir.mkdir(parents=True, exist_ok=True)

    sidecar = Sidecar(
        SidecarConfig(session_dir=workdir, binary_path=_BINARY),
    )
    await sidecar.start()
    try:
        addr = await sidecar.wait_ready()
        yield addr
    finally:
        await sidecar.stop()
        # tmp_path 由 pytest 清理；额外保险删一次以防 SQLite WAL 残留
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_handshake_succeeds_with_current_protocol_version(
    running_sidecar: str,
) -> None:
    """正向握手：Python ``PROTOCOL_VERSION`` 必须能被 sidecar 接受。

    这是 ``v0.3.0`` 漏掉的那条——CI 用 mock transport 跑过去了，但真实
    binary 装上 PyPI wheel 第一次 connect 直接 mismatch。
    """
    transport = await connect(
        running_sidecar,
        expected_protocol_version=PROTOCOL_VERSION,
        handshake_timeout=10.0,
    )
    try:
        assert transport.protocol_version == PROTOCOL_VERSION
        assert transport.sidecar_id  # 非空字符串
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "wrong_version",
    [
        PROTOCOL_VERSION - 1,  # 老客户端 / 新 sidecar
        PROTOCOL_VERSION + 1,  # 新客户端 / 老 sidecar
    ],
)
async def test_handshake_rejects_mismatched_protocol_version(
    running_sidecar: str, wrong_version: int
) -> None:
    """反向握手：任何与 sidecar ``protocolVersion`` 不一致的版本都必须报错。

    错误信息里要明确说 ``mismatch``，避免运维拿到一个含糊的 ``ping``
    超时去查网络。
    """
    if wrong_version < 1:
        pytest.skip("protocol version 0 is not a valid wire value")

    with pytest.raises((SidecarStartupError, TransportError)) as exc_info:
        await connect(
            running_sidecar,
            expected_protocol_version=wrong_version,
            handshake_timeout=10.0,
        )
    assert "mismatch" in str(exc_info.value).lower()
