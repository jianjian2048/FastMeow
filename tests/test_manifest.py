"""用于磁盘上的 Manifest 注册表的测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fastmeow.exceptions import ManifestError
from fastmeow.manifest import Manifest


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    return tmp_path / "session"


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_open_creates_session_dir(session_dir: Path) -> None:
    assert not session_dir.exists()
    m = await Manifest.open(session_dir)
    try:
        assert session_dir.is_dir()
        assert len(m) == 0
    finally:
        await m.close()


async def test_register_persists_to_disk(session_dir: Path) -> None:
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice")
        await m.register("bob", jid="123:7@s.whatsapp.net")
    finally:
        await m.close()

    raw = (session_dir / "accounts.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == 1
    assert data["accounts"]["alice"] == {"jid": ""}
    assert data["accounts"]["bob"] == {"jid": "123:7@s.whatsapp.net"}


async def test_reopen_loads_previous_state(session_dir: Path) -> None:
    m1 = await Manifest.open(session_dir)
    try:
        await m1.register("alice", jid="111@s.whatsapp.net")
    finally:
        await m1.close()

    m2 = await Manifest.open(session_dir)
    try:
        entry = m2.get("alice")
        assert entry is not None
        assert entry.jid == "111@s.whatsapp.net"
        assert entry.is_paired
    finally:
        await m2.close()


# ---------------------------------------------------------------------------
# 更新／漂移
# ---------------------------------------------------------------------------


async def test_update_jid_after_pairing(session_dir: Path) -> None:
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice")
        assert m.jid_for("alice") == ""
        await m.update_jid("alice", "999@s.whatsapp.net")
        assert m.jid_for("alice") == "999@s.whatsapp.net"
    finally:
        await m.close()


async def test_update_jid_idempotent(session_dir: Path) -> None:
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice", jid="999@s.whatsapp.net")
        await m.update_jid("alice", "999@s.whatsapp.net")  # no-op
        assert m.jid_for("alice") == "999@s.whatsapp.net"
    finally:
        await m.close()


async def test_update_jid_drift_raises(session_dir: Path) -> None:
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice", jid="999@s.whatsapp.net")
        with pytest.raises(ManifestError):
            await m.update_jid("alice", "different@s.whatsapp.net")
    finally:
        await m.close()


async def test_register_clobber_protection(session_dir: Path) -> None:
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice", jid="aaa@s.whatsapp.net")
        with pytest.raises(ManifestError):
            await m.register("alice", jid="bbb@s.whatsapp.net")
    finally:
        await m.close()


async def test_register_upgrade_blank_to_real(session_dir: Path) -> None:
    """允许将空 JID 条目重新注册为真实 JID。
    （这是配对后的流程。）"""
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice")  # jid=""
        await m.register("alice", jid="real@s.whatsapp.net")
        assert m.jid_for("alice") == "real@s.whatsapp.net"
    finally:
        await m.close()


async def test_remove_idempotent(session_dir: Path) -> None:
    m = await Manifest.open(session_dir)
    try:
        await m.register("alice")
        await m.remove("alice")
        await m.remove("alice")  # 重复移除是无操作＋警告
        assert "alice" not in m
    finally:
        await m.close()


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


async def test_concurrent_registers_are_serialised(session_dir: Path) -> None:
    """大量并发的 register() 调用必须都能安全落地。"""
    m = await Manifest.open(session_dir)
    try:
        await asyncio.gather(*[m.register(f"u{i}") for i in range(50)])
        assert len(m) == 50
        assert sorted(m.keys()) == sorted([f"u{i}" for i in range(50)])
    finally:
        await m.close()


# ---------------------------------------------------------------------------
# 跨进程锁
# ---------------------------------------------------------------------------


async def test_second_open_on_same_dir_fails(session_dir: Path) -> None:
    m1 = await Manifest.open(session_dir)
    try:
        with pytest.raises(ManifestError):
            await Manifest.open(session_dir)
    finally:
        await m1.close()

    # 关闭第一个之后，新的打开应当成功。
    m2 = await Manifest.open(session_dir)
    await m2.close()


# ---------------------------------------------------------------------------
# 损坏处理
# ---------------------------------------------------------------------------


async def test_corrupt_json_raises(session_dir: Path) -> None:
    session_dir.mkdir(parents=True)
    (session_dir / "accounts.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ManifestError):
        await Manifest.open(session_dir)


async def test_unknown_version_raises(session_dir: Path) -> None:
    session_dir.mkdir(parents=True)
    (session_dir / "accounts.json").write_text(
        json.dumps({"version": 999, "accounts": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        await Manifest.open(session_dir)


async def test_empty_file_treated_as_empty(session_dir: Path) -> None:
    session_dir.mkdir(parents=True)
    (session_dir / "accounts.json").write_text("", encoding="utf-8")
    m = await Manifest.open(session_dir)
    try:
        assert len(m) == 0
    finally:
        await m.close()
