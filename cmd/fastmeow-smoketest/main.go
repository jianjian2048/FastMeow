// fastmeow-smoketest 是用于第一阶段端到端验证的单二进制测试工具：
// 它不启动任何进程 —— 仅连接由操作员启动的 sidecar —— 并引导一个
// 账号完成配对 + 接收消息 + 回复消息的流程。
//
// 使用方法（两个终端）：
//
//	# 终端 A：启动 sidecar
//	./bin/fastmeow-sidecar.exe --listen tcp://127.0.0.1:50071 --session-dir ./smoke-sessions
//
//	# 终端 B：驱动测试
//	./bin/fastmeow-smoketest.exe --addr 127.0.0.1:50071 --account-key smoke
//
// 具体流程：
//  1. Ping（验证协议版本）。
//  2. EnsureAccount(account_key, jid="") → 创建新设备。
//  3. 在 goroutine 中执行 StreamEvents，打印每个事件。
//  4. Connect → 触发 sidecar 上的 QR 码推送。
//  5. 收到第一个 QREvent 时，在终端渲染 QR 码，以便你可以用测试手机扫码。
//  6. 收到 PairSuccess 时，打印绑定的 JID。
//  7. 收到第一个入站 MessageEvent（非 from_me）时，使用带有新
//     client_msg_id 的 SendMessage 回复 "👋 fastmeow phase 1 ack"。
//  8. 回复成功后，以状态码 0 退出。任何意外错误将以非零状态码退出。
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/mdp/qrterminal/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

const protocolVersion uint32 = 1

func main() {
	var (
		addr       = flag.String("addr", "127.0.0.1:50071", "sidecar gRPC address")
		accountKey = flag.String("account-key", "smoke", "account key to pair")
		replyText  = flag.String("reply", "fastmeow phase 1 ack", "reply body")
		timeout    = flag.Duration("timeout", 5*time.Minute, "overall test timeout (covers your QR scan time)")
	)
	flag.Parse()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	ctx, cancelTimeout := context.WithTimeout(ctx, *timeout)
	defer cancelTimeout()

	conn, err := grpc.NewClient(*addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("dial %s: %v", *addr, err)
	}
	defer conn.Close()

	cli := pb.NewGatewayServiceClient(conn)

	// 1. Ping
	pingResp, err := cli.Ping(ctx, &pb.PingRequest{ClientProtocolVersion: protocolVersion})
	if err != nil {
		log.Fatalf("ping: %v", err)
	}
	log.Printf("ping ok: sidecar=%s proto=%d sidecar_id=%s",
		pingResp.GetSidecarVersion(), pingResp.GetServerProtocolVersion(), pingResp.GetSidecarId())

	// 2. EnsureAccount (jid 为空 -> 为 QR 配对创建新设备)
	ens, err := cli.EnsureAccount(ctx, &pb.EnsureAccountRequest{AccountKey: *accountKey})
	if err != nil {
		log.Fatalf("ensure: %v", err)
	}
	log.Printf("ensure ok: created=%v state=%s jid=%q", ens.GetCreated(),
		ens.GetState().GetState(), ens.GetState().GetJid())

	// 3. 在 goroutine 中执行 StreamEvents。通过 channel 发送关键信号。
	stream, err := cli.StreamEvents(ctx, &pb.StreamEventsRequest{})
	if err != nil {
		log.Fatalf("stream: %v", err)
	}

	type inbound struct {
		fromJID   string
		messageID string
	}
	paired := make(chan struct{}, 1)
	connected := make(chan struct{}, 1)
	firstInbound := make(chan inbound, 1)
	streamErr := make(chan error, 1)

	go func() {
		for {
			ev, err := stream.Recv()
			if err == io.EOF {
				streamErr <- fmt.Errorf("stream EOF")
				return
			}
			if err != nil {
				streamErr <- err
				return
			}
			describeEvent(ev)

			switch x := ev.GetEvent().(type) {
			case *pb.StreamEventsResponse_Qr:
				renderQR(x.Qr.GetCode())
			case *pb.StreamEventsResponse_PairSuccess:
				log.Printf(">>> PAIRED as %s (%s)", x.PairSuccess.GetJid(), x.PairSuccess.GetPlatform())
				selectSend(paired)
			case *pb.StreamEventsResponse_Connected:
				selectSend(connected)
			case *pb.StreamEventsResponse_Message:
				m := x.Message
				if m.GetFromMe() {
					continue
				}
				if m.GetText() == "" {
					continue
				}
				select {
				case firstInbound <- inbound{fromJID: m.GetChatJid(), messageID: m.GetMessageId()}:
				default:
				}
			}
		}
	}()

	// 4. Connect (对未配对设备触发 sidecar 上的 QR 码推送)
	if _, err := cli.Connect(ctx, &pb.ConnectRequest{AccountKey: *accountKey}); err != nil {
		log.Fatalf("connect: %v", err)
	}
	log.Printf("connect issued; waiting for QR / PairSuccess / Connected …")

	// 5/6. 等待配对完成并连接成功
	if err := waitFor(ctx, "paired", paired, streamErr); err != nil {
		log.Fatalf("%v", err)
	}
	if err := waitFor(ctx, "connected", connected, streamErr); err != nil {
		log.Fatalf("%v", err)
	}

	log.Printf("READY. Now send any text message to this WhatsApp account from another phone.")

	// 7. 等待第一个入站消息，然后回复
	var in inbound
	select {
	case <-ctx.Done():
		log.Fatalf("timeout waiting for inbound message: %v", ctx.Err())
	case err := <-streamErr:
		log.Fatalf("stream error while waiting for inbound: %v", err)
	case in = <-firstInbound:
	}

	clientMsgID := uuid.NewString()
	sendResp, err := cli.SendMessage(ctx, &pb.SendMessageRequest{
		AccountKey:  *accountKey,
		ToJid:       in.fromJID,
		ClientMsgId: clientMsgID,
		Text: &pb.TextBody{
			Body:             *replyText,
			ReplyToMessageId: in.messageID,
		},
	})
	if err != nil {
		log.Fatalf("send: %v", err)
	}
	log.Printf("REPLY SENT id=%s deduped=%v", sendResp.GetMessageId(), sendResp.GetDeduped())

	// 幂等性检查：相同的 client_msg_id 应当命中重复检查缓存。
	dup, err := cli.SendMessage(ctx, &pb.SendMessageRequest{
		AccountKey:  *accountKey,
		ToJid:       in.fromJID,
		ClientMsgId: clientMsgID,
		Text:        &pb.TextBody{Body: *replyText, ReplyToMessageId: in.messageID},
	})
	if err != nil {
		log.Fatalf("dedup check: %v", err)
	}
	if !dup.GetDeduped() {
		log.Fatalf("dedup check: expected deduped=true, got false")
	}
	log.Printf("DEDUP OK: replay returned cached response (deduped=true, id=%s)", dup.GetMessageId())

	log.Printf("PHASE 1 SMOKE TEST PASSED")
}

func describeEvent(ev *pb.StreamEventsResponse) {
	switch x := ev.GetEvent().(type) {
	case *pb.StreamEventsResponse_Qr:
		log.Printf("evt seq=%d account=%s QR ttl=%ds", ev.GetSeq(), ev.GetAccountKey(), x.Qr.GetTtlSeconds())
	case *pb.StreamEventsResponse_PairSuccess:
		log.Printf("evt seq=%d account=%s PAIR_SUCCESS jid=%s", ev.GetSeq(), ev.GetAccountKey(), x.PairSuccess.GetJid())
	case *pb.StreamEventsResponse_Connected:
		log.Printf("evt seq=%d account=%s CONNECTED", ev.GetSeq(), ev.GetAccountKey())
	case *pb.StreamEventsResponse_Disconnected:
		log.Printf("evt seq=%d account=%s DISCONNECTED reason=%s", ev.GetSeq(), ev.GetAccountKey(), x.Disconnected.GetReason())
	case *pb.StreamEventsResponse_LoggedOut:
		log.Printf("evt seq=%d account=%s LOGGED_OUT reason=%s", ev.GetSeq(), ev.GetAccountKey(), x.LoggedOut.GetReason())
	case *pb.StreamEventsResponse_Message:
		m := x.Message
		log.Printf("evt seq=%d account=%s MESSAGE from_me=%v group=%v chat=%s sender=%s text=%q",
			ev.GetSeq(), ev.GetAccountKey(), m.GetFromMe(), m.GetIsGroup(),
			m.GetChatJid(), m.GetSenderJid(), truncate(m.GetText(), 80))
	case *pb.StreamEventsResponse_Unknown:
		log.Printf("evt seq=%d account=%s UNKNOWN go_type=%s", ev.GetSeq(), ev.GetAccountKey(), x.Unknown.GetGoType())
	default:
		log.Printf("evt seq=%d account=%s (no payload)", ev.GetSeq(), ev.GetAccountKey())
	}
}

func renderQR(code string) {
	if code == "" {
		return
	}
	fmt.Println()
	fmt.Println("====== SCAN THIS QR WITH WHATSAPP ON YOUR TEST PHONE ======")
	// 使用 whatsmeow 默认的 Unicode 块字符 (qrterminal.BLACK / WHITE)。
	// 需要支持 UTF-8 的终端（Windows Terminal，或执行过 `chcp 65001` 并
	// 使用 TrueType 字体的 PowerShell）。QR 码较宽（约 84 列）—— 如果
	// 发生换行，请缩小终端字体或加宽窗口。
	qrterminal.GenerateWithConfig(code, qrterminal.Config{
		Level:     qrterminal.L,
		Writer:    os.Stdout,
		BlackChar: qrterminal.BLACK,
		WhiteChar: qrterminal.WHITE,
		QuietZone: 1,
	})
	fmt.Println("===========================================================")
	fmt.Println("(If the QR looks garbled, run `chcp 65001` first or use Windows Terminal.)")
}

func waitFor(ctx context.Context, label string, ch <-chan struct{}, errCh <-chan error) error {
	select {
	case <-ctx.Done():
		return fmt.Errorf("timeout waiting for %s: %w", label, ctx.Err())
	case err := <-errCh:
		return fmt.Errorf("stream error while waiting for %s: %w", label, err)
	case <-ch:
		return nil
	}
}

func selectSend(ch chan<- struct{}) {
	select {
	case ch <- struct{}{}:
	default:
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
