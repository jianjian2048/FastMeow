"""群组管理示例（Phase 4.1）。

从仓库根目录运行::

    python examples/group_admin.py

这演示了：
    * 列出账号已加入的群组（``list_groups``）。
    * 创建新群、修改名称与公告（``create_group`` / ``set_group_name`` /
      ``set_group_topic``）。
    * 添加 / 提升 / 降级 / 移除成员（``add_group_participants`` 等）。
    * 监听群组生命周期事件（``@router.group_info()`` /
      ``@router.joined_group()`` / ``@router.group_participant_update()``）。
    * 通过捕获 ``GroupError`` 子类做精细化错误处理。

首次运行请用 WhatsApp 应用扫描终端打印的二维码
（设置 -> 已关联设备 -> 关联设备）。后续运行 sidecar
会复用 ``./sessions`` 中保存的会话。

请把下方 ``CANDIDATE_PARTICIPANTS`` 改成你自己测试账号
能联系到的真实手机号 JID 后再运行管理动作。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastmeow import (
    AccountClient,
    Ctx,
    FastMeow,
    GroupError,
    GroupInfoEvent,
    GroupParticipantUpdateEvent,
    InvalidJIDError,
    JoinedGroupEvent,
    NotGroupAdminError,
    NotInGroupError,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

router = Router(name="group-admin")


# --- 群组事件监听器 ---------------------------------------------------


@router.group_info()
async def on_group_info(event: GroupInfoEvent, ctx: Ctx) -> None:
    """群组元数据（标题/公告/管理员）变更时触发。"""
    info = event.group_info
    logging.info(
        "[%s] group_info: %s name=%r topic=%r",
        ctx.account_key,
        info.jid,
        info.name,
        info.topic,
    )


@router.joined_group()
async def on_joined_group(event: JoinedGroupEvent, ctx: Ctx) -> None:
    """当前账号被加入到某个群组时触发。"""
    info = event.group_info
    logging.info(
        "[%s] joined_group: %s name=%r participants=%d reason=%s",
        ctx.account_key,
        info.jid,
        info.name,
        len(info.participants),
        event.join_reason or "(unknown)",
    )


@router.group_participant_update()
async def on_group_participants(
    event: GroupParticipantUpdateEvent,
    ctx: Ctx,
) -> None:
    """群成员加入 / 退出 / 被踢 / 被提升时触发。"""
    logging.info(
        "[%s] group_participant_update: %s action=%s targets=%s actor=%s",
        ctx.account_key,
        event.group_jid,
        event.action.name,
        event.participant_jids,
        event.actor_jid or "(system)",
    )


# --- 一次性管理动作 -------------------------------------------------------


# 在这里填入你自己测试号能联系到的 JID（形如 "8613800000000@s.whatsapp.net"）。
# 留空时只会列出已有群组，不执行写操作。
CANDIDATE_PARTICIPANTS: list[str] = []


async def run_admin_demo(client: AccountClient, account_key: str) -> None:
    """演示 Phase 4.1 的写操作。需要至少 1 个候选成员才能创建群。"""
    # 1. 列出已加入的群。
    try:
        groups = await client.list_groups()
    except GroupError:
        logging.exception("[%s] list_groups failed", account_key)
        return

    logging.info("[%s] joined %d group(s):", account_key, len(groups))
    for g in groups:
        logging.info(
            "  - %s  name=%r  participants=%d",
            g.jid,
            g.name,
            len(g.participants),
        )

    if not CANDIDATE_PARTICIPANTS:
        logging.info(
            "[%s] CANDIDATE_PARTICIPANTS 为空，跳过写操作演示。"
            " 请在脚本中填入真实 JID 后重试。",
            account_key,
        )
        return

    # 2. 创建群。
    try:
        new_group = await client.create_group(
            name="FastMeow demo",
            participants=CANDIDATE_PARTICIPANTS,
        )
    except InvalidJIDError:
        logging.exception("[%s] create_group: 提供的 JID 非法", account_key)
        return
    except GroupError:
        logging.exception("[%s] create_group failed", account_key)
        return

    logging.info("[%s] created group %s", account_key, new_group.jid)

    # 3. 改名 + 改公告。
    try:
        await client.set_group_name(new_group.jid, "FastMeow demo (renamed)")
        await client.set_group_topic(
            new_group.jid,
            "managed by fastmeow examples/group_admin.py",
        )
    except NotGroupAdminError:
        logging.warning("[%s] 没有管理员权限，跳过改名 / 改公告", account_key)
    except GroupError:
        logging.exception("[%s] update group metadata failed", account_key)

    # 4. 提升第一个候选成员为管理员，再降级回普通成员。
    first = CANDIDATE_PARTICIPANTS[0]
    try:
        await client.promote_group_participants(new_group.jid, [first])
        await client.demote_group_participants(new_group.jid, [first])
    except (NotGroupAdminError, NotInGroupError):
        logging.warning("[%s] 权限或成员状态不满足，跳过升降级", account_key)
    except GroupError:
        logging.exception("[%s] promote/demote failed", account_key)

    # 5. 拿邀请链接（只读）。
    try:
        link = await client.get_group_invite_link(new_group.jid)
        logging.info("[%s] invite link: %s", account_key, link)
    except GroupError:
        logging.exception("[%s] get_group_invite_link failed", account_key)

    # 6. 离开演示群（避免堆积）。
    try:
        await client.leave_group(new_group.jid)
        logging.info("[%s] left demo group", account_key)
    except GroupError:
        logging.exception("[%s] leave_group failed", account_key)


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")

        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")

        # 跑一次性的管理动作演示。
        await run_admin_demo(handle.client, handle.account_key)

        print("listening for group events; Ctrl+C to exit.")
        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
