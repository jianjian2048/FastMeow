// handler.go 暴露 3 个 presence RPC 的领域逻辑：
//
//   - SendPresence       — 全局在线状态（available/unavailable）
//   - SendChatPresence   — 单聊输入状态（composing/paused, text/audio）
//   - SubscribePresence  — 订阅某个 JID 的入站 PresenceEvent
//
// 它对 wire 协议无感（除返回 *pb.XxxResponse 这一约定外），把所有错误
// 以 sentinel 值的形式返回；gRPC 服务端层负责将其映射成 codes.* 状态码。
package presence

import (
	"context"
	"errors"
	"fmt"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// 暴露给 gRPC 处理程序的 sentinel 错误。
//
// 校验类（→ INVALID_ARGUMENT）：
//   - ErrInvalidJID                chat_jid / target jid 解析失败
//   - ErrInvalidPresenceType       PresenceType 为 UNSPECIFIED 或未知值
//   - ErrInvalidChatPresenceState  ChatPresenceState 为 UNSPECIFIED 或未知值
//
// ChatPresenceMedia 的 UNSPECIFIED 不是错误：我们将其等同于 TEXT，
// 这与 whatsmeow 上游空字符串语义一致，让旧客户端可以省略该字段。
var (
	ErrInvalidJID                = errors.New("presence: invalid jid")
	ErrInvalidPresenceType       = errors.New("presence: invalid presence type")
	ErrInvalidChatPresenceState  = errors.New("presence: invalid chat presence state")
)

// Handler 是无状态的 presence RPC 入口点。每个 sidecar 一个实例。
//
// 与 receipts.Handler / groups.Handler 一致，不持有 *whatsmeow.Client：
// 调用方负责从 registry 取出 client 后传入。
type Handler struct{}

// NewHandler 返回一个 Handler。当前没有可配置项，但保留构造函数
// 以便将来加入指标 / 日志 / 限流等横切关注点而无需破坏调用点。
func NewHandler() *Handler {
	return &Handler{}
}

// ── SendPresence ────────────────────────────────────────────────────────────
//
// whatsmeow.SendPresence 设置进程级的全局在线状态，不针对单个聊天。
// WhatsApp 要求显式 "available" 才能让其它端看到我们在线 / 收到 Presence
// 推送。该 RPC 通常由 user code 在启动后立刻调用一次。
func (h *Handler) SendPresence(
	ctx context.Context,
	cli *whatsmeow.Client,
	req *pb.SendPresenceRequest,
) (*pb.SendPresenceResponse, error) {
	if req == nil {
		return nil, errors.New("presence: nil SendPresenceRequest")
	}
	state, err := presenceTypeFromProto(req.GetPresence())
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("presence: nil whatsmeow.Client")
	}
	if err := cli.SendPresence(ctx, state); err != nil {
		return nil, fmt.Errorf("presence: SendPresence %s: %w", state, err)
	}
	return &pb.SendPresenceResponse{}, nil
}

// ── SendChatPresence ────────────────────────────────────────────────────────
//
// whatsmeow.SendChatPresence(jid, state, media) 发送单聊范围的输入状态。
// 媒体维度（TEXT / AUDIO）告知对端是「正在打字」还是「正在录音」。
//
// 客户端语义提示：composing 一段时间后应该主动发 paused（whatsmeow 不会
// 自动衰减）。Python SDK 用上下文管理器封装这个模式（Ctx.send_typing()）。
func (h *Handler) SendChatPresence(
	ctx context.Context,
	cli *whatsmeow.Client,
	req *pb.SendChatPresenceRequest,
) (*pb.SendChatPresenceResponse, error) {
	if req == nil {
		return nil, errors.New("presence: nil SendChatPresenceRequest")
	}
	jid, err := parseJID(req.GetChatJid())
	if err != nil {
		return nil, err
	}
	state, err := chatPresenceStateFromProto(req.GetState())
	if err != nil {
		return nil, err
	}
	media := chatPresenceMediaFromProto(req.GetMedia())
	if cli == nil {
		return nil, errors.New("presence: nil whatsmeow.Client")
	}
	if err := cli.SendChatPresence(ctx, jid, state, media); err != nil {
		return nil, fmt.Errorf("presence: SendChatPresence %s/%s/%s: %w",
			jid, state, media, err)
	}
	return &pb.SendChatPresenceResponse{}, nil
}

// ── SubscribePresence ───────────────────────────────────────────────────────
//
// whatsmeow.SubscribePresence(jid) 注册对该 JID 的全局 Presence 推送。
// 不订阅时不会收到 PresenceEvent，因此 user code 必须先调用此 RPC
// 才能让 router.presence() 装饰器生效。
//
// 上游对同一 JID 的多次 Subscribe 是幂等的（仅刷新订阅期），
// 我们因此不做去重，让调用方决定订阅时机。
func (h *Handler) SubscribePresence(
	ctx context.Context,
	cli *whatsmeow.Client,
	req *pb.SubscribePresenceRequest,
) (*pb.SubscribePresenceResponse, error) {
	if req == nil {
		return nil, errors.New("presence: nil SubscribePresenceRequest")
	}
	jid, err := parseJID(req.GetJid())
	if err != nil {
		return nil, err
	}
	if cli == nil {
		return nil, errors.New("presence: nil whatsmeow.Client")
	}
	if err := cli.SubscribePresence(ctx, jid); err != nil {
		return nil, fmt.Errorf("presence: SubscribePresence %s: %w", jid, err)
	}
	return &pb.SubscribePresenceResponse{}, nil
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
