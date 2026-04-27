// converter_test.go 覆盖 internal/receipts/converter.go 的纯函数：
//
//   - receiptTypeFromProto / receiptTypeToProto 的双向映射，含 self
//     变体折叠与未知值兜底；
//   - ReceiptEventToProto 的字段透传与 nil / 空 IDs / 零时间戳边界。
//
// 这些函数无外部依赖，全部以 table-driven 方式覆盖。
package receipts

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// ── receiptTypeFromProto ─────────────────────────────────────────────────────

func TestReceiptTypeFromProto(t *testing.T) {
	cases := []struct {
		in   pb.ReceiptType
		want types.ReceiptType
	}{
		{pb.ReceiptType_RECEIPT_TYPE_DELIVERED, types.ReceiptTypeDelivered},
		{pb.ReceiptType_RECEIPT_TYPE_READ, types.ReceiptTypeRead},
		{pb.ReceiptType_RECEIPT_TYPE_PLAYED, types.ReceiptTypePlayed},
		{pb.ReceiptType_RECEIPT_TYPE_SERVER, types.ReceiptTypeServerError},
		// UNSPECIFIED 兜底为 READ —— MarkRead 的「显然意图」。
		{pb.ReceiptType_RECEIPT_TYPE_UNSPECIFIED, types.ReceiptTypeRead},
	}
	for _, tc := range cases {
		got := receiptTypeFromProto(tc.in)
		if got != tc.want {
			t.Errorf("receiptTypeFromProto(%v): got %q want %q", tc.in, got, tc.want)
		}
	}
}

// ── receiptTypeToProto ───────────────────────────────────────────────────────

func TestReceiptTypeToProto(t *testing.T) {
	cases := []struct {
		in   types.ReceiptType
		want pb.ReceiptType
	}{
		{types.ReceiptTypeDelivered, pb.ReceiptType_RECEIPT_TYPE_DELIVERED},
		{types.ReceiptTypeRead, pb.ReceiptType_RECEIPT_TYPE_READ},
		// READ-self 折叠到 READ —— user code 不应区分。
		{types.ReceiptTypeReadSelf, pb.ReceiptType_RECEIPT_TYPE_READ},
		{types.ReceiptTypePlayed, pb.ReceiptType_RECEIPT_TYPE_PLAYED},
		{types.ReceiptTypePlayedSelf, pb.ReceiptType_RECEIPT_TYPE_PLAYED},
		// 兜底：未知值 / retry / sender / inactive 等 → SERVER。
		{types.ReceiptTypeServerError, pb.ReceiptType_RECEIPT_TYPE_SERVER},
		{types.ReceiptType("retry"), pb.ReceiptType_RECEIPT_TYPE_SERVER},
		{types.ReceiptType("sender"), pb.ReceiptType_RECEIPT_TYPE_SERVER},
		{types.ReceiptType("inactive"), pb.ReceiptType_RECEIPT_TYPE_SERVER},
		{types.ReceiptType("future-unknown"), pb.ReceiptType_RECEIPT_TYPE_SERVER},
	}
	for _, tc := range cases {
		got := receiptTypeToProto(tc.in)
		if got != tc.want {
			t.Errorf("receiptTypeToProto(%q): got %v want %v", tc.in, got, tc.want)
		}
	}
}

// ── ReceiptEventToProto ──────────────────────────────────────────────────────

func TestReceiptEventToProto_Nil(t *testing.T) {
	if got := ReceiptEventToProto(nil); got != nil {
		t.Fatalf("nil input: want nil, got %v", got)
	}
}

func TestReceiptEventToProto_FullFields(t *testing.T) {
	chat := mustParseJID(t, "120363025246125486@g.us")
	sender := mustParseJID(t, "12345@s.whatsapp.net")
	ts := time.Date(2024, 1, 2, 3, 4, 5, 0, time.UTC)

	ev := &events.Receipt{
		MessageSource: types.MessageSource{
			Chat:   chat,
			Sender: sender,
		},
		MessageIDs: []types.MessageID{"id1", "id2", "id3"},
		Timestamp:  ts,
		Type:       types.ReceiptTypeRead,
	}
	got := ReceiptEventToProto(ev)
	if got == nil {
		t.Fatal("got nil for valid event")
	}
	if got.GetChatJid() != chat.String() {
		t.Errorf("chat_jid: got %q want %q", got.GetChatJid(), chat.String())
	}
	if got.GetSenderJid() != sender.String() {
		t.Errorf("sender_jid: got %q want %q", got.GetSenderJid(), sender.String())
	}
	if got.GetReceiptType() != pb.ReceiptType_RECEIPT_TYPE_READ {
		t.Errorf("receipt_type: got %v want READ", got.GetReceiptType())
	}
	gotIDs := got.GetMessageIds()
	if len(gotIDs) != 3 || gotIDs[0] != "id1" || gotIDs[1] != "id2" || gotIDs[2] != "id3" {
		t.Errorf("message_ids: got %v", gotIDs)
	}
	if got.GetTimestamp() == nil {
		t.Error("timestamp: got nil for non-zero source")
	} else if !got.GetTimestamp().AsTime().Equal(ts) {
		t.Errorf("timestamp: got %v want %v", got.GetTimestamp().AsTime(), ts)
	}
}

func TestReceiptEventToProto_EmptyIDsAndZeroTime(t *testing.T) {
	chat := mustParseJID(t, "12345@s.whatsapp.net")
	ev := &events.Receipt{
		MessageSource: types.MessageSource{Chat: chat, Sender: chat},
		// MessageIDs 为 nil；Timestamp 零值。
		Type: types.ReceiptTypeDelivered,
	}
	got := ReceiptEventToProto(ev)
	if got == nil {
		t.Fatal("got nil")
	}
	if len(got.GetMessageIds()) != 0 {
		t.Errorf("message_ids should be empty, got %v", got.GetMessageIds())
	}
	if got.GetTimestamp() != nil {
		t.Errorf("timestamp should be unset for zero time, got %v", got.GetTimestamp())
	}
	if got.GetReceiptType() != pb.ReceiptType_RECEIPT_TYPE_DELIVERED {
		t.Errorf("receipt_type: got %v want DELIVERED", got.GetReceiptType())
	}
}

// ── helpers ──────────────────────────────────────────────────────────────────

func mustParseJID(t *testing.T, raw string) types.JID {
	t.Helper()
	jid, err := types.ParseJID(raw)
	if err != nil {
		t.Fatalf("ParseJID(%q): %v", raw, err)
	}
	return jid
}
