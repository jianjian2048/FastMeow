// Package receipts 实现了 sidecar 的回执表面：MarkRead RPC + 入站
// events.Receipt 到 wire 的映射。它将 whatsmeow 的 ReceiptType（自由
// 字符串）收敛为 wire 上的强类型 enum，同时为未来 / 未知值保留
// SERVER 兜底，让上游变更不静默破坏 Python 侧的 F filter。
//
// 与 messages / groups 包一致，本包不持有任何 *whatsmeow.Client 引用：
// 调用方（gateway）从 accounts.Registry 取出 client 后传入每次调用，
// 这让本包对账号生命周期无感，并让它易于以纯函数方式进行单测。
//
// converter.go 把 ReceiptType / events.Receipt 映射到 proto。所有转换
// 都是无副作用的纯函数，便于围绕字段语义做单元测试 —— 这正是这部分
// 逻辑被独立成文件的原因。
package receipts

import (
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// receiptTypeFromProto 把 wire enum 映射回 whatsmeow.ReceiptType。
//
// 映射表（与 plan §4.2 一致）：
//
//	UNSPECIFIED → "read"        — 默认值；Python SDK 不会主动发送 UNSPECIFIED
//	                              但若发生（旧客户端或未来字段），按 READ 处理
//	                              比按 DELIVERED("") 处理更安全 —— READ 是
//	                              MarkRead RPC 的「显然意图」。
//	DELIVERED   → ""            — whatsmeow 中空字符串语义。
//	READ        → "read"
//	PLAYED      → "played"
//	SERVER      → "server-error" — 兜底类型；正常路径不应落到这里，
//	                              但 wire 上有此 enum 是为了能从入站 Receipt
//	                              事件回落到一个非 UNSPECIFIED 的具体值。
//
// 注意：whatsmeow.MarkRead 的 vararg 接受任意字符串值，因此本函数永远
// 不会因 enum 未知导致调用失败；最坏情况退化到一个上游不识别的字符串，
// 上游会用其默认 "read" 行为。
func receiptTypeFromProto(rt pb.ReceiptType) types.ReceiptType {
	switch rt {
	case pb.ReceiptType_RECEIPT_TYPE_DELIVERED:
		return types.ReceiptTypeDelivered
	case pb.ReceiptType_RECEIPT_TYPE_READ:
		return types.ReceiptTypeRead
	case pb.ReceiptType_RECEIPT_TYPE_PLAYED:
		return types.ReceiptTypePlayed
	case pb.ReceiptType_RECEIPT_TYPE_SERVER:
		return types.ReceiptTypeServerError
	case pb.ReceiptType_RECEIPT_TYPE_UNSPECIFIED:
		fallthrough
	default:
		return types.ReceiptTypeRead
	}
}

// receiptTypeToProto 把 whatsmeow 的字符串 ReceiptType 映射到 wire enum。
//
// 兜底策略：whatsmeow 定义了多种小众值（retry / sender / read-self /
// played-self / inactive / peer_msg / hist_sync），我们按语义聚合：
//
//   - ""           → DELIVERED        (服务端 ack；最常见)
//   - "read" / "read-self"     → READ
//   - "played" / "played-self" → PLAYED
//   - 其它一切                  → SERVER (兜底；含 retry / sender / inactive 等)
//
// 这样 user code 用 `event.receipt_type == READ` 这样的过滤器就能稳定
// 工作，不会被 whatsmeow 在 minor 版本里加新字符串值打穿。
func receiptTypeToProto(rt types.ReceiptType) pb.ReceiptType {
	switch rt {
	case types.ReceiptTypeDelivered:
		return pb.ReceiptType_RECEIPT_TYPE_DELIVERED
	case types.ReceiptTypeRead, types.ReceiptTypeReadSelf:
		return pb.ReceiptType_RECEIPT_TYPE_READ
	case types.ReceiptTypePlayed, types.ReceiptTypePlayedSelf:
		return pb.ReceiptType_RECEIPT_TYPE_PLAYED
	default:
		return pb.ReceiptType_RECEIPT_TYPE_SERVER
	}
}

// ReceiptEventToProto 把 whatsmeow 的 events.Receipt 映射为 wire 上的
// ReceiptEvent。nil 入参返回 nil（便于在 events/bus.go 的 translate
// 中无条件链式赋值）。
//
// 字段映射注意事项：
//
//   - chat_jid     ← MessageSource.Chat   （DM 即对方，群聊即群）
//   - sender_jid   ← MessageSource.Sender （回执发送者；DM 中等于 chat_jid，
//                                          群聊中是「读了消息的那个人」）
//     注意：events.Receipt 还有一个独立的 MessageSender 字段，语义是
//     「原消息的发送者」（在群里别人读了我发的消息时即「我」）。
//     wire 协议选择暴露 Sender 而非 MessageSender，因为对 user code 来说
//     「谁读了 / 谁发的回执」（=Sender）比「这条消息原发送者」更有用 ——
//     原发送者通常就是 user 自己，可由 account_jid 推断。
//   - message_ids  ← MessageIDs           （批量；不展开成 N 个事件）
//   - timestamp    ← Timestamp            （IsZero 时不填，让 Python 端能区分
//                                          「未知时间」和「epoch 0」）
//   - receipt_type ← Type                 （走 receiptTypeToProto 的兜底映射）
func ReceiptEventToProto(ev *events.Receipt) *pb.ReceiptEvent {
	if ev == nil {
		return nil
	}
	out := &pb.ReceiptEvent{
		ChatJid:     ev.Chat.String(),
		SenderJid:   ev.Sender.String(),
		ReceiptType: receiptTypeToProto(ev.Type),
	}
	if len(ev.MessageIDs) > 0 {
		out.MessageIds = make([]string, 0, len(ev.MessageIDs))
		for _, id := range ev.MessageIDs {
			out.MessageIds = append(out.MessageIds, string(id))
		}
	}
	if !ev.Timestamp.IsZero() {
		out.Timestamp = timestamppb.New(ev.Timestamp)
	}
	return out
}
