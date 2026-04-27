// handler_test.go 覆盖 internal/receipts/handler.go 中不依赖
// *whatsmeow.Client 网络往返的逻辑：MarkRead 的入参校验路径
// （nil req、空 ids、空字符串 id、非法 chat/sender JID）以及
// nil-cli 防御断言。参数校验已被刻意放在 nil-cli 检查之前，
// 因此用 nil cli 即可单测纯参数校验路径，不需要伪造 whatsmeow.Client。
//
// 涉及真实 WebSocket 往返的成功路径（cli.MarkRead 真正发出回执）
// 在 scripts/smoke_receipts_presence.py 中以真账号 smoke 验证。
package receipts

import (
	"context"
	"errors"
	"strings"
	"testing"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

func TestHandler_MarkRead_NilRequest(t *testing.T) {
	h := NewHandler()
	_, err := h.MarkRead(context.Background(), nil, nil)
	if err == nil {
		t.Fatal("nil req: expected error")
	}
	if !strings.Contains(err.Error(), "nil MarkReadRequest") {
		t.Errorf("err should mention nil request; got %q", err.Error())
	}
}

func TestHandler_MarkRead_EmptyMessageIDs(t *testing.T) {
	h := NewHandler()
	_, err := h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "12345@s.whatsapp.net",
		SenderJid:  "12345@s.whatsapp.net",
		MessageIds: nil,
	})
	if !errors.Is(err, ErrEmptyMessageIDs) {
		t.Fatalf("nil ids: want ErrEmptyMessageIDs, got %v", err)
	}

	_, err = h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "12345@s.whatsapp.net",
		SenderJid:  "12345@s.whatsapp.net",
		MessageIds: []string{},
	})
	if !errors.Is(err, ErrEmptyMessageIDs) {
		t.Fatalf("empty ids: want ErrEmptyMessageIDs, got %v", err)
	}
}

func TestHandler_MarkRead_EmptyMessageID(t *testing.T) {
	h := NewHandler()
	_, err := h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "12345@s.whatsapp.net",
		SenderJid:  "12345@s.whatsapp.net",
		MessageIds: []string{"valid-id", ""},
	})
	if !errors.Is(err, ErrEmptyMessageID) {
		t.Fatalf("blank id in list: want ErrEmptyMessageID, got %v", err)
	}
}

func TestHandler_MarkRead_InvalidJID(t *testing.T) {
	h := NewHandler()

	// 空 chat_jid。
	_, err := h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "",
		SenderJid:  "12345@s.whatsapp.net",
		MessageIds: []string{"id1"},
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("empty chat: want ErrInvalidJID, got %v", err)
	}

	// 空 sender_jid。
	_, err = h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "12345@s.whatsapp.net",
		SenderJid:  "",
		MessageIds: []string{"id1"},
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("empty sender: want ErrInvalidJID, got %v", err)
	}

	// 非法格式（多冒号）。
	_, err = h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "a:b:c@s.whatsapp.net",
		SenderJid:  "12345@s.whatsapp.net",
		MessageIds: []string{"id1"},
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("malformed chat: want ErrInvalidJID, got %v", err)
	}
}

func TestHandler_MarkRead_NilClient(t *testing.T) {
	// 所有参数合法 → nil-cli 成为唯一不合规处。
	h := NewHandler()
	_, err := h.MarkRead(context.Background(), nil, &pb.MarkReadRequest{
		ChatJid:    "120363025246125486@g.us",
		SenderJid:  "12345@s.whatsapp.net",
		MessageIds: []string{"id1"},
	})
	if err == nil {
		t.Fatal("expected error for nil client")
	}
	if !strings.Contains(err.Error(), "nil whatsmeow.Client") {
		t.Errorf("err should mention nil client; got %q", err.Error())
	}
}

// parseJID 已在 receipts 包内部使用；为避免 ErrInvalidJID 包装路径
// 漏测，单独验证空串与多冒号两条具体分支。
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
