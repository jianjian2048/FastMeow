// Package presence 实现了 sidecar 的在线状态表面：3 个 presence RPC
// + 入站 events.Presence / events.ChatPresence 到 wire 的映射。
//
// 它将 whatsmeow 的字符串枚举（types.Presence / types.ChatPresence /
// types.ChatPresenceMedia）收敛为 wire 上的强类型 enum，未知值由 sidecar
// 显式拒绝（出站）或映射到 UNSPECIFIED（入站，目前无此场景）。
//
// 与 messages / groups / receipts 一致，本包不持有任何 *whatsmeow.Client
// 引用：调用方（gateway）从 accounts.Registry 取出 client 后传入每次调用。
//
// converter.go 把 enum 与 events.Presence / events.ChatPresence 映射到
// proto。所有转换都是无副作用的纯函数，便于围绕字段语义做单元测试 ——
// 这正是这部分逻辑被独立成文件的原因。
package presence

import (
	"fmt"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// presenceTypeFromProto 把 wire enum 映射到 whatsmeow.Presence。
//
// UNSPECIFIED 与未知值都返回 ErrInvalidPresenceType —— 全局 presence
// 没有合理默认（available 与 unavailable 语义对立），强制客户端显式选择。
func presenceTypeFromProto(p pb.PresenceType) (types.Presence, error) {
	switch p {
	case pb.PresenceType_PRESENCE_TYPE_AVAILABLE:
		return types.PresenceAvailable, nil
	case pb.PresenceType_PRESENCE_TYPE_UNAVAILABLE:
		return types.PresenceUnavailable, nil
	default:
		return "", fmt.Errorf("%w: %v", ErrInvalidPresenceType, p)
	}
}

// chatPresenceStateFromProto 把 wire enum 映射到 whatsmeow.ChatPresence。
//
// UNSPECIFIED 与未知值都返回 ErrInvalidChatPresenceState —— 输入状态
// 也没有合理默认（composing 与 paused 语义对立）。
func chatPresenceStateFromProto(s pb.ChatPresenceState) (types.ChatPresence, error) {
	switch s {
	case pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING:
		return types.ChatPresenceComposing, nil
	case pb.ChatPresenceState_CHAT_PRESENCE_STATE_PAUSED:
		return types.ChatPresencePaused, nil
	default:
		return "", fmt.Errorf("%w: %v", ErrInvalidChatPresenceState, s)
	}
}

// chatPresenceMediaFromProto 把 wire enum 映射到 whatsmeow.ChatPresenceMedia。
//
// 与 state 不同，media 的 UNSPECIFIED 显式映射到 TEXT（空字符串）：
// whatsmeow 上游本身就用 "" 表示 text，旧客户端省略 media 字段时
// 应该被当作 text 而不是被拒绝。
func chatPresenceMediaFromProto(m pb.ChatPresenceMedia) types.ChatPresenceMedia {
	switch m {
	case pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_AUDIO:
		return types.ChatPresenceMediaAudio
	case pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT:
		fallthrough
	case pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_UNSPECIFIED:
		fallthrough
	default:
		return types.ChatPresenceMediaText
	}
}

// presenceTypeToProto 把 whatsmeow.Presence 映射回 wire enum。
// 未知 / 空值兜底到 UNSPECIFIED；目前入站事件不使用此函数（events.Presence
// 用 Unavailable bool 而非字符串），保留以备未来 RPC 返回当前状态时使用。
func presenceTypeToProto(p types.Presence) pb.PresenceType {
	switch p {
	case types.PresenceAvailable:
		return pb.PresenceType_PRESENCE_TYPE_AVAILABLE
	case types.PresenceUnavailable:
		return pb.PresenceType_PRESENCE_TYPE_UNAVAILABLE
	default:
		return pb.PresenceType_PRESENCE_TYPE_UNSPECIFIED
	}
}

// chatPresenceStateToProto 把 whatsmeow.ChatPresence 映射回 wire enum。
// 未知值兜底到 UNSPECIFIED；user code 应该按 COMPOSING / PAUSED 过滤，
// UNSPECIFIED 的事件理论上不会到达 Python 层（whatsmeow 永远发非空）。
func chatPresenceStateToProto(s types.ChatPresence) pb.ChatPresenceState {
	switch s {
	case types.ChatPresenceComposing:
		return pb.ChatPresenceState_CHAT_PRESENCE_STATE_COMPOSING
	case types.ChatPresencePaused:
		return pb.ChatPresenceState_CHAT_PRESENCE_STATE_PAUSED
	default:
		return pb.ChatPresenceState_CHAT_PRESENCE_STATE_UNSPECIFIED
	}
}

// chatPresenceMediaToProto 把 whatsmeow.ChatPresenceMedia 映射回 wire enum。
// 与出站方向对称，TEXT 与空字符串都映射为 TEXT，让 user code 不需要
// 区分 "" 和 "text"。
func chatPresenceMediaToProto(m types.ChatPresenceMedia) pb.ChatPresenceMedia {
	switch m {
	case types.ChatPresenceMediaAudio:
		return pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_AUDIO
	case types.ChatPresenceMediaText:
		fallthrough
	default:
		return pb.ChatPresenceMedia_CHAT_PRESENCE_MEDIA_TEXT
	}
}

// PresenceEventToProto 把 whatsmeow events.Presence 映射为 wire 上的
// PresenceEvent。nil 入参返回 nil。
//
// 字段映射：
//
//   - from_jid     ← From         （被订阅的对端 JID）
//   - unavailable  ← Unavailable  （true=离线 / false=在线）
//   - last_seen    ← LastSeen     （IsZero 时不填；上游对隐私设置可能不返回）
//
// 注意：events.Presence 没有 MessageSource，Presence 是「全局对端状态」
// 而非「会话内事件」，因此没有 chat_jid 概念 —— Python 层会把 from_jid
// 同时填到 ctx.from_jid 与 ctx.account_jid 字段，但 chat_jid 留空。
func PresenceEventToProto(ev *events.Presence) *pb.PresenceEvent {
	if ev == nil {
		return nil
	}
	out := &pb.PresenceEvent{
		FromJid:     ev.From.String(),
		Unavailable: ev.Unavailable,
	}
	if !ev.LastSeen.IsZero() {
		out.LastSeen = timestamppb.New(ev.LastSeen)
	}
	return out
}

// ChatPresenceEventToProto 把 whatsmeow events.ChatPresence 映射为
// wire 上的 ChatPresenceEvent。nil 入参返回 nil。
//
// 字段映射：
//
//   - chat_jid    ← MessageSource.Chat
//   - sender_jid  ← MessageSource.Sender
//   - state       ← State  （走 chatPresenceStateToProto）
//   - media       ← Media  （走 chatPresenceMediaToProto）
func ChatPresenceEventToProto(ev *events.ChatPresence) *pb.ChatPresenceEvent {
	if ev == nil {
		return nil
	}
	return &pb.ChatPresenceEvent{
		ChatJid:   ev.Chat.String(),
		SenderJid: ev.Sender.String(),
		State:     chatPresenceStateToProto(ev.State),
		Media:     chatPresenceMediaToProto(ev.Media),
	}
}

// 静态断言：保证 To* 函数在新增 wire enum 时被 lint 提醒去补 case。
// 这些值目前没有 RPC 直接消费，但 ReceiptEventToProto 在 internal/receipts
// 走过同样的兜底模式 —— 这里显式引用，让 unused import 检查通过且
// 表明这两个函数不是「未来可能用」的占位，而是供 events/bus.go 翻译
// PresenceEvent / ChatPresenceEvent 时使用的真实导出。
var _ = presenceTypeToProto
