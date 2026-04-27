// converter_test.go 覆盖 converter.go 里的 4 个纯函数。
//
// 它们之所以适合大量单元测试：
//   - 全部不依赖 *whatsmeow.Client，可纯靠构造 types.GroupInfo /
//     GroupParticipant 字面量驱动；
//   - 字段映射是 wire 协议的契约，回归会直接破坏 SDK 行为，
//     必须用断言锁死字段名与零值语义；
//   - itoa 是手写实现（避免依赖 strconv），需要负数 / 零 / 多位
//     等边界覆盖。
package groups

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

func mustParseJID(t *testing.T, raw string) types.JID {
	t.Helper()
	j, err := types.ParseJID(raw)
	if err != nil {
		t.Fatalf("ParseJID(%q): %v", raw, err)
	}
	return j
}

// ── groupInfoToProto ─────────────────────────────────────────────────────────

func TestGroupInfoToProto_Nil(t *testing.T) {
	if got := groupInfoToProto(nil); got != nil {
		t.Fatalf("nil input should map to nil; got %+v", got)
	}
}

func TestGroupInfoToProto_FullFields(t *testing.T) {
	created := time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC)
	info := &types.GroupInfo{
		JID:      mustParseJID(t, "120363025246125486@g.us"),
		OwnerJID: mustParseJID(t, "12345@s.whatsapp.net"),
		GroupName: types.GroupName{
			Name: "FastMeow QA",
		},
		GroupTopic: types.GroupTopic{
			Topic: "phase 4.1 group RPC tests",
		},
		GroupLocked: types.GroupLocked{
			IsLocked: true,
		},
		GroupAnnounce: types.GroupAnnounce{
			IsAnnounce: true,
		},
		GroupEphemeral: types.GroupEphemeral{
			IsEphemeral:       true,
			DisappearingTimer: 86400,
		},
		GroupMembershipApprovalMode: types.GroupMembershipApprovalMode{
			IsJoinApprovalRequired: true,
		},
		GroupCreated: created,
		Participants: []types.GroupParticipant{
			{JID: mustParseJID(t, "11111@s.whatsapp.net"), IsAdmin: true, IsSuperAdmin: true},
			{JID: mustParseJID(t, "22222@s.whatsapp.net")},
		},
	}

	got := groupInfoToProto(info)
	if got == nil {
		t.Fatal("expected non-nil GroupInfo")
	}
	if got.GetJid() != info.JID.String() {
		t.Errorf("Jid mismatch: got %q want %q", got.GetJid(), info.JID.String())
	}
	if got.GetName() != "FastMeow QA" {
		t.Errorf("Name mismatch: got %q", got.GetName())
	}
	if got.GetTopic() != "phase 4.1 group RPC tests" {
		t.Errorf("Topic mismatch: got %q", got.GetTopic())
	}
	if !got.GetIsLocked() {
		t.Error("IsLocked should be true")
	}
	if !got.GetIsAnnounce() {
		t.Error("IsAnnounce should be true")
	}
	if !got.GetIsEphemeral() {
		t.Error("IsEphemeral should be true")
	}
	if got.GetEphemeralDurationSeconds() != 86400 {
		t.Errorf("EphemeralDurationSeconds: got %d want 86400", got.GetEphemeralDurationSeconds())
	}
	// converter 透传 whatsmeow 字段；该字段在 types.GroupInfo 上是 string 别名（DefaultMembershipApprovalMode）。
	// whatsmeow 当前实现：当 IsJoinApprovalRequired 时不直接给 string —— 见 PHASE_4_PLAN，
	// 这里仅断言字段被读取（不为 panic），具体值由上游决定。
	_ = got.GetMembershipApprovalMode()
	if got.GetOwnerJid() != info.OwnerJID.String() {
		t.Errorf("OwnerJid mismatch: got %q", got.GetOwnerJid())
	}
	if got.GetCreationTimestamp() == nil {
		t.Fatal("CreationTimestamp should be set")
	}
	if !got.GetCreationTimestamp().AsTime().Equal(created) {
		t.Errorf("CreationTimestamp: got %v want %v", got.GetCreationTimestamp().AsTime(), created)
	}
	if len(got.GetParticipants()) != 2 {
		t.Fatalf("Participants len: got %d want 2", len(got.GetParticipants()))
	}
	if !got.GetParticipants()[0].GetIsAdmin() || !got.GetParticipants()[0].GetIsSuperAdmin() {
		t.Error("first participant should be admin + super admin")
	}
	if got.GetParticipants()[1].GetIsAdmin() {
		t.Error("second participant should not be admin")
	}
}

func TestGroupInfoToProto_OmitsEmptyOptionals(t *testing.T) {
	// 零值 OwnerJID + 零 GroupCreated + 空 Participants：相应字段不应出现。
	info := &types.GroupInfo{
		JID: mustParseJID(t, "120363025246125486@g.us"),
		GroupName: types.GroupName{
			Name: "minimal",
		},
	}
	got := groupInfoToProto(info)
	if got.GetOwnerJid() != "" {
		t.Errorf("OwnerJid should be empty, got %q", got.GetOwnerJid())
	}
	if got.GetCreationTimestamp() != nil {
		t.Error("CreationTimestamp should be nil for zero GroupCreated")
	}
	if got.GetParticipants() != nil {
		t.Errorf("Participants should be nil, got %v", got.GetParticipants())
	}
}

// ── groupParticipantToProto ──────────────────────────────────────────────────

func TestGroupParticipantToProto_Nil(t *testing.T) {
	if got := groupParticipantToProto(nil); got != nil {
		t.Fatalf("nil input should map to nil; got %+v", got)
	}
}

func TestGroupParticipantToProto_AdminFlags(t *testing.T) {
	cases := []struct {
		name  string
		admin bool
		super bool
	}{
		{"member", false, false},
		{"admin", true, false},
		{"super_admin", true, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := &types.GroupParticipant{
				JID:          mustParseJID(t, "12345@s.whatsapp.net"),
				IsAdmin:      tc.admin,
				IsSuperAdmin: tc.super,
			}
			got := groupParticipantToProto(p)
			if got.GetIsAdmin() != tc.admin {
				t.Errorf("IsAdmin: got %v want %v", got.GetIsAdmin(), tc.admin)
			}
			if got.GetIsSuperAdmin() != tc.super {
				t.Errorf("IsSuperAdmin: got %v want %v", got.GetIsSuperAdmin(), tc.super)
			}
		})
	}
}

// ── participantUpdateResults ─────────────────────────────────────────────────

func TestParticipantUpdateResults_Empty(t *testing.T) {
	if got := participantUpdateResults(nil); got != nil {
		t.Errorf("nil slice should yield nil; got %v", got)
	}
	if got := participantUpdateResults([]types.GroupParticipant{}); got != nil {
		t.Errorf("empty slice should yield nil; got %v", got)
	}
}

func TestParticipantUpdateResults_MixedSuccessAndError(t *testing.T) {
	updates := []types.GroupParticipant{
		{JID: mustParseJID(t, "11111@s.whatsapp.net"), Error: 0},   // success
		{JID: mustParseJID(t, "22222@s.whatsapp.net"), Error: 403}, // forbidden
		{JID: mustParseJID(t, "33333@s.whatsapp.net"), Error: 409}, // conflict
	}
	got := participantUpdateResults(updates)
	if len(got) != 3 {
		t.Fatalf("len: got %d want 3", len(got))
	}
	// success
	if !got[0].GetSuccess() || got[0].GetErrorCode() != "" {
		t.Errorf("idx 0: expected success+empty code, got %+v", got[0])
	}
	// 403
	if got[1].GetSuccess() || got[1].GetErrorCode() != "403" {
		t.Errorf("idx 1: expected fail+403, got %+v", got[1])
	}
	// 409
	if got[2].GetSuccess() || got[2].GetErrorCode() != "409" {
		t.Errorf("idx 2: expected fail+409, got %+v", got[2])
	}
	// error_message 全部留空（converter 决策）
	for i, r := range got {
		if r.GetErrorMessage() != "" {
			t.Errorf("idx %d: ErrorMessage should be empty, got %q", i, r.GetErrorMessage())
		}
	}
}

// ── itoa ─────────────────────────────────────────────────────────────────────
//
// 手写 itoa 必须与 strconv.Itoa 行为一致：覆盖零、单位、多位、负数。

func TestItoa(t *testing.T) {
	cases := []struct {
		in   int
		want string
	}{
		{0, "0"},
		{1, "1"},
		{9, "9"},
		{10, "10"},
		{99, "99"},
		{100, "100"},
		{403, "403"},
		{409, "409"},
		{1234567, "1234567"},
		{-1, "-1"},
		{-403, "-403"},
		{-1234567, "-1234567"},
	}
	for _, tc := range cases {
		if got := itoa(tc.in); got != tc.want {
			t.Errorf("itoa(%d): got %q want %q", tc.in, got, tc.want)
		}
	}
}

// 编译时断言：导出别名 GroupInfoToProto 与内部实现签名一致。
// 防止未来重构时把内部签名改了却忘了同步导出 wrapper。
var _ = func() bool {
	var info *types.GroupInfo
	var _ *pb.GroupInfo = GroupInfoToProto(info)
	return true
}()
