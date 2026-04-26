import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PingRequest(_message.Message):
    __slots__ = ("client_protocol_version",)
    CLIENT_PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    client_protocol_version: int
    def __init__(self, client_protocol_version: _Optional[int] = ...) -> None: ...

class PingResponse(_message.Message):
    __slots__ = ("server_protocol_version", "sidecar_version", "whatsmeow_version", "sidecar_id")
    SERVER_PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    SIDECAR_VERSION_FIELD_NUMBER: _ClassVar[int]
    WHATSMEOW_VERSION_FIELD_NUMBER: _ClassVar[int]
    SIDECAR_ID_FIELD_NUMBER: _ClassVar[int]
    server_protocol_version: int
    sidecar_version: str
    whatsmeow_version: str
    sidecar_id: str
    def __init__(self, server_protocol_version: _Optional[int] = ..., sidecar_version: _Optional[str] = ..., whatsmeow_version: _Optional[str] = ..., sidecar_id: _Optional[str] = ...) -> None: ...

class AccountState(_message.Message):
    __slots__ = ("account_key", "jid", "state", "reason")
    class State(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STATE_UNSPECIFIED: _ClassVar[AccountState.State]
        STATE_UNPAIRED: _ClassVar[AccountState.State]
        STATE_PAIRING: _ClassVar[AccountState.State]
        STATE_CONNECTING: _ClassVar[AccountState.State]
        STATE_CONNECTED: _ClassVar[AccountState.State]
        STATE_DISCONNECTED: _ClassVar[AccountState.State]
        STATE_LOGGED_OUT: _ClassVar[AccountState.State]
        STATE_RECOVERING: _ClassVar[AccountState.State]
    STATE_UNSPECIFIED: AccountState.State
    STATE_UNPAIRED: AccountState.State
    STATE_PAIRING: AccountState.State
    STATE_CONNECTING: AccountState.State
    STATE_CONNECTED: AccountState.State
    STATE_DISCONNECTED: AccountState.State
    STATE_LOGGED_OUT: AccountState.State
    STATE_RECOVERING: AccountState.State
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    JID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    account_key: str
    jid: str
    state: AccountState.State
    reason: str
    def __init__(self, account_key: _Optional[str] = ..., jid: _Optional[str] = ..., state: _Optional[_Union[AccountState.State, str]] = ..., reason: _Optional[str] = ...) -> None: ...

class EnsureAccountRequest(_message.Message):
    __slots__ = ("account_key", "display_name", "jid")
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    JID_FIELD_NUMBER: _ClassVar[int]
    account_key: str
    display_name: str
    jid: str
    def __init__(self, account_key: _Optional[str] = ..., display_name: _Optional[str] = ..., jid: _Optional[str] = ...) -> None: ...

class EnsureAccountResponse(_message.Message):
    __slots__ = ("state", "created")
    STATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    state: AccountState
    created: bool
    def __init__(self, state: _Optional[_Union[AccountState, _Mapping]] = ..., created: bool = ...) -> None: ...

class ConnectRequest(_message.Message):
    __slots__ = ("account_key",)
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    account_key: str
    def __init__(self, account_key: _Optional[str] = ...) -> None: ...

class ConnectResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: AccountState
    def __init__(self, state: _Optional[_Union[AccountState, _Mapping]] = ...) -> None: ...

class DisconnectRequest(_message.Message):
    __slots__ = ("account_key",)
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    account_key: str
    def __init__(self, account_key: _Optional[str] = ...) -> None: ...

class DisconnectResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: AccountState
    def __init__(self, state: _Optional[_Union[AccountState, _Mapping]] = ...) -> None: ...

class LogoutRequest(_message.Message):
    __slots__ = ("account_key",)
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    account_key: str
    def __init__(self, account_key: _Optional[str] = ...) -> None: ...

class LogoutResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: AccountState
    def __init__(self, state: _Optional[_Union[AccountState, _Mapping]] = ...) -> None: ...

class SendMessageRequest(_message.Message):
    __slots__ = ("account_key", "to_jid", "client_msg_id", "text")
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    TO_JID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MSG_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    account_key: str
    to_jid: str
    client_msg_id: str
    text: TextBody
    def __init__(self, account_key: _Optional[str] = ..., to_jid: _Optional[str] = ..., client_msg_id: _Optional[str] = ..., text: _Optional[_Union[TextBody, _Mapping]] = ...) -> None: ...

class TextBody(_message.Message):
    __slots__ = ("body", "reply_to_message_id")
    BODY_FIELD_NUMBER: _ClassVar[int]
    REPLY_TO_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    body: str
    reply_to_message_id: str
    def __init__(self, body: _Optional[str] = ..., reply_to_message_id: _Optional[str] = ...) -> None: ...

class SendMessageResponse(_message.Message):
    __slots__ = ("message_id", "server_timestamp", "deduped")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    DEDUPED_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    server_timestamp: _timestamp_pb2.Timestamp
    deduped: bool
    def __init__(self, message_id: _Optional[str] = ..., server_timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., deduped: bool = ...) -> None: ...

class StreamEventsRequest(_message.Message):
    __slots__ = ("include_soft_events", "resume_after_seq")
    INCLUDE_SOFT_EVENTS_FIELD_NUMBER: _ClassVar[int]
    RESUME_AFTER_SEQ_FIELD_NUMBER: _ClassVar[int]
    include_soft_events: bool
    resume_after_seq: int
    def __init__(self, include_soft_events: bool = ..., resume_after_seq: _Optional[int] = ...) -> None: ...

class StreamEventsResponse(_message.Message):
    __slots__ = ("seq", "sidecar_id", "account_key", "account_jid", "observed_at", "connected", "disconnected", "qr", "pair_success", "message", "logged_out", "unknown")
    SEQ_FIELD_NUMBER: _ClassVar[int]
    SIDECAR_ID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_JID_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    DISCONNECTED_FIELD_NUMBER: _ClassVar[int]
    QR_FIELD_NUMBER: _ClassVar[int]
    PAIR_SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    LOGGED_OUT_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    seq: int
    sidecar_id: str
    account_key: str
    account_jid: str
    observed_at: _timestamp_pb2.Timestamp
    connected: ConnectedEvent
    disconnected: DisconnectedEvent
    qr: QREvent
    pair_success: PairSuccessEvent
    message: MessageEvent
    logged_out: LoggedOutEvent
    unknown: UnknownEvent
    def __init__(self, seq: _Optional[int] = ..., sidecar_id: _Optional[str] = ..., account_key: _Optional[str] = ..., account_jid: _Optional[str] = ..., observed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., connected: _Optional[_Union[ConnectedEvent, _Mapping]] = ..., disconnected: _Optional[_Union[DisconnectedEvent, _Mapping]] = ..., qr: _Optional[_Union[QREvent, _Mapping]] = ..., pair_success: _Optional[_Union[PairSuccessEvent, _Mapping]] = ..., message: _Optional[_Union[MessageEvent, _Mapping]] = ..., logged_out: _Optional[_Union[LoggedOutEvent, _Mapping]] = ..., unknown: _Optional[_Union[UnknownEvent, _Mapping]] = ...) -> None: ...

class ConnectedEvent(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DisconnectedEvent(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class QREvent(_message.Message):
    __slots__ = ("code", "ttl_seconds")
    CODE_FIELD_NUMBER: _ClassVar[int]
    TTL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    code: str
    ttl_seconds: int
    def __init__(self, code: _Optional[str] = ..., ttl_seconds: _Optional[int] = ...) -> None: ...

class PairSuccessEvent(_message.Message):
    __slots__ = ("jid", "business_name", "platform")
    JID_FIELD_NUMBER: _ClassVar[int]
    BUSINESS_NAME_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    jid: str
    business_name: str
    platform: str
    def __init__(self, jid: _Optional[str] = ..., business_name: _Optional[str] = ..., platform: _Optional[str] = ...) -> None: ...

class LoggedOutEvent(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class MessageEvent(_message.Message):
    __slots__ = ("message_id", "chat_jid", "sender_jid", "from_me", "timestamp", "is_group", "text", "reply_to_message_id")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    CHAT_JID_FIELD_NUMBER: _ClassVar[int]
    SENDER_JID_FIELD_NUMBER: _ClassVar[int]
    FROM_ME_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    IS_GROUP_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    REPLY_TO_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    chat_jid: str
    sender_jid: str
    from_me: bool
    timestamp: _timestamp_pb2.Timestamp
    is_group: bool
    text: str
    reply_to_message_id: str
    def __init__(self, message_id: _Optional[str] = ..., chat_jid: _Optional[str] = ..., sender_jid: _Optional[str] = ..., from_me: bool = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., is_group: bool = ..., text: _Optional[str] = ..., reply_to_message_id: _Optional[str] = ...) -> None: ...

class UnknownEvent(_message.Message):
    __slots__ = ("go_type",)
    GO_TYPE_FIELD_NUMBER: _ClassVar[int]
    go_type: str
    def __init__(self, go_type: _Optional[str] = ...) -> None: ...

class ShutdownRequest(_message.Message):
    __slots__ = ("grace_ms",)
    GRACE_MS_FIELD_NUMBER: _ClassVar[int]
    grace_ms: int
    def __init__(self, grace_ms: _Optional[int] = ...) -> None: ...

class ShutdownResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
