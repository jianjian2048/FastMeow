// handler_test.go 覆盖 internal/presence/handler.go 中不依赖
// *whatsmeow.Client 网络往返的逻辑：3 个 RPC 的入参校验路径
// （nil req、UNSPECIFIED enum、非法 JID）以及 nil-cli 防御断言。
//
// 参数校验已被刻意放在 nil-cli 检查之前，因此用 nil cli 即可单测纯
// 参数校验路径，不需要伪造 whatsmeow.Client。涉及真实 WebSocket 往返
// 的成功路径在 scripts/smoke_receipts_presence.py 中以真账号 smoke 验证。
package presence

import (
	"context"
	"errors"
	"strings"
	"testing"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// ── SendPresence ─────────────────────────────────────────────────────────────

func TestHandler_SendPresence_NilRequest(t *testing.T) {
	h := NewHandler()
	_, err := h.SendPresence(context.Background(), nil, nil)
	if err == nil || !strings.Contains(err.Error(), "nil SendPresenceRequest") {
		t.Fatalf("nil req: got %v", err)
	}
}

func TestHandler_SendPresence_UnspecifiedType(t *testing.T) {
	h := NewHandler()
	_, err := h.SendPresence(context.Background(), nil, &pb.SendPresenceRequest{
		Presence: pb.PresenceType_PRESENCE_TYPE_UNSPECIFIED,
	})
	if !errors.Is(err, ErrInvalidPresenceType) {
		t.Fatalf("UNSPECIFIED: want ErrInvalidPresenceType, got %v", err)
	}
}

func TestHandler_SendPresence_NilClient(t *testing.T) {
	h := NewHandler()
	_, err := h.SendPresence(context.Background(), nil, &pb.SendPresenceRequest{
		Presence: pb.PresenceType_PRESENCE_TYPE_AVAILABLE,
	})
	if err == nil || !strings.Contains(err.Error(), "nil whatsmeow.Client") {
		t.Fatalf("nil cli: got %v", err)
	}
}

// ── SendChatPresence ─────────────────────────────────────────────────────────

func TestHandler_SendChatPresence_NilRequest(t *testing.T) {
	h := NewHandler()
	_, err := h.SendChatPresence(context.Background(), nil, nil)
	if err == nil || !strings.Contains(err.Error(), "nil SendChatPresenceRequest") {
		t.Fatalf("nil req: got %v", err)
	}
}

func TestHandler_SendChatPresence_InvalidJID(t *testing.T) {
	h := NewHandler()

	// 空 JID。
	_, err := h.SendChatPresence(context.Background(), nil, &pb.SendChatPresenceRequest{
		ChatJid: "",
		State:   pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING,
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("empty: want ErrInvalidJID, got %v", err)
	}

	// 非法格式。
	_, err = h.SendChatPresence(context.Background(), nil, &pb.SendChatPresenceRequest{
		ChatJid: "a:b:c@s.whatsapp.net",
		State:   pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING,
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("malformed: want ErrInvalidJID, got %v", err)
	}
}

func TestHandler_SendChatPresence_UnspecifiedState(t *testing.T) {
	h := NewHandler()
	_, err := h.SendChatPresence(context.Background(), nil, &pb.SendChatPresenceRequest{
		ChatJid: "12345@s.whatsapp.net",
		State:   pb.ChatPresenceState_CHAT_PRESENCE_STATE_UNSPECIFIED,
		Media:   pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT,
	})
	if !errors.Is(err, ErrInvalidChatPresenceState) {
		t.Fatalf("UNSPECIFIED state: want ErrInvalidChatPresenceState, got %v", err)
	}
}

func TestHandler_SendChatPresence_MediaUnspecifiedAccepted(t *testing.T) {
	// media UNSPECIFIED 不应报错（兜底为 TEXT）；其它参数全合法 →
	// nil-cli 成为唯一不合规处。
	h := NewHandler()
	_, err := h.SendChatPresence(context.Background(), nil, &pb.SendChatPresenceRequest{
		ChatJid: "12345@s.whatsapp.net",
		State:   pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING,
		Media:   pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_UNSPECIFIED,
	})
	if err == nil || !strings.Contains(err.Error(), "nil whatsmeow.Client") {
		t.Fatalf("media UNSPECIFIED should pass through to nil-cli check; got %v", err)
	}
}

func TestHandler_SendChatPresence_NilClient(t *testing.T) {
	h := NewHandler()
	_, err := h.SendChatPresence(context.Background(), nil, &pb.SendChatPresenceRequest{
		ChatJid: "12345@s.whatsapp.net",
		State:   pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING,
		Media:   pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT,
	})
	if err == nil || !strings.Contains(err.Error(), "nil whatsmeow.Client") {
		t.Fatalf("nil cli: got %v", err)
	}
}

// ── SubscribePresence ────────────────────────────────────────────────────────

func TestHandler_SubscribePresence_NilRequest(t *testing.T) {
	h := NewHandler()
	_, err := h.SubscribePresence(context.Background(), nil, nil)
	if err == nil || !strings.Contains(err.Error(), "nil SubscribePresenceRequest") {
		t.Fatalf("nil req: got %v", err)
	}
}

func TestHandler_SubscribePresence_InvalidJID(t *testing.T) {
	h := NewHandler()
	_, err := h.SubscribePresence(context.Background(), nil, &pb.SubscribePresenceRequest{Jid: ""})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("empty: want ErrInvalidJID, got %v", err)
	}
	_, err = h.SubscribePresence(context.Background(), nil, &pb.SubscribePresenceRequest{Jid: "a:b:c@s.whatsapp.net"})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("malformed: want ErrInvalidJID, got %v", err)
	}
}

func TestHandler_SubscribePresence_NilClient(t *testing.T) {
	h := NewHandler()
	_, err := h.SubscribePresence(context.Background(), nil, &pb.SubscribePresenceRequest{
		Jid: "12345@s.whatsapp.net",
	})
	if err == nil || !strings.Contains(err.Error(), "nil whatsmeow.Client") {
		t.Fatalf("nil cli: got %v", err)
	}
}

// ── parseJID 边界 ────────────────────────────────────────────────────────────

func TestParseJID_Branches(t *testing.T) {
	if _, err := parseJID(""); !errors.Is(err, ErrInvalidJID) {
		t.Errorf("empty: want ErrInvalidJID, got %v", err)
	}
	const bad = "a:b:c@s.whatsapp.net"
	_, err := parseJID(bad)
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("malformed: want ErrInvalidJID, got %v", err)
	}
	if !strings.Contains(err.Error(), bad) {
		t.Errorf("error should mention input; got %q", err.Error())
	}
	jid, err := parseJID("12345@s.whatsapp.net")
	if err != nil {
		t.Fatalf("valid: unexpected err %v", err)
	}
	if jid.String() != "12345@s.whatsapp.net" {
		t.Errorf("roundtrip: got %q", jid.String())
	}
}
