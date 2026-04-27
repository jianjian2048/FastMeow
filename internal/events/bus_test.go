// bus_test.go 覆盖 Bus.translate 中 Phase 4.2 新增的 3 个软状态分支：
// Receipt / Presence / ChatPresence。其余分支（Connected/Message/GroupInfo
// 等）已在更早的集成路径里被覆盖，本文件聚焦 4.2 增量。
//
// 为什么直接调用 translate：Bus.Sink 走 emit → out chan，需要
// Subscribe 配合，且涉及 backpressure 路径——而 translate 是
// 纯函数（输入 evt 输出 []*pb.StreamEventsResponse），单测它就
// 足以证明事件类型识别 + 字段透传正确。Bus 的 fan-out / 背压
// 行为由其它测试覆盖。
package events

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

const testAccountKey = "acct-test"

func newTestBus() *Bus {
	// bufferSize 1 即可——本文件的测试不消费 out chan。
	return NewBus("sidecar-test", 1, nil)
}

func mustParseJID(t *testing.T, raw string) types.JID {
	t.Helper()
	jid, err := types.ParseJID(raw)
	if err != nil {
		t.Fatalf("ParseJID(%q): %v", raw, err)
	}
	return jid
}

// ── Receipt ──────────────────────────────────────────────────────────────────

func TestBus_Translate_Receipt(t *testing.T) {
	b := newTestBus()
	chat := mustParseJID(t, "120363025246125486@g.us")
	sender := mustParseJID(t, "12345@s.whatsapp.net")
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := &events.Receipt{
		MessageSource: types.MessageSource{Chat: chat, Sender: sender},
		MessageIDs:    []types.MessageID{"id-a", "id-b"},
		Timestamp:     ts,
		Type:          types.ReceiptTypeRead,
	}
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	r := resps[0]
	if r.GetAccountKey() != testAccountKey {
		t.Errorf("account_key: got %q", r.GetAccountKey())
	}
	got := r.GetReceipt()
	if got == nil {
		t.Fatal("Receipt oneof not set")
	}
	if got.GetChatJid() != chat.String() || got.GetSenderJid() != sender.String() {
		t.Errorf("jids: chat=%q sender=%q", got.GetChatJid(), got.GetSenderJid())
	}
	if got.GetReceiptType() != pb.ReceiptType_RECEIPT_TYPE_READ {
		t.Errorf("type: got %v", got.GetReceiptType())
	}
	if ids := got.GetMessageIds(); len(ids) != 2 || ids[0] != "id-a" || ids[1] != "id-b" {
		t.Errorf("message_ids: got %v", ids)
	}
	if got.GetTimestamp() == nil || !got.GetTimestamp().AsTime().Equal(ts) {
		t.Errorf("timestamp: got %v", got.GetTimestamp())
	}
}

// ── Presence ─────────────────────────────────────────────────────────────────

func TestBus_Translate_Presence(t *testing.T) {
	b := newTestBus()
	from := mustParseJID(t, "12345@s.whatsapp.net")
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := &events.Presence{
		From:        from,
		Unavailable: false,
		LastSeen:    ts,
	}
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	r := resps[0]
	if r.GetAccountKey() != testAccountKey {
		t.Errorf("account_key: got %q", r.GetAccountKey())
	}
	got := r.GetPresence()
	if got == nil {
		t.Fatal("Presence oneof not set")
	}
	if got.GetFromJid() != from.String() {
		t.Errorf("from_jid: got %q", got.GetFromJid())
	}
	if got.GetUnavailable() {
		t.Error("unavailable: should be false")
	}
	if got.GetLastSeen() == nil || !got.GetLastSeen().AsTime().Equal(ts) {
		t.Errorf("last_seen: got %v", got.GetLastSeen())
	}
}

// ── ChatPresence ─────────────────────────────────────────────────────────────

func TestBus_Translate_ChatPresence(t *testing.T) {
	b := newTestBus()
	chat := mustParseJID(t, "120363025246125486@g.us")
	sender := mustParseJID(t, "12345@s.whatsapp.net")

	evt := &events.ChatPresence{
		MessageSource: types.MessageSource{Chat: chat, Sender: sender},
		State:         types.ChatPresenceComposing,
		Media:         types.ChatPresenceMediaAudio,
	}
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	r := resps[0]
	if r.GetAccountKey() != testAccountKey {
		t.Errorf("account_key: got %q", r.GetAccountKey())
	}
	got := r.GetChatPresence()
	if got == nil {
		t.Fatal("ChatPresence oneof not set")
	}
	if got.GetChatJid() != chat.String() || got.GetSenderJid() != sender.String() {
		t.Errorf("jids: chat=%q sender=%q", got.GetChatJid(), got.GetSenderJid())
	}
	if got.GetState() != pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING {
		t.Errorf("state: got %v", got.GetState())
	}
	if got.GetMedia() != pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_AUDIO {
		t.Errorf("media: got %v", got.GetMedia())
	}
}
