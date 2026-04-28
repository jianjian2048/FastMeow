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

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"

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

// ── Media (Phase 4.3) ────────────────────────────────────────────────────────
//
// 媒体消息走独立 oneof slot（MediaMessageEvent，tag 22），与文本
// MessageEvent 互斥。dispatcher 在 Python 端会把 MediaMessageEvent
// 折叠回公开层 MessageEvent(media=...)，但这是 Python 端职责；
// Go 端 Bus 只负责正确翻译并占用正确的 oneof slot。

func newMessageEvent(chat, sender types.JID, msgID string, isGroup bool, ts time.Time, msg *waE2E.Message) *events.Message {
	return &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:     chat,
				Sender:   sender,
				IsFromMe: false,
				IsGroup:  isGroup,
			},
			ID:        msgID,
			Timestamp: ts,
		},
		Message: msg,
	}
}

func TestBus_Translate_MediaMessage_Image(t *testing.T) {
	b := newTestBus()
	chat := mustParseJID(t, "120363025246125486@g.us")
	sender := mustParseJID(t, "12345@s.whatsapp.net")
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := newMessageEvent(chat, sender, "MSG-IMG-1", true, ts, &waE2E.Message{
		ImageMessage: &waE2E.ImageMessage{
			Mimetype:      proto.String("image/jpeg"),
			Caption:       proto.String("hello"),
			FileLength:    proto.Uint64(2048),
			Height:        proto.Uint32(720),
			Width:         proto.Uint32(1280),
			DirectPath:    proto.String("/v/t62.x/y"),
			MediaKey:      []byte{1, 2, 3},
			FileSHA256:    []byte{4, 5, 6},
			FileEncSHA256: []byte{7, 8, 9},
			JPEGThumbnail: []byte{0xFF, 0xD8},
			ContextInfo:   &waE2E.ContextInfo{StanzaID: proto.String("QUOTED-1")},
		},
	})
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	r := resps[0]
	if r.GetAccountKey() != testAccountKey {
		t.Errorf("account_key: got %q", r.GetAccountKey())
	}
	if r.GetMessage() != nil {
		t.Error("text MessageEvent should NOT be set when media path matches")
	}
	got := r.GetMediaMessage()
	if got == nil {
		t.Fatal("MediaMessage oneof not set")
	}
	if got.GetMessageId() != "MSG-IMG-1" {
		t.Errorf("message_id: got %q", got.GetMessageId())
	}
	if got.GetChatJid() != chat.String() || got.GetSenderJid() != sender.String() {
		t.Errorf("jids: chat=%q sender=%q", got.GetChatJid(), got.GetSenderJid())
	}
	if !got.GetIsGroup() {
		t.Error("is_group: want true")
	}
	if got.GetCaption() != "hello" {
		t.Errorf("caption (top): got %q", got.GetCaption())
	}
	if got.GetReplyToMessageId() != "QUOTED-1" {
		t.Errorf("reply_to: got %q", got.GetReplyToMessageId())
	}
	m := got.GetMedia()
	if m == nil {
		t.Fatal("media not set")
	}
	if m.GetKind() != pb.MediaKind_MEDIA_KIND_IMAGE {
		t.Errorf("kind: got %v", m.GetKind())
	}
	if m.GetMimeType() != "image/jpeg" || m.GetCaption() != "hello" {
		t.Errorf("mime/caption: %q / %q", m.GetMimeType(), m.GetCaption())
	}
	if m.GetWidth() != 1280 || m.GetHeight() != 720 {
		t.Errorf("dims: %dx%d", m.GetWidth(), m.GetHeight())
	}
	if m.GetFileLength() != 2048 {
		t.Errorf("file_length: got %d", m.GetFileLength())
	}
	if m.GetDirectPath() != "/v/t62.x/y" {
		t.Errorf("direct_path: got %q", m.GetDirectPath())
	}
	if len(m.GetMediaKey()) != 3 || len(m.GetFileSha256()) != 3 || len(m.GetFileEncSha256()) != 3 {
		t.Error("download credentials not propagated")
	}
	if len(m.GetThumbnailJpeg()) != 2 {
		t.Errorf("thumbnail: got %d bytes", len(m.GetThumbnailJpeg()))
	}
}

func TestBus_Translate_MediaMessage_Audio_VoiceNote(t *testing.T) {
	b := newTestBus()
	chat := mustParseJID(t, "12345@s.whatsapp.net")
	sender := chat
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := newMessageEvent(chat, sender, "MSG-AUD-1", false, ts, &waE2E.Message{
		AudioMessage: &waE2E.AudioMessage{
			Mimetype:      proto.String("audio/ogg; codecs=opus"),
			FileLength:    proto.Uint64(8192),
			Seconds:       proto.Uint32(7),
			PTT:           proto.Bool(true),
			DirectPath:    proto.String("/v/aud/x"),
			MediaKey:      []byte{0xAA},
			FileSHA256:    []byte{0xBB},
			FileEncSHA256: []byte{0xCC},
		},
	})
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	got := resps[0].GetMediaMessage()
	if got == nil {
		t.Fatal("MediaMessage oneof not set")
	}
	if got.GetCaption() != "" {
		t.Errorf("audio should not carry caption: got %q", got.GetCaption())
	}
	m := got.GetMedia()
	if m.GetKind() != pb.MediaKind_MEDIA_KIND_AUDIO {
		t.Errorf("kind: got %v", m.GetKind())
	}
	if !m.GetVoiceNote() {
		t.Error("voice_note: want true (PTT)")
	}
	if m.GetDurationSeconds() != 7 {
		t.Errorf("duration: got %d", m.GetDurationSeconds())
	}
}

func TestBus_Translate_MediaMessage_Document_FileName(t *testing.T) {
	b := newTestBus()
	chat := mustParseJID(t, "12345@s.whatsapp.net")
	sender := chat
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := newMessageEvent(chat, sender, "MSG-DOC-1", false, ts, &waE2E.Message{
		DocumentMessage: &waE2E.DocumentMessage{
			Mimetype:      proto.String("application/pdf"),
			FileName:      proto.String("report.pdf"),
			FileLength:    proto.Uint64(123456),
			Caption:       proto.String("Q2 report"),
			DirectPath:    proto.String("/v/doc/x"),
			MediaKey:      []byte{0x01},
			FileSHA256:    []byte{0x02},
			FileEncSHA256: []byte{0x03},
		},
	})
	resps := b.translate(testAccountKey, evt)
	got := resps[0].GetMediaMessage()
	if got == nil {
		t.Fatal("MediaMessage oneof not set")
	}
	m := got.GetMedia()
	if m.GetKind() != pb.MediaKind_MEDIA_KIND_DOCUMENT {
		t.Errorf("kind: got %v", m.GetKind())
	}
	if m.GetFileName() != "report.pdf" {
		t.Errorf("file_name: got %q", m.GetFileName())
	}
	if got.GetCaption() != "Q2 report" || m.GetCaption() != "Q2 report" {
		t.Errorf("caption (top/media): %q / %q", got.GetCaption(), m.GetCaption())
	}
}

func TestBus_Translate_MediaMessage_Sticker_NoCaption(t *testing.T) {
	b := newTestBus()
	chat := mustParseJID(t, "12345@s.whatsapp.net")
	sender := chat
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := newMessageEvent(chat, sender, "MSG-STK-1", false, ts, &waE2E.Message{
		StickerMessage: &waE2E.StickerMessage{
			Mimetype:      proto.String("image/webp"),
			FileLength:    proto.Uint64(512),
			Width:         proto.Uint32(512),
			Height:        proto.Uint32(512),
			IsAnimated:    proto.Bool(true),
			DirectPath:    proto.String("/v/stk/x"),
			MediaKey:      []byte{0x10},
			FileSHA256:    []byte{0x20},
			FileEncSHA256: []byte{0x30},
		},
	})
	resps := b.translate(testAccountKey, evt)
	got := resps[0].GetMediaMessage()
	if got == nil {
		t.Fatal("MediaMessage oneof not set")
	}
	if got.GetCaption() != "" {
		t.Errorf("sticker carries no caption: got %q", got.GetCaption())
	}
	m := got.GetMedia()
	if m.GetKind() != pb.MediaKind_MEDIA_KIND_STICKER {
		t.Errorf("kind: got %v", m.GetKind())
	}
	if !m.GetAnimated() {
		t.Error("animated: want true")
	}
}

func TestBus_Translate_MediaMessage_PrecedenceOverText(t *testing.T) {
	// 同时携带 ImageMessage 与 ExtendedTextMessage 时，媒体路径应优先；
	// 文本 MessageEvent 不应被发出（互斥保证 dispatcher 不会双发）。
	b := newTestBus()
	chat := mustParseJID(t, "12345@s.whatsapp.net")
	sender := chat
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := newMessageEvent(chat, sender, "MSG-MIX-1", false, ts, &waE2E.Message{
		ImageMessage: &waE2E.ImageMessage{
			Mimetype:      proto.String("image/jpeg"),
			DirectPath:    proto.String("/v/img/x"),
			MediaKey:      []byte{0x01},
			FileSHA256:    []byte{0x02},
			FileEncSHA256: []byte{0x03},
		},
		ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String("should be ignored"),
		},
	})
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	if resps[0].GetMediaMessage() == nil {
		t.Error("expected media path to win")
	}
	if resps[0].GetMessage() != nil {
		t.Error("text MessageEvent must NOT be emitted alongside media")
	}
}

func TestBus_Translate_MediaMessage_NoMediaFallsBackToText(t *testing.T) {
	// 纯文本消息：媒体路径返 nil，fallback 到文本 MessageEvent。
	b := newTestBus()
	chat := mustParseJID(t, "12345@s.whatsapp.net")
	sender := chat
	ts := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)

	evt := newMessageEvent(chat, sender, "MSG-TXT-1", false, ts, &waE2E.Message{
		Conversation: proto.String("plain text"),
	})
	resps := b.translate(testAccountKey, evt)
	if len(resps) != 1 {
		t.Fatalf("want 1 wire event, got %d", len(resps))
	}
	if resps[0].GetMediaMessage() != nil {
		t.Error("media path should not match for plain text")
	}
	me := resps[0].GetMessage()
	if me == nil {
		t.Fatal("text MessageEvent must be emitted for Conversation")
	}
	if me.GetText() != "plain text" {
		t.Errorf("text: got %q", me.GetText())
	}
}
