// handler.go 暴露 MarkRead RPC 的领域逻辑。它对 wire 协议无感
// （除返回 *pb.MarkReadResponse 这一约定外），把所有错误以 sentinel
// 值的形式返回；gRPC 服务端层负责将其映射成 codes.InvalidArgument 等。
package receipts

import (
	"context"
	"errors"
	"fmt"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// 暴露给 gRPC 处理程序的 sentinel 错误。
//
// 校验类（→ INVALID_ARGUMENT）：
//   - ErrInvalidJID            chat_jid / sender_jid 解析失败
//   - ErrEmptyMessageIDs       message_ids 为空
//   - ErrEmptyMessageID        message_ids 中含空字符串
var (
	ErrInvalidJID      = errors.New("receipts: invalid jid")
	ErrEmptyMessageIDs = errors.New("receipts: at least one message_id is required")
	ErrEmptyMessageID  = errors.New("receipts: message_id must not be empty")
)

// Handler 是无状态的回执 RPC 入口点。每个 sidecar 一个实例；
// 在所有账号之间并发使用是安全的（whatsmeow.Client 自身是并发安全的）。
//
// 与 messages.Sender / groups.Handler 一致，不持有 *whatsmeow.Client：
// 调用方负责从 registry 取出 client 后传入。这避免了循环引用，
// 并让每个方法都是「纯函数 + 一次 whatsmeow 调用」的结构，便于单测。
type Handler struct{}

// NewHandler 返回一个 Handler。当前没有可配置项，但保留构造函数
// 以便将来加入指标 / 日志 / 限流等横切关注点而无需破坏调用点。
func NewHandler() *Handler {
	return &Handler{}
}

// ── MarkRead ────────────────────────────────────────────────────────────────
//
// whatsmeow.MarkRead 的签名是
//
//	MarkRead(ctx, ids []MessageID, timestamp time.Time, chat, sender JID,
//	         receiptTypeExtra ...ReceiptType) error
//
// receiptTypeExtra 是 vararg 但实际只取首个（whatsmeow 内部 receipt.go 的
// 实现明确写明此约定）。我们在 sidecar 这一层显式取首个并丢弃多余值，
// 防止上游未来在该 vararg 上做出语义变化时静默泄露到 wire 协议。
func (h *Handler) MarkRead(
	ctx context.Context,
	cli *whatsmeow.Client,
	req *pb.MarkReadRequest,
) (*pb.MarkReadResponse, error) {
	if req == nil {
		return nil, errors.New("receipts: nil MarkReadRequest")
	}

	// 参数校验先于 nil-cli 检查：让单测可以无 client 驱动校验路径，
	// 与 internal/groups.Handler 同模式。
	rawIDs := req.GetMessageIds()
	if len(rawIDs) == 0 {
		return nil, ErrEmptyMessageIDs
	}
	ids := make([]types.MessageID, 0, len(rawIDs))
	for _, raw := range rawIDs {
		if raw == "" {
			return nil, ErrEmptyMessageID
		}
		ids = append(ids, types.MessageID(raw))
	}

	chat, err := parseJID(req.GetChatJid())
	if err != nil {
		return nil, err
	}
	sender, err := parseJID(req.GetSenderJid())
	if err != nil {
		return nil, err
	}

	if cli == nil {
		return nil, errors.New("receipts: nil whatsmeow.Client")
	}

	// read_at 为空时取当前时间。WhatsApp 服务端要求一个时间戳，
	// 缺失会被认为是异常 ack；time.Now() 是最合理的兜底。
	ts := time.Now()
	if req.GetReadAt() != nil {
		ts = req.GetReadAt().AsTime()
	}

	rt := receiptTypeFromProto(req.GetReceiptType())

	if err := cli.MarkRead(ctx, ids, ts, chat, sender, rt); err != nil {
		return nil, fmt.Errorf("receipts: MarkRead chat=%s sender=%s ids=%d: %w",
			chat, sender, len(ids), err)
	}
	return &pb.MarkReadResponse{}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// 辅助函数
// ─────────────────────────────────────────────────────────────────────────────

// parseJID 解析单个 JID 字符串。空串与解析失败都映射到
// ErrInvalidJID（带原值便于服务端日志），让上层用一个 sentinel
// 即可全部归入 INVALID_ARGUMENT。
//
// 与 internal/groups.parseJID 同语义但拷贝在此包，避免跨包私有依赖；
// 两份实现足够小（< 10 行），可接受这点重复。
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
