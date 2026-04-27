// converter_test.go 覆盖 internal/presence/converter.go 的纯函数：
//
//   - 三对双向 enum 映射 (Presence / ChatPresence / ChatPresenceMedia)
//     的全部 case，含 UNSPECIFIED 决策（presence/state 报错；media 兜底 TEXT）
//     与未知值兜底；
//   - PresenceEventToProto / ChatPresenceEventToProto 的字段透传
//     与 nil / 零时间戳边界。
package presence

import (
	"errors"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// ── presenceTypeFromProto ────────────────────────────────────────────────────

func TestPresenceTypeFromProto(t *testing.T) {
	avail, err := presenceTypeFromProto(pb.PresenceType_PRESENCE_TYPE_AVAILABLE)
	if err != nil || avail != types.PresenceAvailable {
		t.Errorf("AVAILABLE: got (%q, %v)", avail, err)
	}
	unavail, err := presenceTypeFromProto(pb.PresenceType_PRESENCE_TYPE_UNAVAILABLE)
	if err != nil || unavail != types.PresenceUnavailable {
		t.Errorf("UNAVAILABLE: got (%q, %v)", unavail, err)
	}
	// UNSPECIFIED → ErrInvalidPresenceType（无合理默认）。
	if _, err := presenceTypeFromProto(pb.PresenceType_PRESENCE_TYPE_UNSPECIFIED); !errors.Is(err, ErrInvalidPresenceType) {
		t.Errorf("UNSPECIFIED: want ErrInvalidPresenceType, got %v", err)
	}
}

// ── chatPresenceStateFromProto ───────────────────────────────────────────────

func TestChatPresenceStateFromProto(t *testing.T) {
	composing, err := chatPresenceStateFromProto(pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING)
	if err != nil || composing != types.ChatPresenceComposing {
		t.Errorf("COMPOSING: got (%q, %v)", composing, err)
	}
	paused, err := chatPresenceStateFromProto(pb.ChatPresenceState_CHAT_PRESENCE_STATE_PAUSED)
	if err != nil || paused != types.ChatPresencePaused {
		t.Errorf("PAUSED: got (%q, %v)", paused, err)
	}
	if _, err := chatPresenceStateFromProto(pb.ChatPresenceState_CHAT_PRESENCE_STATE_UNSPECIFIED); !errors.Is(err, ErrInvalidChatPresenceState) {
		t.Errorf("UNSPECIFIED: want ErrInvalidChatPresenceState, got %v", err)
	}
}

// ── chatPresenceMediaFromProto ───────────────────────────────────────────────

func TestChatPresenceMediaFromProto(t *testing.T) {
	cases := []struct {
		in   pb.ChatPresenceMedia
		want types.ChatPresenceMedia
	}{
		{pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT, types.ChatPresenceMediaText},
		{pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_AUDIO, types.ChatPresenceMediaAudio},
		// UNSPECIFIED 兜底为 TEXT —— 旧客户端省略 media 等同于 text。
		{pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_UNSPECIFIED, types.ChatPresenceMediaText},
	}
	for _, tc := range cases {
		got := chatPresenceMediaFromProto(tc.in)
		if got != tc.want {
			t.Errorf("%v: got %q want %q", tc.in, got, tc.want)
		}
	}
}

// ── presenceTypeToProto ──────────────────────────────────────────────────────

func TestPresenceTypeToProto(t *testing.T) {
	cases := []struct {
		in   types.Presence
		want pb.PresenceType
	}{
		{types.PresenceAvailable, pb.PresenceType_PRESENCE_TYPE_AVAILABLE},
		{types.PresenceUnavailable, pb.PresenceType_PRESENCE_TYPE_UNAVAILABLE},
		{types.Presence(""), pb.PresenceType_PRESENCE_TYPE_UNSPECIFIED},
		{types.Presence("future-unknown"), pb.PresenceType_PRESENCE_TYPE_UNSPECIFIED},
	}
	for _, tc := range cases {
		got := presenceTypeToProto(tc.in)
		if got != tc.want {
			t.Errorf("%q: got %v want %v", tc.in, got, tc.want)
		}
	}
}

// ── chatPresenceStateToProto ─────────────────────────────────────────────────

func TestChatPresenceStateToProto(t *testing.T) {
	cases := []struct {
		in   types.ChatPresence
		want pb.ChatPresenceState
	}{
		{types.ChatPresenceComposing, pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING},
		{types.ChatPresencePaused, pb.ChatPresenceState_CHAT_PRESENCE_STATE_PAUSED},
		{types.ChatPresence(""), pb.ChatPresenceState_CHAT_PRESENCE_STATE_UNSPECIFIED},
		{types.ChatPresence("future-unknown"), pb.ChatPresenceState_CHAT_PRESENCE_STATE_UNSPECIFIED},
	}
	for _, tc := range cases {
		got := chatPresenceStateToProto(tc.in)
		if got != tc.want {
			t.Errorf("%q: got %v want %v", tc.in, got, tc.want)
		}
	}
}

// ── chatPresenceMediaToProto ─────────────────────────────────────────────────

func TestChatPresenceMediaToProto(t *testing.T) {
	cases := []struct {
		in   types.ChatPresenceMedia
		want pb.ChatPresenceMedia
	}{
		{types.ChatPresenceMediaAudio, pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_AUDIO},
		{types.ChatPresenceMediaText, pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT},
		// "" 与未知都兜底 TEXT —— 与上游空字符串语义一致。
		{types.ChatPresenceMedia(""), pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT},
		{types.ChatPresenceMedia("future-unknown"), pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT},
	}
	for _, tc := range cases {
		got := chatPresenceMediaToProto(tc.in)
		if got != tc.want {
			t.Errorf("%q: got %v want %v", tc.in, got, tc.want)
		}
	}
}

// ── PresenceEventToProto ─────────────────────────────────────────────────────

func TestPresenceEventToProto_Nil(t *testing.T) {
	if got := PresenceEventToProto(nil); got != nil {
		t.Fatalf("nil input: want nil, got %v", got)
	}
}

func TestPresenceEventToProto_OnlineWithLastSeen(t *testing.T) {
	from := mustParseJID(t, "12345@s.whatsapp.net")
	ts := time.Date(2024, 1, 2, 3, 4, 5, 0, time.UTC)
	ev := &events.Presence{
		From:        from,
		Unavailable: false,
		LastSeen:    ts,
	}
	got := PresenceEventToProto(ev)
	if got == nil {
		t.Fatal("got nil")
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

func TestPresenceEventToProto_OfflineNoLastSeen(t *testing.T) {
	from := mustParseJID(t, "12345@s.whatsapp.net")
	ev := &events.Presence{
		From:        from,
		Unavailable: true,
		// LastSeen 零值 —— 服务端因隐私设置不返回。
	}
	got := PresenceEventToProto(ev)
	if got == nil {
		t.Fatal("got nil")
	}
	if !got.GetUnavailable() {
		t.Error("unavailable: should be true")
	}
	if got.GetLastSeen() != nil {
		t.Errorf("last_seen: should be unset, got %v", got.GetLastSeen())
	}
}

// ── ChatPresenceEventToProto ─────────────────────────────────────────────────

func TestChatPresenceEventToProto_Nil(t *testing.T) {
	if got := ChatPresenceEventToProto(nil); got != nil {
		t.Fatalf("nil input: want nil, got %v", got)
	}
}

func TestChatPresenceEventToProto_FullFields(t *testing.T) {
	chat := mustParseJID(t, "120363025246125486@g.us")
	sender := mustParseJID(t, "12345@s.whatsapp.net")
	ev := &events.ChatPresence{
		MessageSource: types.MessageSource{Chat: chat, Sender: sender},
		State:         types.ChatPresenceComposing,
		Media:         types.ChatPresenceMediaAudio,
	}
	got := ChatPresenceEventToProto(ev)
	if got == nil {
		t.Fatal("got nil")
	}
	if got.GetChatJid() != chat.String() {
		t.Errorf("chat_jid: got %q", got.GetChatJid())
	}
	if got.GetSenderJid() != sender.String() {
		t.Errorf("sender_jid: got %q", got.GetSenderJid())
	}
	if got.GetState() != pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING {
		t.Errorf("state: got %v", got.GetState())
	}
	if got.GetMedia() != pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_AUDIO {
		t.Errorf("media: got %v", got.GetMedia())
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
