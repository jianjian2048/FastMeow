// Package groups 实现了 sidecar 的群组管理表面：列出 / 查询 / 创建 / 加入 /
// 退出 / 改设置 / 改成员 / 邀请链接。它将 whatsmeow 的群组 API 包装成
// proto 消息，并把领域错误暴露为 errors.Is-able 的 sentinel 值，
// 由 gRPC 服务端层负责映射到 status code。
//
// 与 messages 包一致，本包不持有任何 *whatsmeow.Client 引用：
// 调用方（gateway）从 accounts.Registry 取出 client 后传入每次调用，
// 这让本包对账号生命周期无感，并让它易于以纯函数方式进行单测。
//
// converter.go 把 whatsmeow types.GroupInfo / types.GroupParticipant 映射到
// proto。所有转换都是无副作用的纯函数，便于围绕字段语义做单元测试 ——
// 这正是这部分逻辑被独立成文件的原因。
package groups

import (
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// GroupInfoToProto 是 groupInfoToProto 的导出别名，供 events 包在
// 翻译 *events.JoinedGroup / *events.GroupInfo 时复用同一份字段映射逻辑，
// 避免把 11 字段的转换在两个包里重复实现并随时间漂移。
func GroupInfoToProto(info *types.GroupInfo) *pb.GroupInfo {
	return groupInfoToProto(info)
}

// groupInfoToProto 把 whatsmeow 的 GroupInfo 映射为 wire 上的 GroupInfo。
// nil 入参返回 nil（便于在 RPC handler 中无条件地链式赋值）。
//
// 字段映射注意事项：
//   - membership_approval_mode 透传 whatsmeow 给出的字符串，
//     已知值见 proto 注释。我们不在这里做枚举化，
//     是为了保证当 WhatsApp 侧引入新值时无需改动 wire 协议。
//   - ephemeral_duration_seconds 仅在 IsEphemeral=true 时才有意义，
//     但我们无条件填充以避免「字段缺失」与「显式 0 秒」之间的歧义。
//   - 创建时间使用 GroupCreated（非零值时才转 timestamppb）。
func groupInfoToProto(info *types.GroupInfo) *pb.GroupInfo {
	if info == nil {
		return nil
	}
	out := &pb.GroupInfo{
		Jid:                      info.JID.String(),
		Name:                     info.Name,
		Topic:                    info.Topic,
		IsAnnounce:               info.IsAnnounce,
		IsLocked:                 info.IsLocked,
		IsEphemeral:              info.IsEphemeral,
		EphemeralDurationSeconds: info.DisappearingTimer,
		MembershipApprovalMode:   info.DefaultMembershipApprovalMode,
	}
	if !info.OwnerJID.IsEmpty() {
		out.OwnerJid = info.OwnerJID.String()
	}
	if !info.GroupCreated.IsZero() {
		out.CreationTimestamp = timestamppb.New(info.GroupCreated)
	}
	if len(info.Participants) > 0 {
		out.Participants = make([]*pb.GroupParticipant, 0, len(info.Participants))
		for i := range info.Participants {
			out.Participants = append(out.Participants, groupParticipantToProto(&info.Participants[i]))
		}
	}
	return out
}

// groupParticipantToProto 把 whatsmeow 的 GroupParticipant 映射为 wire 上的
// GroupParticipant。注意：本 wire 类型只保留身份与权限标记，
// 不携带 GroupParticipantAddRequest 等加群审批信息 —— 这部分目前
// 走事件流，未来如需暴露可在此追加字段。
func groupParticipantToProto(p *types.GroupParticipant) *pb.GroupParticipant {
	if p == nil {
		return nil
	}
	return &pb.GroupParticipant{
		Jid:          p.JID.String(),
		IsAdmin:      p.IsAdmin,
		IsSuperAdmin: p.IsSuperAdmin,
	}
}

// participantUpdateResults 把 UpdateGroupParticipants 返回的逐成员结果
// 映射为 wire 上的列表。whatsmeow 通过非零 Error 字段表示失败，
// 我们将其转成 success=false + 数字 error_code 字符串。
//
// error_message 留空的策略：whatsmeow 这一层只回 HTTP-like 数字码
// （403=屏蔽添加请求，408=未授权，409=已是成员，等等），
// 由 Python SDK 侧根据语境给出人类可读消息更合适。
func participantUpdateResults(updated []types.GroupParticipant) []*pb.GroupParticipantUpdateResult {
	if len(updated) == 0 {
		return nil
	}
	out := make([]*pb.GroupParticipantUpdateResult, 0, len(updated))
	for i := range updated {
		p := &updated[i]
		r := &pb.GroupParticipantUpdateResult{
			Jid:     p.JID.String(),
			Success: p.Error == 0,
		}
		if p.Error != 0 {
			r.ErrorCode = itoa(p.Error)
		}
		out = append(out, r)
	}
	return out
}

// itoa 是 strconv.Itoa 的最小化内联实现，避免为单一调用点
// 引入对 strconv 包的依赖。仅处理 [0, ~math.MaxInt) 区间的值，
// 这对 whatsmeow 返回的 HTTP-like 状态码（3 位数）足够。
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := false
	if n < 0 {
		neg = true
		n = -n
	}
	// 20 位足以容纳 int64 的十进制表示。
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
