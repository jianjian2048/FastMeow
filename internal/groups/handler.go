// handler.go 暴露 9 个群组 RPC 的领域逻辑。它对 wire 协议无感
// （除了返回 *pb.XxxResponse 这一约定外），把所有错误以 sentinel
// 值的形式返回；gRPC 服务端层（cmd/fastmeow-sidecar/main.go）
// 负责将其映射成 codes.InvalidArgument / NotFound / FailedPrecondition 等。
package groups

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// 暴露给 gRPC 处理程序的 sentinel 错误。
//
// 校验类（→ INVALID_ARGUMENT）：
//   - ErrInvalidJID                 group_jid / participant_jid 解析失败
//   - ErrEmptyInviteCode            邀请链接为空
//   - ErrEmptyGroupName             创建群时 name 为空
//   - ErrNoParticipants             创建群 / 改成员时目标列表为空
//   - ErrInvalidParticipantAction   action 为 UNSPECIFIED 或未知值
//
// 业务类（来自 whatsmeow，原样转发；服务端按下方表映射 status code）：
//   - whatsmeow.ErrGroupNotFound              → NOT_FOUND
//   - whatsmeow.ErrNotInGroup                 → PERMISSION_DENIED
//   - whatsmeow.ErrInviteLinkInvalid          → INVALID_ARGUMENT
//   - whatsmeow.ErrInviteLinkRevoked          → NOT_FOUND
//   - whatsmeow.ErrGroupInviteLinkUnauthorized → PERMISSION_DENIED
var (
	ErrInvalidJID               = errors.New("groups: invalid jid")
	ErrEmptyInviteCode          = errors.New("groups: invite link/code is required")
	ErrEmptyGroupName           = errors.New("groups: group name is required")
	ErrNoParticipants           = errors.New("groups: at least one participant jid is required")
	ErrInvalidParticipantAction = errors.New("groups: invalid participant action")
)

// inviteLinkPrefix 是 WhatsApp 邀请链接的标准前缀。我们接受
// 完整链接或裸 code（whatsmeow 的 GetGroupInfoFromLink / JoinGroupWithLink
// 在这一层已能容忍前缀，但提前剥离能让我们对纯 code 输入做一致校验）。
const inviteLinkPrefix = "https://chat.whatsapp.com/"

// Handler 是无状态的群组 RPC 入口点。每个 sidecar 一个实例；
// 在所有账号之间并发使用是安全的（whatsmeow.Client 自身是并发安全的）。
//
// 不持有 Registry 引用是刻意的：同 messages.Sender 一样，调用方负责
// 从 registry 取出 *whatsmeow.Client 后传入，这避免了循环引用、
// 并让本包的每个方法都是「纯函数 + 一次 whatsmeow 调用」的结构。
type Handler struct{}

// NewHandler 返回一个 Handler。当前没有可配置项，但保留构造函数
// 以便将来加入指标 / 日志 / 限流等横切关注点而无需破坏调用点。
func NewHandler() *Handler {
	return &Handler{}
}

// ── ListJoinedGroups ─────────────────────────────────────────────────────────

func (h *Handler) ListJoinedGroups(
	ctx context.Context,
	cli *whatsmeow.Client,
) (*pb.ListJoinedGroupsResponse, error) {
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	infos, err := cli.GetJoinedGroups(ctx)
	if err != nil {
		return nil, fmt.Errorf("groups: GetJoinedGroups: %w", err)
	}
	out := &pb.ListJoinedGroupsResponse{
		Groups: make([]*pb.GroupInfo, 0, len(infos)),
	}
	for _, info := range infos {
		out.Groups = append(out.Groups, groupInfoToProto(info))
	}
	return out, nil
}

// ── GetGroupInfo ─────────────────────────────────────────────────────────────

func (h *Handler) GetGroupInfo(
	ctx context.Context,
	cli *whatsmeow.Client,
	groupJID string,
) (*pb.GetGroupInfoResponse, error) {
	jid, err := parseJID(groupJID)
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	info, err := cli.GetGroupInfo(ctx, jid)
	if err != nil {
		return nil, fmt.Errorf("groups: GetGroupInfo %s: %w", jid, err)
	}
	return &pb.GetGroupInfoResponse{GroupInfo: groupInfoToProto(info)}, nil
}

// ── PreviewGroupInvite ───────────────────────────────────────────────────────

func (h *Handler) PreviewGroupInvite(
	ctx context.Context,
	cli *whatsmeow.Client,
	inviteLink string,
) (*pb.PreviewGroupInviteResponse, error) {
	code, err := normalizeInviteCode(inviteLink)
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	info, err := cli.GetGroupInfoFromLink(ctx, code)
	if err != nil {
		return nil, fmt.Errorf("groups: GetGroupInfoFromLink: %w", err)
	}
	return &pb.PreviewGroupInviteResponse{GroupInfo: groupInfoToProto(info)}, nil
}

// ── JoinGroupViaInvite ───────────────────────────────────────────────────────

func (h *Handler) JoinGroupViaInvite(
	ctx context.Context,
	cli *whatsmeow.Client,
	inviteLink string,
) (*pb.JoinGroupViaInviteResponse, error) {
	code, err := normalizeInviteCode(inviteLink)
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	jid, err := cli.JoinGroupWithLink(ctx, code)
	if err != nil {
		return nil, fmt.Errorf("groups: JoinGroupWithLink: %w", err)
	}
	return &pb.JoinGroupViaInviteResponse{GroupJid: jid.String()}, nil
}

// ── LeaveGroup ───────────────────────────────────────────────────────────────

func (h *Handler) LeaveGroup(
	ctx context.Context,
	cli *whatsmeow.Client,
	groupJID string,
) (*pb.LeaveGroupResponse, error) {
	jid, err := parseJID(groupJID)
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	if err := cli.LeaveGroup(ctx, jid); err != nil {
		return nil, fmt.Errorf("groups: LeaveGroup %s: %w", jid, err)
	}
	return &pb.LeaveGroupResponse{}, nil
}

// ── CreateGroup ──────────────────────────────────────────────────────────────

func (h *Handler) CreateGroup(
	ctx context.Context,
	cli *whatsmeow.Client,
	name string,
	participantJIDs []string,
) (*pb.CreateGroupResponse, error) {
	if name == "" {
		return nil, ErrEmptyGroupName
	}
	if len(participantJIDs) == 0 {
		return nil, ErrNoParticipants
	}
	parsed := make([]types.JID, 0, len(participantJIDs))
	for _, raw := range participantJIDs {
		jid, err := parseJID(raw)
		if err != nil {
			return nil, err
		}
		parsed = append(parsed, jid)
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	info, err := cli.CreateGroup(ctx, whatsmeow.ReqCreateGroup{
		Name:         name,
		Participants: parsed,
	})
	if err != nil {
		return nil, fmt.Errorf("groups: CreateGroup: %w", err)
	}
	return &pb.CreateGroupResponse{GroupInfo: groupInfoToProto(info)}, nil
}

// ── UpdateGroupSettings ──────────────────────────────────────────────────────

// UpdateGroupSettings 仅修改 has_* 标志为 true 的字段。其他字段保持不变。
// 如果没有任何字段被标记为修改，返回当前快照（一次 GetGroupInfo 调用）。
//
// whatsmeow 没有「批量更新群设置」API，每个开关都是独立 RPC：
// SetGroupName / SetGroupTopic / SetGroupAnnounce / SetGroupLocked。
// 我们顺序执行，并在最后用一次 GetGroupInfo 拉取最新快照返回，
// 这样客户端只需一次往返就能拿到改动后的完整状态。
//
// 失败语义：第一个失败的子调用会立即返回错误，已成功的更改保留在
// 服务端（whatsmeow 没有事务）。客户端可通过返回的错误重试或回滚。
func (h *Handler) UpdateGroupSettings(
	ctx context.Context,
	cli *whatsmeow.Client,
	req *pb.UpdateGroupSettingsRequest,
) (*pb.UpdateGroupSettingsResponse, error) {
	if req == nil {
		return nil, errors.New("groups: nil UpdateGroupSettingsRequest")
	}
	jid, err := parseJID(req.GetGroupJid())
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}

	if req.GetHasName() {
		if err := cli.SetGroupName(ctx, jid, req.GetName()); err != nil {
			return nil, fmt.Errorf("groups: SetGroupName: %w", err)
		}
	}
	if req.GetHasTopic() {
		// SetGroupTopic 的 prevID/newID 传空字符串：whatsmeow 会自动
		// 取当前 topic_id 并生成新 id。这是绝大多数场景下的正确行为；
		// 高级 topic-id 链式管理留给未来需要时再开 API。
		if err := cli.SetGroupTopic(ctx, jid, "", "", req.GetTopic()); err != nil {
			return nil, fmt.Errorf("groups: SetGroupTopic: %w", err)
		}
	}
	if req.GetHasIsAnnounce() {
		if err := cli.SetGroupAnnounce(ctx, jid, req.GetIsAnnounce()); err != nil {
			return nil, fmt.Errorf("groups: SetGroupAnnounce: %w", err)
		}
	}
	if req.GetHasIsLocked() {
		if err := cli.SetGroupLocked(ctx, jid, req.GetIsLocked()); err != nil {
			return nil, fmt.Errorf("groups: SetGroupLocked: %w", err)
		}
	}

	info, err := cli.GetGroupInfo(ctx, jid)
	if err != nil {
		return nil, fmt.Errorf("groups: GetGroupInfo (post-update) %s: %w", jid, err)
	}
	return &pb.UpdateGroupSettingsResponse{GroupInfo: groupInfoToProto(info)}, nil
}

// ── UpdateGroupParticipants ──────────────────────────────────────────────────

func (h *Handler) UpdateGroupParticipants(
	ctx context.Context,
	cli *whatsmeow.Client,
	req *pb.UpdateGroupParticipantsRequest,
) (*pb.UpdateGroupParticipantsResponse, error) {
	if req == nil {
		return nil, errors.New("groups: nil UpdateGroupParticipantsRequest")
	}
	jid, err := parseJID(req.GetGroupJid())
	if err != nil {
		return nil, err
	}
	action, err := participantActionFromProto(req.GetAction())
	if err != nil {
		return nil, err
	}
	if len(req.GetParticipantJids()) == 0 {
		return nil, ErrNoParticipants
	}
	parsed := make([]types.JID, 0, len(req.GetParticipantJids()))
	for _, raw := range req.GetParticipantJids() {
		pjid, err := parseJID(raw)
		if err != nil {
			return nil, err
		}
		parsed = append(parsed, pjid)
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	updated, err := cli.UpdateGroupParticipants(ctx, jid, parsed, action)
	if err != nil {
		return nil, fmt.Errorf("groups: UpdateGroupParticipants %s/%s: %w", jid, action, err)
	}
	return &pb.UpdateGroupParticipantsResponse{
		Results: participantUpdateResults(updated),
	}, nil
}

// ── GetGroupInviteLink ───────────────────────────────────────────────────────

func (h *Handler) GetGroupInviteLink(
	ctx context.Context,
	cli *whatsmeow.Client,
	groupJID string,
	reset bool,
) (*pb.GetGroupInviteLinkResponse, error) {
	jid, err := parseJID(groupJID)
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("groups: nil whatsmeow.Client")
	}
	link, err := cli.GetGroupInviteLink(ctx, jid, reset)
	if err != nil {
		return nil, fmt.Errorf("groups: GetGroupInviteLink %s: %w", jid, err)
	}
	return &pb.GetGroupInviteLinkResponse{InviteLink: link}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// 辅助函数
// ─────────────────────────────────────────────────────────────────────────────

// parseJID 解析单个 JID 字符串。空串与解析失败都映射到
// ErrInvalidJID（带原值便于服务端日志），让上层用一个 sentinel
// 即可全部归入 INVALID_ARGUMENT。
func parseJID(raw string) (types.JID, error) {
	if raw == "" {
		return types.JID{}, fmt.Errorf("%w: empty", ErrInvalidJID)
	}
	jid, err := types.ParseJID(raw)
	if err != nil {
		return types.JID{}, fmt.Errorf("%w: %s: %v", ErrInvalidJID, raw, err)
	}
	return jid, nil
}

// normalizeInviteCode 接受完整邀请链接或裸 code，剥离前缀后
// 返回 code。空 code 返回 ErrEmptyInviteCode。
//
// whatsmeow 的 GetGroupInfoFromLink / JoinGroupWithLink 内部也会做
// 前缀剥离，但我们先做一次让「空 code」与「合法 code」在校验阶段
// 就分流，避免空字符串走到 whatsmeow 才报出语义不清晰的 invalid 错误。
func normalizeInviteCode(raw string) (string, error) {
	code := strings.TrimPrefix(strings.TrimSpace(raw), inviteLinkPrefix)
	if code == "" {
		return "", ErrEmptyInviteCode
	}
	return code, nil
}

// participantActionFromProto 把 proto enum 映射到 whatsmeow 常量。
// UNSPECIFIED 与未知值都返回 ErrInvalidParticipantAction，
// 强制客户端显式选择动作（防御性默认）。
func participantActionFromProto(a pb.GroupParticipantUpdateEvent_GroupParticipantAction) (whatsmeow.ParticipantChange, error) {
	switch a {
	case pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_ADD:
		return whatsmeow.ParticipantChangeAdd, nil
	case pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_REMOVE:
		return whatsmeow.ParticipantChangeRemove, nil
	case pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_PROMOTE:
		return whatsmeow.ParticipantChangePromote, nil
	case pb.GroupParticipantUpdateEvent_GROUP_PARTICIPANT_ACTION_DEMOTE:
		return whatsmeow.ParticipantChangeDemote, nil
	default:
		return "", fmt.Errorf("%w: %v", ErrInvalidParticipantAction, a)
	}
}
