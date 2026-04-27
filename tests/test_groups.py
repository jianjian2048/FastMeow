"""Phase 4.1 群组 API 单测。

覆盖三层：

1. ``types`` —— ``GroupParticipantAction`` 枚举往返、``GroupInfo`` /
   ``GroupParticipant`` / ``GroupParticipantUpdateResult`` 的 ``from_proto``、
   以及三个新群组事件的 ``event_from_proto`` 分支。
2. ``router`` —— 三个新装饰器 (``joined_group`` / ``group_info`` /
   ``group_participant_update``) 派发到正确事件类型并支持单参形态。
3. ``_transport._translate`` —— 群组 RPC 的 gRPC 错误码到公开异常的映射。
4. ``_BoundClient`` —— 9 个 ``AccountClient`` 群组方法把参数正确转发到 transport，
   并用 enum (而非字符串) 传递 ``action``。

测试不接触网络：FakeTransport / FakeRpcError 在内存中模拟 transport 与 grpc。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import grpc
import pytest

from fastmeow._dispatcher import _BoundClient
from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb
from fastmeow._transport import _translate
from fastmeow.exceptions import (
    GroupError,
    GroupPermissionError,
    InvalidJIDError,
    InviteLinkInvalidError,
    InviteLinkRevokedError,
)
from fastmeow.router import Router
from fastmeow.types import (
    GroupInfo,
    GroupInfoEvent,
    GroupParticipant,
    GroupParticipantAction,
    GroupParticipantUpdateEvent,
    GroupParticipantUpdateResult,
    JoinedGroupEvent,
    event_from_proto,
)

# ---------------------------------------------------------------------------
# Helpers: proto envelopes
# ---------------------------------------------------------------------------


def _make_pb_group_info(
    *,
    jid: str = "g@g.us",
    name: str = "Ops",
    participants: tuple[tuple[str, bool, bool], ...] = (),
    creation_seconds: int | None = 1_700_000_000,
) -> pb.GroupInfo:
    msg = pb.GroupInfo(
        jid=jid,
        name=name,
        topic="topic",
        owner_jid="owner@s.whatsapp.net",
        is_announce=False,
        is_locked=False,
        is_ephemeral=False,
        ephemeral_duration_seconds=0,
        membership_approval_mode="off",
    )
    if creation_seconds is not None:
        msg.creation_timestamp.seconds = creation_seconds
    for p_jid, is_admin, is_super in participants:
        p = msg.participants.add()
        p.jid = p_jid
        p.is_admin = is_admin
        p.is_super_admin = is_super
    return msg


def _envelope(seq: int = 1) -> pb.StreamEventsResponse:
    env = pb.StreamEventsResponse(
        seq=seq,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
    )
    env.observed_at.seconds = 1_700_000_000
    return env


# ---------------------------------------------------------------------------
# Types — enum & dataclass round-trips
# ---------------------------------------------------------------------------


def test_group_participant_action_round_trip() -> None:
    for action in GroupParticipantAction:
        proto_val = getattr(
            pb.GroupParticipantUpdateEvent.GroupParticipantAction,
            f"GROUP_PARTICIPANT_ACTION_{action.value}",
        )
        assert GroupParticipantAction.from_proto(proto_val) is action


def test_group_participant_action_unknown_falls_back_to_unspecified() -> None:
    # Any value the proto doesn't know about should not crash.
    assert GroupParticipantAction.from_proto(9999) is GroupParticipantAction.UNSPECIFIED


def test_group_participant_from_proto() -> None:
    p = pb.GroupParticipant(jid="x@s.whatsapp.net", is_admin=True, is_super_admin=False)
    out = GroupParticipant.from_proto(p)
    assert out == GroupParticipant(jid="x@s.whatsapp.net", is_admin=True)


def test_group_info_from_proto_with_timestamp() -> None:
    msg = _make_pb_group_info(
        participants=(
            ("a@s.whatsapp.net", False, False),
            ("b@s.whatsapp.net", True, False),
        )
    )
    info = GroupInfo.from_proto(msg)
    assert info.jid == "g@g.us"
    assert info.name == "Ops"
    assert info.creation_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert len(info.participants) == 2
    assert info.participants[1].is_admin is True


def test_group_info_from_proto_without_timestamp() -> None:
    msg = _make_pb_group_info(creation_seconds=None)
    info = GroupInfo.from_proto(msg)
    assert info.creation_time is None


def test_group_participant_update_result_from_proto() -> None:
    msg = pb.GroupParticipantUpdateResult(
        jid="z@s.whatsapp.net",
        success=False,
        error_code="403",
        error_message="not authorized",
    )
    out = GroupParticipantUpdateResult.from_proto(msg)
    assert out.success is False
    assert out.error_code == "403"


# ---------------------------------------------------------------------------
# event_from_proto — three new oneof branches
# ---------------------------------------------------------------------------


def test_event_from_proto_joined_group() -> None:
    env = _envelope()
    env.joined_group.group_info.CopyFrom(_make_pb_group_info())
    env.joined_group.join_reason = "invite"
    out = event_from_proto(env)
    assert isinstance(out, JoinedGroupEvent)
    assert out.group_info.jid == "g@g.us"
    assert out.join_reason == "invite"


def test_event_from_proto_group_info_change() -> None:
    env = _envelope()
    env.group_info.group_info.CopyFrom(_make_pb_group_info(name="renamed"))
    out = event_from_proto(env)
    assert isinstance(out, GroupInfoEvent)
    assert out.group_info.name == "renamed"


def test_event_from_proto_group_participant_update() -> None:
    env = _envelope()
    env.group_participant_update.group_jid = "g@g.us"
    env.group_participant_update.action = (
        pb.GroupParticipantUpdateEvent.GROUP_PARTICIPANT_ACTION_ADD
    )
    env.group_participant_update.participant_jids.append("new@s.whatsapp.net")
    env.group_participant_update.actor_jid = "admin@s.whatsapp.net"
    out = event_from_proto(env)
    assert isinstance(out, GroupParticipantUpdateEvent)
    assert out.action is GroupParticipantAction.ADD
    assert out.participant_jids == ("new@s.whatsapp.net",)
    assert out.actor_jid == "admin@s.whatsapp.net"


# ---------------------------------------------------------------------------
# Router — three new decorators
# ---------------------------------------------------------------------------


async def test_router_joined_group_decorator() -> None:
    router = Router()
    seen: list[JoinedGroupEvent] = []

    @router.joined_group()
    async def on_joined(event: JoinedGroupEvent) -> None:
        seen.append(event)

    env = _envelope()
    env.joined_group.group_info.CopyFrom(_make_pb_group_info())
    ev = event_from_proto(env)
    assert ev is not None
    await router.dispatch(ev, ctx=cast(Any, AsyncMock()))
    assert len(seen) == 1
    assert seen[0].group_info.jid == "g@g.us"


async def test_router_group_info_decorator() -> None:
    router = Router()
    seen: list[GroupInfoEvent] = []

    @router.group_info()
    async def on_info(event: GroupInfoEvent) -> None:
        seen.append(event)

    env = _envelope()
    env.group_info.group_info.CopyFrom(_make_pb_group_info(name="x"))
    ev = event_from_proto(env)
    assert ev is not None
    await router.dispatch(ev, ctx=cast(Any, AsyncMock()))
    assert len(seen) == 1


async def test_router_group_participant_update_decorator() -> None:
    router = Router()
    seen: list[GroupParticipantUpdateEvent] = []

    @router.group_participant_update()
    async def on_update(event: GroupParticipantUpdateEvent) -> None:
        seen.append(event)

    env = _envelope()
    env.group_participant_update.group_jid = "g@g.us"
    env.group_participant_update.action = (
        pb.GroupParticipantUpdateEvent.GROUP_PARTICIPANT_ACTION_REMOVE
    )
    env.group_participant_update.participant_jids.append("kicked@s.whatsapp.net")
    ev = event_from_proto(env)
    assert ev is not None
    await router.dispatch(ev, ctx=cast(Any, AsyncMock()))
    assert len(seen) == 1
    assert seen[0].action is GroupParticipantAction.REMOVE


# ---------------------------------------------------------------------------
# _translate — group RPC error mapping
# ---------------------------------------------------------------------------


class _FakeRpcError(grpc.aio.AioRpcError):
    """grpc.aio.AioRpcError 的最小可构造替身 —— 只暴露 code()/details()。"""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        # 跳过父类 __init__（它需要复杂 metadata），直接设置内部字段。
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:  # type: ignore[override]
        return self._code

    def details(self) -> str:  # type: ignore[override]
        return self._details


def test_translate_group_permission_denied() -> None:
    exc = _FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, "not admin")
    out = _translate(exc, op="update_group_settings", account_key="alice")
    assert isinstance(out, GroupPermissionError)


def test_translate_group_invite_invalid() -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "invite link malformed")
    out = _translate(exc, op="join_group_via_invite", account_key="alice")
    assert isinstance(out, InviteLinkInvalidError)


def test_translate_group_invalid_jid() -> None:
    exc = _FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "bad jid format")
    out = _translate(exc, op="get_group_info", account_key="alice")
    assert isinstance(out, InvalidJIDError)


def test_translate_group_invite_revoked() -> None:
    exc = _FakeRpcError(grpc.StatusCode.NOT_FOUND, "invite revoked")
    out = _translate(exc, op="preview_group_invite", account_key="alice")
    assert isinstance(out, InviteLinkRevokedError)


def test_translate_group_not_found_falls_back_to_group_error() -> None:
    # account_key 缺失时 sidecar 会先在 INVALID_ARGUMENT 阶段拒绝；
    # 群组 RPC 上的 NOT_FOUND 实际上意味着“群组不存在”。
    exc = _FakeRpcError(grpc.StatusCode.NOT_FOUND, "group does not exist")
    out = _translate(exc, op="get_group_info", account_key="alice")
    assert isinstance(out, GroupError)
    assert not isinstance(out, InviteLinkRevokedError)


def test_translate_group_internal_falls_back_to_group_error() -> None:
    exc = _FakeRpcError(grpc.StatusCode.INTERNAL, "boom")
    out = _translate(exc, op="leave_group", account_key="alice")
    assert isinstance(out, GroupError)


# ---------------------------------------------------------------------------
# _BoundClient — group method forwarding
# ---------------------------------------------------------------------------


def _make_client(transport: Any) -> _BoundClient:
    return _BoundClient(_transport=transport, account_key="alice", jid="aaa")


async def test_bound_client_list_groups_uses_positional_account_key() -> None:
    transport: Any = AsyncMock()
    transport.list_joined_groups.return_value = ()
    out = await _make_client(transport).list_groups()
    transport.list_joined_groups.assert_awaited_once_with("alice")
    assert out == ()


async def test_bound_client_get_group_info_forwards_kwargs() -> None:
    transport: Any = AsyncMock()
    info = GroupInfo.from_proto(_make_pb_group_info())
    transport.get_group_info.return_value = info

    out = await _make_client(transport).get_group_info("g@g.us")
    transport.get_group_info.assert_awaited_once_with(account_key="alice", group_jid="g@g.us")
    assert out is info


async def test_bound_client_preview_invite_uses_invite_link_kwarg() -> None:
    transport: Any = AsyncMock()
    transport.preview_group_invite.return_value = GroupInfo.from_proto(_make_pb_group_info())
    await _make_client(transport).preview_group_invite("https://chat.whatsapp.com/ABC")
    transport.preview_group_invite.assert_awaited_once_with(
        account_key="alice", invite_link="https://chat.whatsapp.com/ABC"
    )


async def test_bound_client_join_group_returns_jid_string() -> None:
    transport: Any = AsyncMock()
    transport.join_group_via_invite.return_value = "g@g.us"
    out = await _make_client(transport).join_group("ABC")
    transport.join_group_via_invite.assert_awaited_once_with(account_key="alice", invite_link="ABC")
    assert out == "g@g.us"


async def test_bound_client_leave_group() -> None:
    transport: Any = AsyncMock()
    transport.leave_group.return_value = None
    out = await _make_client(transport).leave_group("g@g.us")
    transport.leave_group.assert_awaited_once_with(account_key="alice", group_jid="g@g.us")
    assert out is None


async def test_bound_client_create_group_passes_tuple() -> None:
    transport: Any = AsyncMock()
    transport.create_group.return_value = GroupInfo.from_proto(_make_pb_group_info())
    await _make_client(transport).create_group(
        "Ops", participants=["a@s.whatsapp.net", "b@s.whatsapp.net"]
    )
    transport.create_group.assert_awaited_once_with(
        account_key="alice",
        name="Ops",
        participant_jids=("a@s.whatsapp.net", "b@s.whatsapp.net"),
    )


async def test_bound_client_set_group_name_only_passes_name_field() -> None:
    transport: Any = AsyncMock()
    transport.update_group_settings.return_value = GroupInfo.from_proto(_make_pb_group_info())
    await _make_client(transport).set_group_name("g@g.us", "renamed")
    transport.update_group_settings.assert_awaited_once_with(
        account_key="alice", group_jid="g@g.us", name="renamed"
    )


async def test_bound_client_set_group_topic_only_passes_topic_field() -> None:
    transport: Any = AsyncMock()
    transport.update_group_settings.return_value = GroupInfo.from_proto(_make_pb_group_info())
    await _make_client(transport).set_group_topic("g@g.us", "hello topic")
    transport.update_group_settings.assert_awaited_once_with(
        account_key="alice", group_jid="g@g.us", topic="hello topic"
    )


async def test_bound_client_set_group_announce_passes_bool() -> None:
    transport: Any = AsyncMock()
    transport.update_group_settings.return_value = GroupInfo.from_proto(_make_pb_group_info())
    await _make_client(transport).set_group_announce("g@g.us", True)
    transport.update_group_settings.assert_awaited_once_with(
        account_key="alice", group_jid="g@g.us", is_announce=True
    )


async def test_bound_client_set_group_locked_passes_bool() -> None:
    transport: Any = AsyncMock()
    transport.update_group_settings.return_value = GroupInfo.from_proto(_make_pb_group_info())
    await _make_client(transport).set_group_locked("g@g.us", False)
    transport.update_group_settings.assert_awaited_once_with(
        account_key="alice", group_jid="g@g.us", is_locked=False
    )


@pytest.mark.parametrize(
    ("method", "expected_action"),
    [
        ("add_group_participants", GroupParticipantAction.ADD),
        ("remove_group_participants", GroupParticipantAction.REMOVE),
        ("promote_group_participants", GroupParticipantAction.PROMOTE),
        ("demote_group_participants", GroupParticipantAction.DEMOTE),
    ],
)
async def test_bound_client_participant_methods_pass_enum_action(
    method: str, expected_action: GroupParticipantAction
) -> None:
    transport: Any = AsyncMock()
    transport.update_group_participants.return_value = ()
    client = _make_client(transport)
    await getattr(client, method)("g@g.us", ["x@s.whatsapp.net"])
    transport.update_group_participants.assert_awaited_once_with(
        account_key="alice",
        group_jid="g@g.us",
        action=expected_action,
        participant_jids=("x@s.whatsapp.net",),
    )


async def test_bound_client_get_group_invite_link_translates_revoke_to_reset() -> None:
    transport: Any = AsyncMock()
    transport.get_group_invite_link.return_value = "https://chat.whatsapp.com/XYZ"
    out = await _make_client(transport).get_group_invite_link("g@g.us", revoke=True)
    transport.get_group_invite_link.assert_awaited_once_with(
        account_key="alice", group_jid="g@g.us", reset=True
    )
    assert out == "https://chat.whatsapp.com/XYZ"


async def test_bound_client_get_group_invite_link_default_no_reset() -> None:
    transport: Any = AsyncMock()
    transport.get_group_invite_link.return_value = "https://chat.whatsapp.com/XYZ"
    await _make_client(transport).get_group_invite_link("g@g.us")
    transport.get_group_invite_link.assert_awaited_once_with(
        account_key="alice", group_jid="g@g.us", reset=False
    )
