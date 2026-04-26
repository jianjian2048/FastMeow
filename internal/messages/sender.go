// Package messages 实现了网关的出站侧：目前仅支持 SendText，
// 后续里程碑将加入对媒体类型的支持。它维护着一个
// (account_key, client_msg_id) 去重缓存，让 Python SDK
// 在遇到瞬时传输故障时可以安全地重试 SendMessage RPC，
// 而不会在 WhatsApp 上产生重复投递的风险。
//
// 去重语义
//
// 第一阶段缓存：
//   - Key: "<account_key>\x00<client_msg_id>"（使用 NUL 分隔，
//     以防止 account_key 和 client_msg_id 在拼接成相同字符串时
//     发生碰撞）。
//   - Value: 成功的 SendMessageResponse。
//   - Capacity: 10,000 个条目，采用 LRU 淘汰策略。
//   - TTL: 每个条目 5 分钟；过期的条目被视为缓存未命中并进行重发。
//
// 缓存范围：
//   - 进程内。sidecar 重启会丢失缓存；在第一阶段这是可以
//     接受的，因为 Python 在崩溃时也不会持久化任何正在发送的消息，
//     且新的 sidecar 在 Python 重试时最多只会重新投递一次。
//   - 仅缓存成功的发送。失败的发送不被缓存，以便 Python
//     的重试能够真正生效。
//
// 我们特意不根据消息体哈希进行缓存。client_msg_id 是
// 保证正确性的唯一键 —— 基于消息体哈希的去重会静默地
// 抑制内容相同的合法重发（例如自动化的每小时 "OK" 回复）。
package messages

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	lru "github.com/hashicorp/golang-lru/v2"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// 默认值与 proto 文件中记录的值一致。
const (
	DefaultDedupCapacity = 10_000
	DefaultDedupTTL      = 5 * time.Minute
)

// 暴露给 gRPC 处理程序的错误。在服务端层使用 status.Error
// 进行封装以映射到 gRPC 状态码。
var (
	ErrEmptyClientMsgID = errors.New("messages: client_msg_id is required")
	ErrEmptyToJID       = errors.New("messages: to_jid is required")
	ErrEmptyBody        = errors.New("messages: text body is required")
	ErrInvalidJID       = errors.New("messages: invalid jid")
)

// Sender 是出站消息的入口点。每个 sidecar 一个实例；
// 在所有账号之间并发使用是安全的（whatsmeow.Client
// 自身在内部会按账号对发送进行串行化）。
type Sender struct {
	dedup *lru.Cache[string, dedupEntry]
	ttl   time.Duration
	now   func() time.Time // 可在测试中重写
}

type dedupEntry struct {
	resp     *pb.SendMessageResponse
	cachedAt time.Time
}

// NewSender 使用默认的去重容量和 TTL 构造一个 Sender。
// 传入 capacity<=0 / ttl<=0 将使用默认值。
func NewSender(capacity int, ttl time.Duration) (*Sender, error) {
	if capacity <= 0 {
		capacity = DefaultDedupCapacity
	}
	if ttl <= 0 {
		ttl = DefaultDedupTTL
	}
	cache, err := lru.New[string, dedupEntry](capacity)
	if err != nil {
		return nil, fmt.Errorf("messages: build dedup cache: %w", err)
	}
	return &Sender{
		dedup: cache,
		ttl:   ttl,
		now:   time.Now,
	}, nil
}

// SendText 通过 cli 向给定接收者发送纯文本消息。
// (accountKey, clientMsgID) 元组用作去重缓存的键。如果缓存中
// 存在该元组的新鲜成功发送记录，则返回缓存的响应，并将
// deduped 设置为 true，且跳过 WhatsApp 发送。
//
// accountKey 纯粹用于缓存范围限定；它不会根据传入的
// cli 进行校验。调用方（gRPC 服务端）负责提供匹配的值。
func (s *Sender) SendText(
	ctx context.Context,
	cli *whatsmeow.Client,
	accountKey string,
	toJID string,
	clientMsgID string,
	body string,
	replyToMessageID string,
) (*pb.SendMessageResponse, error) {
	if cli == nil {
		return nil, errors.New("messages: nil whatsmeow.Client")
	}
	if clientMsgID == "" {
		return nil, ErrEmptyClientMsgID
	}
	if toJID == "" {
		return nil, ErrEmptyToJID
	}
	if body == "" {
		return nil, ErrEmptyBody
	}

	// 去重缓存检查。
	key := dedupKey(accountKey, clientMsgID)
	if cached, ok := s.dedup.Get(key); ok {
		if s.now().Sub(cached.cachedAt) <= s.ttl {
			// 克隆对象以便调用方在不污染缓存的情况下进行修改，
			// 并重新标记 deduped=true，即使缓存的条目原本
			// 返回的是 false（它总是 false）。
			out := proto.Clone(cached.resp).(*pb.SendMessageResponse)
			out.Deduped = true
			return out, nil
		}
		// 已过期；移除并继续执行真实发送。
		s.dedup.Remove(key)
	}

	jid, err := types.ParseJID(toJID)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidJID, toJID, err)
	}

	msg := buildTextMessage(body, replyToMessageID)

	// 我们将 clientMsgID 作为消息 ID 传递给 whatsmeow。
	// 这赋予了我们端到端的幂等性：即使在未来的高可用架构中，
	// 两个 sidecar 都看到了相同的重试，WhatsApp 自身的去重
	// 也会合并它们。建议使用 UUID 形式的 clientMsgID；
	// whatsmeow 在此处接受任何非空字符串。
	extra := whatsmeow.SendRequestExtra{ID: types.MessageID(clientMsgID)}

	resp, err := cli.SendMessage(ctx, jid, msg, extra)
	if err != nil {
		return nil, fmt.Errorf("messages: SendMessage to %s: %w", jid, err)
	}

	out := &pb.SendMessageResponse{
		MessageId:       resp.ID,
		ServerTimestamp: timestamppb.New(resp.Timestamp),
		Deduped:         false,
	}

	s.dedup.Add(key, dedupEntry{resp: out, cachedAt: s.now()})
	return out, nil
}

// buildTextMessage 构造一个 whatsmeow 纯文本负载。如果 replyTo
// 非空，我们将其封装在 ExtendedTextMessage 中以携带回复引用，
// 否则简单的 Conversation 字段就足够了。这两种形式在接收端
// 渲染效果相同。
func buildTextMessage(body, replyTo string) *waE2E.Message {
	if replyTo == "" {
		return &waE2E.Message{Conversation: proto.String(body)}
	}
	return &waE2E.Message{
		ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String(body),
			ContextInfo: &waE2E.ContextInfo{
				StanzaID: proto.String(replyTo),
			},
		},
	}
}

// dedupKey 组合缓存键。参见包说明以了解为何使用 NUL 分隔。
func dedupKey(accountKey, clientMsgID string) string {
	var b strings.Builder
	b.Grow(len(accountKey) + 1 + len(clientMsgID))
	b.WriteString(accountKey)
	b.WriteByte(0)
	b.WriteString(clientMsgID)
	return b.String()
}
