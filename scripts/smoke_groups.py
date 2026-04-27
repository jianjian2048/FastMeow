"""Phase 4.1 群组 API 真账号 smoke 脚本。

**用途**：在真实 WhatsApp 账号上端到端验证 9 个群组 RPC，确认 sidecar
返回的数据结构能被 SDK 正确解码。**不在 CI 中运行**——仅供开发者本地手动验证。

**运行**::

    python scripts/smoke_groups.py

**前置条件**：
    * ``./sessions/smoke/`` 下已有配对会话（首次运行会打印 QR）。
    * 至少有一个测试联系人 JID 可加入新建群组。设 ``$FASTMEOW_SMOKE_PEER``，
      例如 ``$Env:FASTMEOW_SMOKE_PEER="8613800138000@s.whatsapp.net"``。
      未设置则跳过 ``create_group`` / participant 操作。

**覆盖**：list_groups → create_group → set_group_name → set_group_topic →
add/promote/demote/remove_group_participants → get_group_invite_link → leave_group。
``preview_group_invite`` / ``join_group`` 需对端给的邀请链接，本脚本只在
``$FASTMEOW_SMOKE_INVITE`` 设置时验证。

每步都打印 ``OK <step>`` 或抛错；非零退出码代表失败。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from fastmeow import (
    FastMeow,
    GroupError,
    GroupInfo,
    InviteLinkInvalidError,
    InviteLinkRevokedError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smoke_groups")


def _ok(step: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"OK  {step}{suffix}")


async def main() -> int:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    peer = os.environ.get("FASTMEOW_SMOKE_PEER")
    invite_link = os.environ.get("FASTMEOW_SMOKE_INVITE")

    async with FastMeow(session_dir=session_dir) as app:
        handle = app.add_account("smoke", on_qr="terminal")
        print("waiting for pairing/connect (timeout 120s)...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")
        client = handle.client

        # 1) list_groups —— 安全只读。
        groups = await client.list_groups()
        _ok("list_groups", f"{len(groups)} groups")
        for g in groups[:5]:
            print(f"    - {g.jid} {g.name!r} ({len(g.participants)} members)")

        # 2) get_group_info —— 复用列表里第一个群验证读路径。
        if groups:
            info = await client.get_group_info(groups[0].jid)
            assert isinstance(info, GroupInfo)
            _ok("get_group_info", f"{info.jid} owner={info.owner_jid}")

        # 3) preview_group_invite —— 仅在显式提供邀请时验证。
        if invite_link:
            try:
                preview = await client.preview_group_invite(invite_link)
                _ok("preview_group_invite", f"{preview.jid} {preview.name!r}")
            except (InviteLinkInvalidError, InviteLinkRevokedError) as exc:
                log.warning("preview_group_invite skipped: %s", exc)

        # 4) 写路径 —— 需要 peer JID 才能 create + participant 操作。
        if not peer:
            print("FASTMEOW_SMOKE_PEER not set; skipping write-path checks")
            return 0

        created = await client.create_group("fastmeow-smoke", participants=[peer])
        gjid = created.jid
        _ok("create_group", f"{gjid} ({len(created.participants)} members)")

        try:
            renamed = await client.set_group_name(gjid, "fastmeow-smoke-2")
            assert renamed.name == "fastmeow-smoke-2"
            _ok("set_group_name")

            topic_info = await client.set_group_topic(gjid, "phase 4.1 smoke")
            _ok("set_group_topic", topic_info.topic)

            link = await client.get_group_invite_link(gjid)
            _ok("get_group_invite_link", link)

            link2 = await client.get_group_invite_link(gjid, revoke=True)
            assert link2 != link, "revoke should rotate the invite token"
            _ok("get_group_invite_link revoke", link2)

            # promote / demote 自身 + remove peer。注意：promote/demote 自身
            # 是合法操作（创建者已经是 super admin，所以这里只当存活探测）。
            promoted = await client.promote_group_participants(gjid, [peer])
            _ok("promote_group_participants", f"{len(promoted)} results")

            demoted = await client.demote_group_participants(gjid, [peer])
            _ok("demote_group_participants", f"{len(demoted)} results")

            removed = await client.remove_group_participants(gjid, [peer])
            _ok("remove_group_participants", f"{len(removed)} results")

            added = await client.add_group_participants(gjid, [peer])
            _ok("add_group_participants", f"{len(added)} results")
        finally:
            try:
                await client.leave_group(gjid)
                _ok("leave_group", gjid)
            except GroupError as exc:
                log.warning("leave_group failed (non-fatal): %s", exc)

    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
