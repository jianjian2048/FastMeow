// handler_test.go 覆盖 internal/groups/handler.go 里不依赖 *whatsmeow.Client
// 网络往返的逻辑：
//
//   - parseJID / normalizeInviteCode / participantActionFromProto 三个纯函数
//     的全部分支；
//   - 9 个 RPC 方法的入参校验路径（nil client、空 JID、空邀请码、空名称、
//     空成员列表、未知 action）—— 这些路径在 client 真正被调用之前就返回
//     sentinel error，因此不需要构造可工作的 whatsmeow client。
//
// 涉及真正网络往返的成功路径（例如 GetJoinedGroups 走 WebSocket 回到
// WhatsApp 服务器）在 scripts/smoke_groups.py 里以真账号 smoke 验证；
// 在 Go 单测层模拟整个 whatsmeow 客户端会让测试比被测代码本身还复杂，
// ROI 很差。
package groups

import (
	"context"
	"errors"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// ── parseJID ─────────────────────────────────────────────────────────────────

func TestParseJID_Empty(t *testing.T) {
	_, err := parseJID("")
	if !errors.Is(err, ErrInvalidJID) {
		t.Fatalf("empty: want ErrInvalidJID, got %v", err)
	}
}

func TestParseJID_Malformed(t *testing.T) {
	// whatsmeow.ParseJID 极其宽容（无 @ 时回退为 default user JID），
	// 几乎不返回 error。已知会触发的是「冒号数量异常」分支：
	//   "a:b:c@s.whatsapp.net" → "unexpected number of colons in JID"
	const bad = "a:b:c@s.whatsapp.net"
	_, err := parseJID(bad)
	if !errors.Is(err, ErrInvalidJID) {
		t.Fatalf("malformed: want ErrInvalidJID, got %v", err)
	}
	// 错误消息应携带原值，便于服务端日志排查。
	if !strings.Contains(err.Error(), bad) {
		t.Errorf("error should mention input; got %q", err.Error())
	}
}

func TestParseJID_Valid(t *testing.T) {
	cases := []string{
		"12345@s.whatsapp.net",
		"120363025246125486@g.us",
	}
	for _, raw := range cases {
		jid, err := parseJID(raw)
		if err != nil {
			t.Errorf("parseJID(%q): %v", raw, err)
			continue
		}
		if jid.String() != raw {
			t.Errorf("roundtrip: got %q want %q", jid.String(), raw)
		}
	}
}

// ── normalizeInviteCode ──────────────────────────────────────────────────────

func TestNormalizeInviteCode(t *testing.T) {
	cases := []struct {
		in      string
		want    string
		wantErr error
	}{
		{"", "", ErrEmptyInviteCode},
		{"   ", "", ErrEmptyInviteCode},
		{"https://chat.whatsapp.com/", "", ErrEmptyInviteCode},
		{"ABCDEF1234567", "ABCDEF1234567", nil},
		{"https://chat.whatsapp.com/ABCDEF1234567", "ABCDEF1234567", nil},
		// 前后空白：TrimSpace 应消除。
		{"  ABCDEF1234567  ", "ABCDEF1234567", nil},
	}
	for _, tc := range cases {
		got, err := normalizeInviteCode(tc.in)
		if tc.wantErr != nil {
			if !errors.Is(err, tc.wantErr) {
				t.Errorf("normalizeInviteCode(%q): want err %v, got %v", tc.in, tc.wantErr, err)
			}
			continue
		}
		if err != nil {
			t.Errorf("normalizeInviteCode(%q): unexpected err %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("normalizeInviteCode(%q): got %q want %q", tc.in, got, tc.want)
		}
	}
}

// ── participantActionFromProto ───────────────────────────────────────────────

func TestParticipantActionFromProto_AllKnown(t *testing.T) {
	cases := []struct {
		in   pb.GroupParticipantUpdateEvent_GroupParticipantAction
		want whatsmeow.ParticipantChange
	}{
		{pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_ADD, whatsmeow.ParticipantChangeAdd},
		{pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_REMOVE, whatsmeow.ParticipantChangeRemove},
		{pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_PROMOTE, whatsmeow.ParticipantChangePromote},
		{pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_DEMOTE, whatsmeow.ParticipantChangeDemote},
	}
	for _, tc := range cases {
		got, err := participantActionFromProto(tc.in)
		if err != nil {
			t.Errorf("action %v: unexpected err %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("action %v: got %v want %v", tc.in, got, tc.want)
		}
	}
}

func TestParticipantActionFromProto_Unspecified(t *testing.T) {
	_, err := participantActionFromProto(pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_UNSPECIFIED)
	if !errors.Is(err, ErrInvalidParticipantAction) {
		t.Fatalf("UNSPECIFIED should map to ErrInvalidParticipantAction; got %v", err)
	}
}

// ── handler RPC 入参校验路径 ─────────────────────────────────────────────────
//
// 所有 handler 方法在 cli == nil 时直接返回 errors.New("groups: nil ...")，
// 这是 sidecar 内部不应发生的状态，但留作防御断言。
// 这些路径不依赖 whatsmeow，可以无 client 地直接驱动。

// 所有 handler 方法在 cli == nil 时应返回 "groups: nil whatsmeow.Client"。
// 由于参数校验已被故意排在 nil-check 之前，触发该分支必须传入**有效**
// 的所有参数，让 nil cli 成为唯一不合规处。
func TestHandler_NilClient(t *testing.T) {
	h := NewHandler()
	ctx := context.Background()

	checks := []struct {
		name string
		call func() error
	}{
		{"ListJoinedGroups", func() error {
			_, err := h.ListJoinedGroups(ctx, nil)
			return err
		}},
		{"GetGroupInfo", func() error {
			_, err := h.GetGroupInfo(ctx, nil, "120363025246125486@g.us")
			return err
		}},
		{"PreviewGroupInvite", func() error {
			_, err := h.PreviewGroupInvite(ctx, nil, "ABC123")
			return err
		}},
		{"JoinGroupViaInvite", func() error {
			_, err := h.JoinGroupViaInvite(ctx, nil, "ABC123")
			return err
		}},
		{"LeaveGroup", func() error {
			_, err := h.LeaveGroup(ctx, nil, "120363025246125486@g.us")
			return err
		}},
		{"CreateGroup", func() error {
			_, err := h.CreateGroup(ctx, nil, "name", []string{"12345@s.whatsapp.net"})
			return err
		}},
		{"UpdateGroupSettings", func() error {
			_, err := h.UpdateGroupSettings(ctx, nil, &pb.UpdateGroupSettingsRequest{
				GroupJid: "120363025246125486@g.us",
			})
			return err
		}},
		{"UpdateGroupParticipants", func() error {
			_, err := h.UpdateGroupParticipants(ctx, nil, &pb.UpdateGroupParticipantsRequest{
				GroupJid:        "120363025246125486@g.us",
				Action:          pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_ADD,
				ParticipantJids: []string{"12345@s.whatsapp.net"},
			})
			return err
		}},
		{"GetGroupInviteLink", func() error {
			_, err := h.GetGroupInviteLink(ctx, nil, "120363025246125486@g.us", false)
			return err
		}},
	}
	for _, tc := range checks {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.call()
			if err == nil {
				t.Fatal("expected error for nil client; got nil")
			}
			if !strings.Contains(err.Error(), "nil whatsmeow.Client") {
				t.Errorf("error should mention nil client; got %q", err.Error())
			}
		})
	}
}

// CreateGroup 的入参校验：空 name 与空 participants 两条独立路径。
//
// 注：参数校验已被刻意放在 nil-client 检查之前，因此用 nil cli 即可
// 单测纯参数校验路径，不需要伪造 whatsmeow.Client。
func TestHandler_CreateGroup_ParamValidation(t *testing.T) {
	h := NewHandler()
	ctx := context.Background()

	if _, err := h.CreateGroup(ctx, nil, "", []string{"12345@s.whatsapp.net"}); !errors.Is(err, ErrEmptyGroupName) {
		t.Errorf("empty name: want ErrEmptyGroupName, got %v", err)
	}
	if _, err := h.CreateGroup(ctx, nil, "name", nil); !errors.Is(err, ErrNoParticipants) {
		t.Errorf("nil participants: want ErrNoParticipants, got %v", err)
	}
	if _, err := h.CreateGroup(ctx, nil, "name", []string{}); !errors.Is(err, ErrNoParticipants) {
		t.Errorf("empty participants: want ErrNoParticipants, got %v", err)
	}
	// 非法 JID 走 parseJID -> ErrInvalidJID。
	if _, err := h.CreateGroup(ctx, nil, "name", []string{"a:b:c@s.whatsapp.net"}); !errors.Is(err, ErrInvalidJID) {
		t.Errorf("malformed participant JID: want ErrInvalidJID, got %v", err)
	}
}

// UpdateGroupParticipants 的入参校验：UNSPECIFIED action / 空 participants /
// 非法 group JID / 非法 participant JID。参数校验全部位于 nil-client 检查
// 之前，所以这里传 nil cli 是安全的。
func TestHandler_UpdateGroupParticipants_ParamValidation(t *testing.T) {
	h := NewHandler()
	ctx := context.Background()

	// 非法 group JID
	_, err := h.UpdateGroupParticipants(ctx, nil, &pb.UpdateGroupParticipantsRequest{
		GroupJid: "a:b:c@s.whatsapp.net",
		Action:   pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_ADD,
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("bad group jid: want ErrInvalidJID, got %v", err)
	}

	// UNSPECIFIED action
	_, err = h.UpdateGroupParticipants(ctx, nil, &pb.UpdateGroupParticipantsRequest{
		GroupJid: "120363025246125486@g.us",
		Action:   pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_UNSPECIFIED,
	})
	if !errors.Is(err, ErrInvalidParticipantAction) {
		t.Errorf("UNSPECIFIED action: want ErrInvalidParticipantAction, got %v", err)
	}

	// 空 participants
	_, err = h.UpdateGroupParticipants(ctx, nil, &pb.UpdateGroupParticipantsRequest{
		GroupJid:        "120363025246125486@g.us",
		Action:          pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_ADD,
		ParticipantJids: nil,
	})
	if !errors.Is(err, ErrNoParticipants) {
		t.Errorf("nil participants: want ErrNoParticipants, got %v", err)
	}

	// 非法 participant JID
	_, err = h.UpdateGroupParticipants(ctx, nil, &pb.UpdateGroupParticipantsRequest{
		GroupJid:        "120363025246125486@g.us",
		Action:          pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_ADD,
		ParticipantJids: []string{"a:b:c@s.whatsapp.net"},
	})
	if !errors.Is(err, ErrInvalidJID) {
		t.Errorf("bad participant jid: want ErrInvalidJID, got %v", err)
	}
}

// PreviewGroupInvite / JoinGroupViaInvite 共享的 normalizeInviteCode 边界。
func TestHandler_InviteCode_EmptyValidation(t *testing.T) {
	h := NewHandler()
	ctx := context.Background()

	if _, err := h.PreviewGroupInvite(ctx, nil, ""); !errors.Is(err, ErrEmptyInviteCode) {
		t.Errorf("PreviewGroupInvite empty: want ErrEmptyInviteCode, got %v", err)
	}
	if _, err := h.JoinGroupViaInvite(ctx, nil, ""); !errors.Is(err, ErrEmptyInviteCode) {
		t.Errorf("JoinGroupViaInvite empty: want ErrEmptyInviteCode, got %v", err)
	}
}

// GetGroupInfo / LeaveGroup / GetGroupInviteLink 共享的 group JID 校验。
func TestHandler_GroupJID_EmptyValidation(t *testing.T) {
	h := NewHandler()
	ctx := context.Background()

	if _, err := h.GetGroupInfo(ctx, nil, ""); !errors.Is(err, ErrInvalidJID) {
		t.Errorf("GetGroupInfo empty: want ErrInvalidJID, got %v", err)
	}
	if _, err := h.LeaveGroup(ctx, nil, ""); !errors.Is(err, ErrInvalidJID) {
		t.Errorf("LeaveGroup empty: want ErrInvalidJID, got %v", err)
	}
	if _, err := h.GetGroupInviteLink(ctx, nil, "", false); !errors.Is(err, ErrInvalidJID) {
		t.Errorf("GetGroupInviteLink empty: want ErrInvalidJID, got %v", err)
	}
}
