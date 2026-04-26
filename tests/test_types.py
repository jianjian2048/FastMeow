"""Tests for the public types module: enum mapping and proto round-trips.

These exercise the conversion helpers without spinning up the sidecar:
we synthesise protobuf messages directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp

from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb
from fastmeow.types import (
    Account,
    AccountState,
    ConnectedEvent,
    DisconnectedEvent,
    LoggedOutEvent,
    MessageEvent,
    PairSuccessEvent,
    QREvent,
    SendResult,
    UnknownEvent,
    event_from_proto,
)


def _make_ts(seconds: int = 1_700_000_000) -> Timestamp:
    ts = Timestamp()
    ts.seconds = seconds
    ts.nanos = 0
    return ts


def _base_envelope(seq: int = 7) -> pb.StreamEventsResponse:
    msg = pb.StreamEventsResponse(
        seq=seq,
        sidecar_id="sc-1",
        account_key="alice",
        account_jid="aaa@s.whatsapp.net",
    )
    msg.observed_at.CopyFrom(_make_ts())
    return msg


# ---------------------------------------------------------------------------
# Enum mapping
# ---------------------------------------------------------------------------


def test_account_state_from_proto_known_values() -> None:
    assert AccountState.from_proto(pb.AccountState.STATE_CONNECTED) is AccountState.CONNECTED
    assert AccountState.from_proto(pb.AccountState.STATE_UNPAIRED) is AccountState.UNPAIRED
    assert AccountState.from_proto(pb.AccountState.STATE_LOGGED_OUT) is AccountState.LOGGED_OUT


def test_account_state_unspecified_default() -> None:
    assert AccountState.from_proto(pb.AccountState.STATE_UNSPECIFIED) is AccountState.UNSPECIFIED


def test_account_from_proto() -> None:
    pa = pb.AccountState(
        account_key="alice",
        jid="aaa@s.whatsapp.net",
        state=pb.AccountState.STATE_CONNECTED,
        reason="",
    )
    a = Account.from_proto(pa)
    assert a == Account(
        account_key="alice",
        jid="aaa@s.whatsapp.net",
        state=AccountState.CONNECTED,
        reason="",
    )


# ---------------------------------------------------------------------------
# event_from_proto: each oneof variant
# ---------------------------------------------------------------------------


def test_event_from_proto_message() -> None:
    env = _base_envelope()
    env.message.CopyFrom(
        pb.MessageEvent(
            message_id="MID",
            chat_jid="chat@s.whatsapp.net",
            sender_jid="sender@s.whatsapp.net",
            from_me=False,
            is_group=True,
            text="hello",
            reply_to_message_id="",
        )
    )
    env.message.timestamp.CopyFrom(_make_ts(1_700_000_500))

    out = event_from_proto(env)
    assert isinstance(out, MessageEvent)
    assert out.message_id == "MID"
    assert out.is_group is True
    assert out.text == "hello"
    assert out.timestamp is not None
    assert out.timestamp.tzinfo is UTC


def test_event_from_proto_message_without_timestamp() -> None:
    env = _base_envelope()
    env.message.CopyFrom(pb.MessageEvent(message_id="x", chat_jid="c", text="t"))
    out = event_from_proto(env)
    assert isinstance(out, MessageEvent)
    assert out.timestamp is None


def test_event_from_proto_connected() -> None:
    env = _base_envelope()
    env.connected.CopyFrom(pb.ConnectedEvent())
    out = event_from_proto(env)
    assert isinstance(out, ConnectedEvent)
    assert out.account_key == "alice"


def test_event_from_proto_disconnected() -> None:
    env = _base_envelope()
    env.disconnected.CopyFrom(pb.DisconnectedEvent(reason="nettoss"))
    out = event_from_proto(env)
    assert isinstance(out, DisconnectedEvent)
    assert out.reason == "nettoss"


def test_event_from_proto_qr() -> None:
    env = _base_envelope()
    env.qr.CopyFrom(pb.QREvent(code="2@xxx,yyy", ttl_seconds=20))
    out = event_from_proto(env)
    assert isinstance(out, QREvent)
    assert out.code == "2@xxx,yyy"
    assert out.ttl_seconds == 20


def test_event_from_proto_pair_success() -> None:
    env = _base_envelope()
    env.pair_success.CopyFrom(
        pb.PairSuccessEvent(
            jid="999:7@s.whatsapp.net",
            business_name="ACME",
            platform="web",
        )
    )
    out = event_from_proto(env)
    assert isinstance(out, PairSuccessEvent)
    assert out.jid == "999:7@s.whatsapp.net"
    assert out.business_name == "ACME"


def test_event_from_proto_logged_out() -> None:
    env = _base_envelope()
    env.logged_out.CopyFrom(pb.LoggedOutEvent(reason="user revoked"))
    out = event_from_proto(env)
    assert isinstance(out, LoggedOutEvent)
    assert out.reason == "user revoked"


def test_event_from_proto_unknown() -> None:
    env = _base_envelope()
    env.unknown.CopyFrom(pb.UnknownEvent(go_type="*events.HistorySync"))
    out = event_from_proto(env)
    assert isinstance(out, UnknownEvent)
    assert out.go_type == "*events.HistorySync"


def test_event_from_proto_returns_none_when_oneof_unset() -> None:
    env = _base_envelope()
    # No oneof set on purpose.
    assert event_from_proto(env) is None


# ---------------------------------------------------------------------------
# SendResult
# ---------------------------------------------------------------------------


def test_send_result_from_proto() -> None:
    pr = pb.SendMessageResponse(
        message_id="OUT-1",
        deduped=False,
    )
    pr.server_timestamp.CopyFrom(_make_ts(1_700_000_100))
    sr = SendResult.from_proto(pr)
    assert sr.message_id == "OUT-1"
    assert sr.deduped is False
    assert sr.server_timestamp.tzinfo is UTC
    assert isinstance(sr.server_timestamp, datetime)


def test_message_event_predicates() -> None:
    ev = MessageEvent(
        seq=1,
        sidecar_id="sc",
        account_key="alice",
        account_jid="aaa",
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        message_id="m",
        chat_jid="c",
        sender_jid="s",
        from_me=False,
        is_group=False,
        text="hi",
        reply_to_message_id="QUOTED",
        timestamp=None,
    )
    assert ev.is_dm is True
    assert ev.is_reply is True
