// Package events 将 whatsmeow 的事件类型转换为 proto
// StreamEventsResponse 的传输格式，并将它们复用到单个
// 进程范围的通道，用于 gRPC StreamEvents 服务端流。
//
// 架构
//
// 每个 sidecar 进程拥有一个 Bus（事件总线）。通过 accounts 包注册的
// 每个 whatsmeow.Client 都会将其事件处理程序路由到 bus.Sink，它会执行：
//
//  1. 分配一个单调递增的序号（seq），
//  2. 标记观测时间（observed_at），
//  3. 将 Go 事件翻译为 proto 的 oneof 类型，
//  4. 入队到单个有界通道中。
//
// 第一阶段（Phase 1）特意只支持每个 sidecar 拥有一个 StreamEvents 订阅者
// —— 即 Python 管理器 —— 这与 proto 中的注释相匹配
// （"Python 每个 sidecar 正好开启一个 StreamEvents 调用"）。第二次并发的
// Subscribe 会引发 panic，而不是静默地拆分事件。
//
// 背压（Backpressure）
//
// 事件总线通道是有界的（默认 1024）。在溢出时，我们会丢弃
// 传入的事件并输出一条警告日志。我们特意不阻塞
// whatsmeow 的派发 goroutine：阻塞它会导致
// 每个其他账号的事件递送发生停顿。丢弃最新数据的策略
// 在第一阶段是可以接受的，因为：
//
//   - 我们没有订阅软状态事件（presence/receipts），
//     因此通道仅携带连接状态变化和收到的消息，
//     这两者的容量都很低。
//   - 收到的消息本身已经由 whatsmeow 以至多一次（at-most-once）
//     的语义进行递送；重连时在 Python 端进行对账
//     是恢复丢弃事件的正确方式，而不是在此处进行无限缓冲。
//
// # QR 配对
//
// QR 码并不通过 AddEventHandler 传递 —— whatsmeow 通过
// Client.GetQRChannel(ctx) 暴露它们，该方法返回一个 QRChannelItem 的
// 类型化通道。PumpQRChannel 将其接入到事件总线，以便订阅者
// 能通过与其它事件相同的路径看到 qr / pair_success / disconnected 事件。
package events

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// DefaultBufferSize 是事件总线通道的深度。大小设定为能够承载
// 所有账号约 1 秒钟的突发入站流量，然后才会开始丢弃。
const DefaultBufferSize = 1024

// Bus 是整个 sidecar 范围内事件流的唯一汇聚/分发点。
type Bus struct {
	sidecarID string
	log       waLog.Logger

	// out 是唯一的通道；正好被订阅一次。
	out chan *pb.StreamEventsResponse

	// seq 是分配给每个发出的事件的单调递增计数器。
	seq atomic.Uint64

	// subscribed 在第一次 Subscribe 调用时变为 true。第二次调用将 panic。
	subscribed atomic.Bool

	// jids 保存每个 account_key 最近观测到的 JID，以便我们
	// 可以在每个发出的事件上标记 account_jid，而无需强制
	// 调用方进行传递。在 PairSuccess 时更新。
	jidsMu sync.RWMutex
	jids   map[string]string

	// dropCount 在由于 out 通道已满而丢弃事件时递增。
	// 定期记录日志以便运维人员察觉。
	dropCount atomic.Uint64
}

// NewBus 构造一个 Bus。sidecarID 会被标记在每个事件上，用于
// 未来的分片模式；在单一模式下传入 "default"。
func NewBus(sidecarID string, bufferSize int, log waLog.Logger) *Bus {
	if bufferSize <= 0 {
		bufferSize = DefaultBufferSize
	}
	if log == nil {
		log = waLog.Noop
	}
	return &Bus{
		sidecarID: sidecarID,
		log:       log,
		out:       make(chan *pb.StreamEventsResponse, bufferSize),
		jids:      make(map[string]string),
	}
}

// Subscribe 返回事件总线通道的读取端。每个 Bus 生命周期中
// 正好可以调用一次；第二次调用会 panic，因为在两个
// 消费者之间静默地拆分流会导致每个消费者都丢失一半的事件。
func (b *Bus) Subscribe() <-chan *pb.StreamEventsResponse {
	if !b.subscribed.CompareAndSwap(false, true) {
		panic("events.Bus.Subscribe called more than once; only one StreamEvents subscriber is supported")
	}
	return b.out
}

// Close 关闭总线。在 Close 之后，Sink 是一个空操作，并且
// 被订阅的通道会被关闭（这会以 EOF 清洁地终止 gRPC 服务端流）。
func (b *Bus) Close() {
	defer func() {
		// 如果 Close 被调用两次，out 可能已经关闭；
		// 通过 recover 来保持调用的幂等性。
		_ = recover()
	}()
	close(b.out)
}

// Sink 是 accounts 包附加到每个 whatsmeow.Client 的 EventSink 回调。
// 它由 whatsmeow 的 dispatchEvent goroutine 同步调用，
// 因此绝不能阻塞。
func (b *Bus) Sink(accountKey string, evt any) {
	resp := b.translate(accountKey, evt)
	if resp == nil {
		return
	}
	// 在发出之前更新缓存的 JID，以便发出的消息携带它。
	if ps, ok := evt.(*events.PairSuccess); ok {
		b.setJID(accountKey, ps.ID.String())
		// 现在知道了 JID，重新进行标记。
		resp.AccountJid = ps.ID.String()
	}
	b.emit(resp)
}

// PumpQRChannel 从 whatsmeow 的 QR 通道读取 QRChannelItem 值，
// 并将其转发到事件总线上，直到通道关闭或 ctx 被取消。旨在
// 在 Client.GetQRChannel 返回之后（且根据 whatsmeow 的配对协议，
// 在 Client.Connect 之前）立即在其自身的 goroutine 中启动。
func (b *Bus) PumpQRChannel(ctx context.Context, accountKey string, qrCh <-chan whatsmeow.QRChannelItem) {
	for {
		select {
		case <-ctx.Done():
			return
		case item, ok := <-qrCh:
			if !ok {
				return
			}
			resp := b.qrItemToResp(accountKey, item)
			if resp != nil {
				b.emit(resp)
			}
		}
	}
}

// SetAccountJID 让 accounts 包为已经配对并从磁盘加载的账号
// 预置 JID 缓存（以便它们在 Connect 之后发出的第一个事件
// 携带 account_jid）。可选。
func (b *Bus) SetAccountJID(accountKey string, jid types.JID) {
	if jid.IsEmpty() {
		return
	}
	b.setJID(accountKey, jid.String())
}

// ─────────────────────────────────────────────────────────────────────────────
// 内部实现
// ─────────────────────────────────────────────────────────────────────────────

func (b *Bus) emit(resp *pb.StreamEventsResponse) {
	resp.Seq = b.seq.Add(1)
	resp.SidecarId = b.sidecarID
	if resp.ObservedAt == nil {
		resp.ObservedAt = timestamppb.Now()
	}
	if resp.AccountJid == "" {
		resp.AccountJid = b.getJID(resp.AccountKey)
	}

	select {
	case b.out <- resp:
	default:
		// 通道已满。丢弃并记录日志。我们不阻塞 whatsmeow 的
		// 派发 goroutine —— 参见包说明。
		n := b.dropCount.Add(1)
		// 记录第一次丢弃以及之后的每 100 次丢弃，以避免在订阅者
		// 长时间离开时导致日志泛滥。
		if n == 1 || n%100 == 0 {
			b.log.Warnf("events: bus full, dropped event seq=%d account=%q kind=%T (total dropped=%d)",
				resp.Seq, resp.AccountKey, resp.Event, n)
		}
	}
}

func (b *Bus) setJID(accountKey, jid string) {
	b.jidsMu.Lock()
	b.jids[accountKey] = jid
	b.jidsMu.Unlock()
}

func (b *Bus) getJID(accountKey string) string {
	b.jidsMu.RLock()
	defer b.jidsMu.RUnlock()
	return b.jids[accountKey]
}

// translate 将 whatsmeow 事件映射为 *pb.StreamEventsResponse。
// 对于我们特意丢弃的事件，返回 nil（第一阶段忽略软状态事件）。
func (b *Bus) translate(accountKey string, evt any) *pb.StreamEventsResponse {
	base := &pb.StreamEventsResponse{AccountKey: accountKey}

	switch e := evt.(type) {
	case *events.Connected:
		base.Event = &pb.StreamEventsResponse_Connected{Connected: &pb.ConnectedEvent{}}
		return base

	case *events.Disconnected:
		base.Event = &pb.StreamEventsResponse_Disconnected{
			Disconnected: &pb.DisconnectedEvent{Reason: "disconnected"},
		}
		return base

	case *events.LoggedOut:
		base.Event = &pb.StreamEventsResponse_LoggedOut{
			LoggedOut: &pb.LoggedOutEvent{Reason: loggedOutReason(e)},
		}
		return base

	case *events.PairSuccess:
		base.Event = &pb.StreamEventsResponse_PairSuccess{
			PairSuccess: &pb.PairSuccessEvent{
				Jid:          e.ID.String(),
				BusinessName: e.BusinessName,
				Platform:     e.Platform,
			},
		}
		return base

	case *events.Message:
		me := messageEvent(e)
		if me == nil {
			return nil // 第一阶段不支持的消息类型（例如仅包含媒体）
		}
		base.Event = &pb.StreamEventsResponse_Message{Message: me}
		return base

	// 第一阶段：显式丢弃这些嘈杂的软状态事件。它们在配对后会爆发式触发
	// （历史同步可能会发出数十个），而在 v1 版本中 Python 业务代码并不关心它们。
	// 将其中任何一个提升为一等 proto 事件是未来协议升级的内容。
	case *events.HistorySync,
		*events.OfflineSyncPreview,
		*events.OfflineSyncCompleted,
		*events.AppStateSyncComplete,
		*events.AppState,
		*events.PushNameSetting,
		*events.UnarchiveChatsSetting,
		*events.PushName,
		*events.Contact,
		*events.PrivacySettings,
		*events.MediaRetry:
		return nil

	default:
		// 第一阶段：将无法识别的事件表现为 UnknownEvent，以便 Python 端
		// 可以看到正在流动的事件，而无需我们预先翻译每一个变体。
		// 生产环境的 wheel 可能会在调试标志位后将其切换为空操作。
		base.Event = &pb.StreamEventsResponse_Unknown{
			Unknown: &pb.UnknownEvent{GoType: fmt.Sprintf("%T", evt)},
		}
		return base
	}
}

// qrItemToResp 将 whatsmeow.QRChannelItem（QR 通道元素类型）
// 翻译为 StreamEventsResponse。根据上游 API，每个条目的 Event 字段
// 为 "code" / "success" / "timeout" / "err"。
func (b *Bus) qrItemToResp(accountKey string, item whatsmeow.QRChannelItem) *pb.StreamEventsResponse {
	base := &pb.StreamEventsResponse{AccountKey: accountKey}

	switch item.Event {
	case "code":
		base.Event = &pb.StreamEventsResponse_Qr{
			Qr: &pb.QREvent{
				Code:       item.Code,
				TtlSeconds: uint32(item.Timeout / time.Second),
			},
		}
		return base

	case "success":
		// 配对成功；whatsmeow 也会通过带有 JID 的常规处理程序发出 *events.PairSuccess。
		// 我们让该路径发出 proto PairSuccessEvent，以便不进行重复递送。
		return nil

	case "timeout":
		// QR 超时；表现为带原因的 Disconnected，以便 Python 端可以
		// 呈现“扫描失败，请重试”的 UI。
		base.Event = &pb.StreamEventsResponse_Disconnected{
			Disconnected: &pb.DisconnectedEvent{Reason: "qr_timeout"},
		}
		return base

	case "err":
		reason := "qr_error"
		if item.Error != nil {
			reason = "qr_error: " + item.Error.Error()
		}
		base.Event = &pb.StreamEventsResponse_Disconnected{
			Disconnected: &pb.DisconnectedEvent{Reason: reason},
		}
		return base

	default:
		return nil
	}
}

// messageEvent 从 *events.Message 中提取第一阶段所需的字段。
// 如果消息没有可表示的文本主体（例如它是一个仅含图像的消息；
// 第一阶段不携带媒体），则返回 nil。
func messageEvent(e *events.Message) *pb.MessageEvent {
	text, replyTo := extractText(e)
	if text == "" && replyTo == "" {
		// 纯非文本负载；在第一阶段跳过，而不是发出一个
		// Python 端必须特殊处理的空 MessageEvent。
		return nil
	}
	return &pb.MessageEvent{
		MessageId:        e.Info.ID,
		ChatJid:          e.Info.Chat.String(),
		SenderJid:        e.Info.Sender.String(),
		FromMe:           e.Info.IsFromMe,
		Timestamp:        timestamppb.New(e.Info.Timestamp),
		IsGroup:          e.Info.IsGroup,
		Text:             text,
		ReplyToMessageId: replyTo,
	}
}

// extractText 从 *events.Message 中提取可显示的文本 + 回复上下文，
// 同时查找简单的 Conversation 字段和 WhatsApp 用于回复、链接预览等的
// 更丰富的 ExtendedTextMessage 变体。
func extractText(e *events.Message) (text, replyTo string) {
	if e == nil || e.Message == nil {
		return "", ""
	}
	if c := e.Message.GetConversation(); c != "" {
		return c, ""
	}
	if ext := e.Message.GetExtendedTextMessage(); ext != nil {
		text = ext.GetText()
		if ctx := ext.GetContextInfo(); ctx != nil {
			replyTo = ctx.GetStanzaID()
		}
	}
	return text, replyTo
}

// loggedOutReason 将 *events.LoggedOut 渲染为 proto 中携带的自由格式
// 原因字符串。我们避免直接泄露 whatsmeow 内部的 ConnectFailureReason
// 枚举值，以免上游的变化静默地破坏我们的传输格式。
func loggedOutReason(e *events.LoggedOut) string {
	if e == nil {
		return "logged_out"
	}
	if e.OnConnect {
		return fmt.Sprintf("logged_out_on_connect:%d", e.Reason)
	}
	return "logged_out"
}

// errClosedBus 如果未来调用方在 Close 之后向总线询问
// 是否成功发出的确认，则返回该错误（目前未使用）。
var errClosedBus = errors.New("events: bus closed")
var _ = errClosedBus
