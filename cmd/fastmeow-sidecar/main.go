// fastmeow-sidecar 是嵌入在 FastMeow Python wheel 中的 Go 进程。
// 它托管 whatsmeow.Client 实例，并通过本地 gRPC API 向 Python SDK
// 暴露其功能（见 proto/fastmeow/v1/gateway.proto）。
//
// 监听器（Listener）选择：
//   - POSIX (linux, darwin)：通过 --listen unix:///path/to/sock 传入的
//     Unix 域套接字路径（由 Python 管理器在每次运行的临时目录中创建）。
//   - Windows：带有 --auth-token（每次 RPC 均需提供）的 TCP 回环
//     127.0.0.1:<port>；端口由管理器选择并通过 --listen tcp://127.0.0.1:0
//     传入（0 = 让操作系统挑选，sidecar 会在 stderr 上以 JSON 形式
//     {"event":"listening","addr":"..."} 打印绑定的端口）。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strings"
	"sync"
	"syscall"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
	"github.com/jianjian2048/fastmeow/internal/accounts"
	"github.com/jianjian2048/fastmeow/internal/events"
	"github.com/jianjian2048/fastmeow/internal/groups"
	"github.com/jianjian2048/fastmeow/internal/media"
	"github.com/jianjian2048/fastmeow/internal/messages"
	"github.com/jianjian2048/fastmeow/internal/presence"
	"github.com/jianjian2048/fastmeow/internal/receipts"
	"github.com/jianjian2048/fastmeow/internal/sessions"
)

// 每当传输协议（wire contract）发生破坏性变化时，协议版本（protocolVersion）就会提升。
// Python 管理器在 Ping 时发送其预期版本；我们拒绝版本不匹配的请求，以防
// 过时的 wheel 与较新的 sidecar（或反之）进行静默通信。
const protocolVersion uint32 = 2

// 编译时可注入的版本字符串。
var (
	sidecarVersion   = "0.1.0-dev"
	whatsmeowVersion = "unknown"
)

func main() {
	var (
		listenSpec = flag.String("listen", "",
			"监听器规范：unix:///path 或 tcp://127.0.0.1:port")
		sidecarID = flag.String("sidecar-id", "default",
			"sidecar 标识符；单 sidecar 模式下为 \"default\"")
		sessionDir = flag.String("session-dir", "",
			"包含 main.sqlite 的目录（如果不存在则创建）；必填")
		_ = flag.String("auth-token", "",
			"tcp 监听器所需的 Bearer 令牌；对于 unix 套接字将被忽略；保留用于下一个里程碑")
	)
	flag.Parse()

	if *listenSpec == "" {
		logJSON("fatal", map[string]any{"error": "--listen is required"})
		os.Exit(2)
	}
	if *sessionDir == "" {
		logJSON("fatal", map[string]any{"error": "--session-dir is required"})
		os.Exit(2)
	}

	logJSON("starting", map[string]any{
		"sidecar_id":        *sidecarID,
		"sidecar_version":   sidecarVersion,
		"whatsmeow_version": whatsmeowVersion,
		"protocol_version":  protocolVersion,
		"pid":               os.Getpid(),
		"session_dir":       *sessionDir,
	})

	// 按依赖顺序引导进程内各层：
	//   sessions.Store  (sqlite 容器)
	//     -> events.Bus (翻译 + 分发)
	//        -> accounts.Registry (客户端生命周期，sink 到总线)
	//           -> messages.Sender (出站 + 去重)
	//              -> gateway (gRPC 处理程序)
	rootLog := waLog.Stdout("sidecar", "INFO", true)

	dbPath := filepath.Join(*sessionDir, "main.sqlite")
	store, err := sessions.Open(context.Background(), dbPath, rootLog.Sub("store"))
	if err != nil {
		logJSON("fatal", map[string]any{"error": err.Error(), "stage": "sessions.Open"})
		os.Exit(1)
	}
	defer func() { _ = store.Close() }()

	bus := events.NewBus(*sidecarID, events.DefaultBufferSize, rootLog.Sub("bus"))
	registry := accounts.NewRegistry(store, bus.Sink, rootLog.Sub("accounts"))
	sender, err := messages.NewSender(0, 0)
	if err != nil {
		logJSON("fatal", map[string]any{"error": err.Error(), "stage": "messages.NewSender"})
		os.Exit(1)
	}
	groupsHandler := groups.NewHandler()
	receiptsHandler := receipts.NewHandler()
	presenceHandler := presence.NewHandler()
	// 媒体临时文件落到 session 根下的 _media_tmp/，与各账号 session
	// 隔离；全局并发上限 4 是 plan §2.6 的默认值，单机 100+ 账号下
	// 既能避免出口带宽抢占，又给单账号留充足吞吐。
	mediaHandler := media.NewHandler(*sessionDir, 4)

	lis, cleanup, err := newListener(*listenSpec)
	if err != nil {
		logJSON("fatal", map[string]any{"error": err.Error(), "listen": *listenSpec})
		os.Exit(1)
	}
	defer cleanup()

	logJSON("listening", map[string]any{"addr": lis.Addr().String()})

	srv := grpc.NewServer(
		// 16 MiB 远高于我们目前发送的任何单条消息负载，但
		// 为未来的媒体元数据留出了空间。实际的媒体字节将
		// 通过流式 RPC 传输，而不是通过一元（unary）RPC。
		grpc.MaxRecvMsgSize(16*1024*1024),
		grpc.MaxSendMsgSize(16*1024*1024),
	)

	gw := newGateway(*sidecarID, store, registry, bus, sender, groupsHandler, receiptsHandler, presenceHandler, mediaHandler, rootLog.Sub("gw"))
	pb.RegisterGatewayServiceServer(srv, gw)

	// 协同关闭：
	//   1. SIGINT/SIGTERM 或 Shutdown RPC -> 关闭 gw.shutdownCh。
	//   2. 停止接受新 RPC（GracefulStop 会消耗排队中的调用）。
	//   3. 断开所有 whatsmeow 客户端连接。
	//   4. 关闭总线（终止 StreamEvents）。
	//   5. 如果超出宽限时间，则强制停止。
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		select {
		case <-ctx.Done():
			logJSON("shutdown", map[string]any{"trigger": "signal"})
		case <-gw.shutdownCh:
			logJSON("shutdown", map[string]any{"trigger": "rpc"})
		}
		// 首先断开客户端连接，这样在关闭总线之后就不会
		// 再有新事件进入总线。然后 GracefulStop 会消耗正在进行的 RPC。
		registry.Shutdown()
		bus.Close()

		done := make(chan struct{})
		go func() {
			srv.GracefulStop()
			close(done)
		}()
		select {
		case <-done:
			logJSON("stopped", map[string]any{"clean": true})
		case <-time.After(10 * time.Second):
			logJSON("stopped", map[string]any{"clean": false, "reason": "grace_timeout"})
			srv.Stop()
		}
	}()

	if err := srv.Serve(lis); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
		logJSON("fatal", map[string]any{"error": err.Error()})
		os.Exit(1)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// 监听器（Listener）选择
// ─────────────────────────────────────────────────────────────────────────────

func newListener(spec string) (net.Listener, func(), error) {
	switch {
	case strings.HasPrefix(spec, "unix://"):
		path := strings.TrimPrefix(spec, "unix://")
		// 尽力而为清理先前崩溃运行遗留的过时套接字。
		// 在正常的生命周期中，Python 在回收 sidecar 时会删除临时目录。
		_ = os.Remove(path)
		lis, err := net.Listen("unix", path)
		if err != nil {
			return nil, nil, fmt.Errorf("unix listen %q: %w", path, err)
		}
		return lis, func() { _ = os.Remove(path) }, nil

	case strings.HasPrefix(spec, "tcp://"):
		addr := strings.TrimPrefix(spec, "tcp://")
		lis, err := net.Listen("tcp", addr)
		if err != nil {
			return nil, nil, fmt.Errorf("tcp listen %q: %w", addr, err)
		}
		return lis, func() {}, nil

	default:
		return nil, nil, fmt.Errorf("unsupported listen spec %q (want unix:// or tcp://)", spec)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Gateway
// ─────────────────────────────────────────────────────────────────────────────

type gateway struct {
	pb.UnimplementedGatewayServiceServer

	sidecarID  string
	store      *sessions.Store
	registry   *accounts.Registry
	bus        *events.Bus
	sender     *messages.Sender
	groups     *groups.Handler
	receipts   *receipts.Handler
	presence   *presence.Handler
	media      *media.Handler
	log        waLog.Logger
	shutdownCh chan struct{}
	shutdownMu sync.Mutex
}

func newGateway(
	sidecarID string,
	store *sessions.Store,
	registry *accounts.Registry,
	bus *events.Bus,
	sender *messages.Sender,
	groupsHandler *groups.Handler,
	receiptsHandler *receipts.Handler,
	presenceHandler *presence.Handler,
	mediaHandler *media.Handler,
	log waLog.Logger,
) *gateway {
	return &gateway{
		sidecarID:  sidecarID,
		store:      store,
		registry:   registry,
		bus:        bus,
		sender:     sender,
		groups:     groupsHandler,
		receipts:   receiptsHandler,
		presence:   presenceHandler,
		media:      mediaHandler,
		log:        log,
		shutdownCh: make(chan struct{}),
	}
}

// ── Ping ─────────────────────────────────────────────────────────────────────

func (g *gateway) Ping(_ context.Context, req *pb.PingRequest) (*pb.PingResponse, error) {
	defer recoverPanic("Ping")

	if req.GetClientProtocolVersion() != protocolVersion {
		return nil, status.Errorf(codes.FailedPrecondition,
			"protocol version mismatch: client=%d server=%d (rebuild the wheel or upgrade fastmeow)",
			req.GetClientProtocolVersion(), protocolVersion)
	}
	return &pb.PingResponse{
		ServerProtocolVersion: protocolVersion,
		SidecarVersion:        sidecarVersion,
		WhatsmeowVersion:      whatsmeowVersion,
		SidecarId:             g.sidecarID,
	}, nil
}

// ── EnsureAccount ────────────────────────────────────────────────────────────

func (g *gateway) EnsureAccount(ctx context.Context, req *pb.EnsureAccountRequest) (*pb.EnsureAccountResponse, error) {
	defer recoverPanic("EnsureAccount")

	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}

	var jid types.JID
	if raw := req.GetJid(); raw != "" {
		parsed, err := types.ParseJID(raw)
		if err != nil {
			return nil, status.Errorf(codes.InvalidArgument, "invalid jid %q: %v", raw, err)
		}
		jid = parsed
	}

	// 通过此调用之前该账号是否已在注册表中来检测 "created"。
	// 我们在 EnsureAccount 之前进行检查，这样并发的调用方
	// 竞速时不会都报告 created=true。
	_, getErr := g.registry.Get(key)
	wasMissing := errors.Is(getErr, accounts.ErrNotFound)

	acct, err := g.registry.EnsureAccount(ctx, key, jid)
	if err != nil {
		switch {
		case errors.Is(err, accounts.ErrAlreadyExists):
			return nil, status.Errorf(codes.FailedPrecondition, "%v", err)
		default:
			// sessions.Store 缺失设备的情况是普通的 fmt.Errorf；
			// 表现为 FailedPrecondition（清单/数据库发生偏差）。
			if strings.Contains(err.Error(), "manifest/db drift") {
				return nil, status.Errorf(codes.FailedPrecondition, "%v", err)
			}
			return nil, status.Errorf(codes.Internal, "ensure: %v", err)
		}
	}

	// 预置总线的 JID 缓存，以便在第一次 PairSuccess 之前发出的事件
	// （例如已配对的恢复流程中的 Connected 事件）携带 account_jid。
	if !acct.JID.IsEmpty() {
		g.bus.SetAccountJID(key, acct.JID)
	}

	return &pb.EnsureAccountResponse{
		State:   accountState(acct, computeState(acct)),
		Created: wasMissing,
	}, nil
}

// ── Connect ──────────────────────────────────────────────────────────────────

func (g *gateway) Connect(ctx context.Context, req *pb.ConnectRequest) (*pb.ConnectResponse, error) {
	defer recoverPanic("Connect")

	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}

	acct, err := g.registry.Get(key)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "account %q not registered (call EnsureAccount first)", key)
	}

	cli := acct.Client
	if cli.IsConnected() {
		return &pb.ConnectResponse{State: accountState(acct, pb.AccountState_STATE_CONNECTED)}, nil
	}

	// 根据配对状态有两种流程：
	//   - 未配对 (Store.ID == nil)：在 Connect 之前调用 GetQRChannel。
	//     whatsmeow 的配对协议要求在 WebSocket 握手完成时
	//     打开通道，以便发出 QR 码。我们在后台 goroutine 中
	//     将通道接入到总线。
	//   - 已配对：直接 Connect。无 QR 通道。
	if cli.Store.ID == nil {
		qrCh, qerr := cli.GetQRChannel(context.Background())
		if qerr != nil && !errors.Is(qerr, whatsmeow.ErrQRStoreContainsID) {
			return nil, status.Errorf(codes.Internal, "GetQRChannel: %v", qerr)
		}
		if qrCh != nil {
			go g.bus.PumpQRChannel(context.Background(), key, qrCh)
		}
	}

	if err := cli.Connect(); err != nil {
		return nil, status.Errorf(codes.Unavailable, "connect: %v", err)
	}

	return &pb.ConnectResponse{State: accountState(acct, pb.AccountState_STATE_CONNECTING)}, nil
}

// ── Disconnect ───────────────────────────────────────────────────────────────

func (g *gateway) Disconnect(_ context.Context, req *pb.DisconnectRequest) (*pb.DisconnectResponse, error) {
	defer recoverPanic("Disconnect")

	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}
	if err := g.registry.Disconnect(key); err != nil {
		if errors.Is(err, accounts.ErrNotFound) {
			return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
		}
		return nil, status.Errorf(codes.Internal, "disconnect: %v", err)
	}
	acct, _ := g.registry.Get(key)
	return &pb.DisconnectResponse{State: accountState(acct, pb.AccountState_STATE_DISCONNECTED)}, nil
}

// ── Logout ───────────────────────────────────────────────────────────────────

func (g *gateway) Logout(ctx context.Context, req *pb.LogoutRequest) (*pb.LogoutResponse, error) {
	defer recoverPanic("Logout")

	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}
	if err := g.registry.Remove(ctx, key); err != nil {
		if errors.Is(err, accounts.ErrNotFound) {
			return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
		}
		return nil, status.Errorf(codes.Internal, "logout: %v", err)
	}
	return &pb.LogoutResponse{
		State: &pb.AccountState{
			AccountKey: key,
			State:      pb.AccountState_STATE_LOGGED_OUT,
		},
	}, nil
}

// ── SendMessage ──────────────────────────────────────────────────────────────

func (g *gateway) SendMessage(ctx context.Context, req *pb.SendMessageRequest) (*pb.SendMessageResponse, error) {
	defer recoverPanic("SendMessage")

	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}

	acct, err := g.registry.Get(key)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
	}

	text := req.GetText()
	if text == nil {
		return nil, status.Error(codes.InvalidArgument, "text body is required (Phase 1: text only)")
	}

	resp, err := g.sender.SendText(
		ctx,
		acct.Client,
		key,
		req.GetToJid(),
		req.GetClientMsgId(),
		text.GetBody(),
		text.GetReplyToMessageId(),
	)
	if err != nil {
		switch {
		case errors.Is(err, messages.ErrEmptyClientMsgID),
			errors.Is(err, messages.ErrEmptyToJID),
			errors.Is(err, messages.ErrEmptyBody),
			errors.Is(err, messages.ErrInvalidJID):
			return nil, status.Errorf(codes.InvalidArgument, "%v", err)
		default:
			return nil, status.Errorf(codes.Internal, "send: %v", err)
		}
	}

	// SendText 可能会丢失时间戳（whatsmeow 在某些路径下返回零值）；
	// 确保非空，以免下游 proto 使用者触发空指针。
	if resp.ServerTimestamp == nil {
		resp.ServerTimestamp = timestamppb.Now()
	}
	return resp, nil
}

// ── StreamEvents ─────────────────────────────────────────────────────────────

func (g *gateway) StreamEvents(req *pb.StreamEventsRequest, stream pb.GatewayService_StreamEventsServer) error {
	defer recoverPanic("StreamEvents")

	// 总线强制执行单一订阅者；第二次并发的 StreamEvents
	// 将引发 panic 并由上方的代码通过结构化日志进行恢复。
	ch := g.bus.Subscribe()
	ctx := stream.Context()

	// Phase 4.2：软状态事件（Receipt / Presence / ChatPresence）默认关闭，
	// 仅在调用方显式请求 include_soft_events=true 时透传。Bus 始终翻译它们，
	// 过滤在此处发生 —— 让 Bus 保持纯粹、把订阅者能力声明隔离在边缘。
	includeSoft := req.GetIncludeSoftEvents()

	for {
		select {
		case <-ctx.Done():
			return nil
		case evt, ok := <-ch:
			if !ok {
				// 总线在关闭期间关闭；清洁的 EOF。
				return nil
			}
			if !includeSoft && isSoftEvent(evt) {
				continue
			}
			if err := stream.Send(evt); err != nil {
				return err
			}
		}
	}
}

// isSoftEvent 判断 wire 事件是否属于「软状态」类别：
// Receipt / Presence / ChatPresence。这些事件高频且对多数业务无关，
// 因此 Phase 4.2 决策为默认不递送，需调用方显式 opt-in。
func isSoftEvent(evt *pb.StreamEventsResponse) bool {
	switch evt.GetEvent().(type) {
	case *pb.StreamEventsResponse_Receipt,
		*pb.StreamEventsResponse_Presence,
		*pb.StreamEventsResponse_ChatPresence:
		return true
	default:
		return false
	}
}

// ── Shutdown ─────────────────────────────────────────────────────────────────

func (g *gateway) Shutdown(_ context.Context, _ *pb.ShutdownRequest) (*pb.ShutdownResponse, error) {
	defer recoverPanic("Shutdown")

	g.shutdownMu.Lock()
	defer g.shutdownMu.Unlock()
	select {
	case <-g.shutdownCh:
		// 已关闭；幂等。
	default:
		close(g.shutdownCh)
	}
	return &pb.ShutdownResponse{}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// 群组 RPC（Phase 4.1）
// ─────────────────────────────────────────────────────────────────────────────
//
// 9 个群组 RPC 都共享相同的两步骤前缀：解析 account_key、从注册表取出
// *whatsmeow.Client；之后委派给 internal/groups.Handler。错误统一通过
// groupErrToStatus 映射到 gRPC code，避免每个 RPC 重复 switch。
//
// 注：whatsmeow.ErrGroupNotFound / ErrNotInGroup / ErrGroupInviteLinkUnauthorized
// 等 sentinel 在 Phase 4.1 决策中被显式映射；具体值见
// PHASE_4_PLAN 与 proto 注释。

func (g *gateway) clientFor(key string) (*whatsmeow.Client, error) {
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}
	acct, err := g.registry.Get(key)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
	}
	return acct.Client, nil
}

func (g *gateway) ListJoinedGroups(ctx context.Context, req *pb.ListJoinedGroupsRequest) (*pb.ListJoinedGroupsResponse, error) {
	defer recoverPanic("ListJoinedGroups")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.ListJoinedGroups(ctx, cli)
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) GetGroupInfo(ctx context.Context, req *pb.GetGroupInfoRequest) (*pb.GetGroupInfoResponse, error) {
	defer recoverPanic("GetGroupInfo")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.GetGroupInfo(ctx, cli, req.GetGroupJid())
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) PreviewGroupInvite(ctx context.Context, req *pb.PreviewGroupInviteRequest) (*pb.PreviewGroupInviteResponse, error) {
	defer recoverPanic("PreviewGroupInvite")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.PreviewGroupInvite(ctx, cli, req.GetInviteLink())
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) JoinGroupViaInvite(ctx context.Context, req *pb.JoinGroupViaInviteRequest) (*pb.JoinGroupViaInviteResponse, error) {
	defer recoverPanic("JoinGroupViaInvite")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.JoinGroupViaInvite(ctx, cli, req.GetInviteLink())
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) LeaveGroup(ctx context.Context, req *pb.LeaveGroupRequest) (*pb.LeaveGroupResponse, error) {
	defer recoverPanic("LeaveGroup")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.LeaveGroup(ctx, cli, req.GetGroupJid())
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) CreateGroup(ctx context.Context, req *pb.CreateGroupRequest) (*pb.CreateGroupResponse, error) {
	defer recoverPanic("CreateGroup")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.CreateGroup(ctx, cli, req.GetName(), req.GetParticipantJids())
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) UpdateGroupSettings(ctx context.Context, req *pb.UpdateGroupSettingsRequest) (*pb.UpdateGroupSettingsResponse, error) {
	defer recoverPanic("UpdateGroupSettings")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.UpdateGroupSettings(ctx, cli, req)
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) UpdateGroupParticipants(ctx context.Context, req *pb.UpdateGroupParticipantsRequest) (*pb.UpdateGroupParticipantsResponse, error) {
	defer recoverPanic("UpdateGroupParticipants")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.UpdateGroupParticipants(ctx, cli, req)
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) GetGroupInviteLink(ctx context.Context, req *pb.GetGroupInviteLinkRequest) (*pb.GetGroupInviteLinkResponse, error) {
	defer recoverPanic("GetGroupInviteLink")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.groups.GetGroupInviteLink(ctx, cli, req.GetGroupJid(), req.GetReset_())
	if err != nil {
		return nil, groupErrToStatus(err)
	}
	return resp, nil
}

// groupErrToStatus 把 internal/groups 与 whatsmeow 的错误映射到 gRPC status。
//
// 映射规则（与 PHASE_4_PLAN 群组错误决策保持一致）：
//   - groups.ErrInvalidJID / ErrEmptyInviteCode / ErrEmptyGroupName /
//     ErrNoParticipants / ErrInvalidParticipantAction
//                                                  → INVALID_ARGUMENT
//   - whatsmeow.ErrGroupNotFound                   → NOT_FOUND
//   - whatsmeow.ErrNotInGroup /
//     whatsmeow.ErrGroupInviteLinkUnauthorized     → PERMISSION_DENIED
//   - whatsmeow.ErrInviteLinkInvalid               → INVALID_ARGUMENT
//   - whatsmeow.ErrInviteLinkRevoked               → NOT_FOUND
//   - 其他                                          → UNAVAILABLE（多数为
//     网络 / 服务端临时错误；客户端可安全重试）
func groupErrToStatus(err error) error {
	switch {
	case errors.Is(err, groups.ErrInvalidJID),
		errors.Is(err, groups.ErrEmptyInviteCode),
		errors.Is(err, groups.ErrEmptyGroupName),
		errors.Is(err, groups.ErrNoParticipants),
		errors.Is(err, groups.ErrInvalidParticipantAction),
		errors.Is(err, whatsmeow.ErrInviteLinkInvalid):
		return status.Errorf(codes.InvalidArgument, "%v", err)
	case errors.Is(err, whatsmeow.ErrGroupNotFound),
		errors.Is(err, whatsmeow.ErrInviteLinkRevoked):
		return status.Errorf(codes.NotFound, "%v", err)
	case errors.Is(err, whatsmeow.ErrNotInGroup),
		errors.Is(err, whatsmeow.ErrGroupInviteLinkUnauthorized):
		return status.Errorf(codes.PermissionDenied, "%v", err)
	default:
		return status.Errorf(codes.Unavailable, "%v", err)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// 回执 / 在线状态 RPC（Phase 4.2）
// ─────────────────────────────────────────────────────────────────────────────
//
// 4 个 RPC 共享 clientFor 的两步骤前缀（解析 account_key + 取 client），
// 之后委派给 internal/receipts.Handler 或 internal/presence.Handler。
// 错误统一通过 receiptErrToStatus / presenceErrToStatus 映射，
// 与群组 RPC 同模式。

func (g *gateway) MarkRead(ctx context.Context, req *pb.MarkReadRequest) (*pb.MarkReadResponse, error) {
	defer recoverPanic("MarkRead")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.receipts.MarkRead(ctx, cli, req)
	if err != nil {
		return nil, receiptErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) SendPresence(ctx context.Context, req *pb.SendPresenceRequest) (*pb.SendPresenceResponse, error) {
	defer recoverPanic("SendPresence")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.presence.SendPresence(ctx, cli, req)
	if err != nil {
		return nil, presenceErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) SendChatPresence(ctx context.Context, req *pb.SendChatPresenceRequest) (*pb.SendChatPresenceResponse, error) {
	defer recoverPanic("SendChatPresence")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.presence.SendChatPresence(ctx, cli, req)
	if err != nil {
		return nil, presenceErrToStatus(err)
	}
	return resp, nil
}

func (g *gateway) SubscribePresence(ctx context.Context, req *pb.SubscribePresenceRequest) (*pb.SubscribePresenceResponse, error) {
	defer recoverPanic("SubscribePresence")
	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return nil, err
	}
	resp, err := g.presence.SubscribePresence(ctx, cli, req)
	if err != nil {
		return nil, presenceErrToStatus(err)
	}
	return resp, nil
}

// receiptErrToStatus 把 internal/receipts 的 sentinel + whatsmeow 错误
// 映射到 gRPC code。
//
// 映射规则：
//   - receipts.ErrInvalidJID / ErrEmptyMessageIDs / ErrEmptyMessageID
//                                                   → INVALID_ARGUMENT
//   - 其他                                            → UNAVAILABLE（多数为
//     网络 / WebSocket 临时错误；客户端可安全重试）
func receiptErrToStatus(err error) error {
	switch {
	case errors.Is(err, receipts.ErrInvalidJID),
		errors.Is(err, receipts.ErrEmptyMessageIDs),
		errors.Is(err, receipts.ErrEmptyMessageID):
		return status.Errorf(codes.InvalidArgument, "%v", err)
	default:
		return status.Errorf(codes.Unavailable, "%v", err)
	}
}

// presenceErrToStatus 把 internal/presence 的 sentinel + whatsmeow 错误
// 映射到 gRPC code。
//
// 映射规则：
//   - presence.ErrInvalidJID / ErrInvalidPresenceType /
//     ErrInvalidChatPresenceState                    → INVALID_ARGUMENT
//   - 其他                                            → UNAVAILABLE
func presenceErrToStatus(err error) error {
	switch {
	case errors.Is(err, presence.ErrInvalidJID),
		errors.Is(err, presence.ErrInvalidPresenceType),
		errors.Is(err, presence.ErrInvalidChatPresenceState):
		return status.Errorf(codes.InvalidArgument, "%v", err)
	default:
		return status.Errorf(codes.Unavailable, "%v", err)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// 媒体 RPC（Phase 4.3）
// ─────────────────────────────────────────────────────────────────────────────
//
// SendMedia (client streaming):
//   1. 第 1 帧必须是 SendMediaInit；解析 account_key 拿 client。
//   2. 启动一条 io.Pipe；后续 chunk 帧写入 pipe.Writer，media.Send 在另一
//      goroutine 上 io.Copy 读取并 UploadReader。流终止 / 错误时 pipe 必关。
//   3. 任何陈旧 init 帧（第 2+ 帧再发 init）视为协议违规 InvalidArgument。
//
// DownloadMedia (server streaming):
//   1. 解析 account_key + 取 client。
//   2. 调 media.Download，由 sidecar 把分片写入临时文件后逐块回传；
//      chunkWriter 内部直接 stream.Send(&pb.DownloadMediaChunk{Chunk: ...})。
//
// 错误统一通过 mediaErrToStatus 映射；upload/download 共用同一表。

func (g *gateway) SendMedia(stream pb.GatewayService_SendMediaServer) error {
	defer recoverPanic("SendMedia")

	// 第 1 帧必须是 init。
	first, err := stream.Recv()
	if err != nil {
		return status.Errorf(codes.InvalidArgument, "media: failed to read first frame: %v", err)
	}
	init := first.GetInit()
	if init == nil {
		return status.Error(codes.InvalidArgument, "media: first frame must be init")
	}

	cli, err := g.clientFor(init.GetAccountKey())
	if err != nil {
		return err
	}

	// io.Pipe 把 stream.Recv 的 chunk 串起来喂给 media.Send。
	// 关闭顺序：reader-goroutine 跑完时 CloseWithError(nil) -> writer 收 EOF。
	// 任何 stream.Recv 错误 -> CloseWithError(err) -> media.Send io.Copy 立即停。
	pr, pw := io.Pipe()

	// reader goroutine: 把后续 chunk 帧泵入 pipe writer。
	go func() {
		for {
			frame, recvErr := stream.Recv()
			if recvErr == io.EOF {
				_ = pw.Close()
				return
			}
			if recvErr != nil {
				_ = pw.CloseWithError(recvErr)
				return
			}
			// 流的中段再发 init 是协议违规；用 InvalidArgument 关 pipe。
			if frame.GetInit() != nil {
				_ = pw.CloseWithError(status.Error(codes.InvalidArgument,
					"media: unexpected init frame mid-stream"))
				return
			}
			chunk := frame.GetChunk()
			if len(chunk) == 0 {
				continue // 空 chunk 视为 keep-alive；忽略。
			}
			if _, werr := pw.Write(chunk); werr != nil {
				// pipe 已被 reader（media.Send）关闭，停止泵入。
				return
			}
		}
	}()

	resp, err := g.media.Send(stream.Context(), cli, init, pr)
	// media.Send 不关 reader；这里兜底关一次让 reader-goroutine 退出。
	_ = pr.Close()
	if err != nil {
		return mediaErrToStatus(err, true)
	}
	return stream.SendAndClose(resp)
}

func (g *gateway) DownloadMedia(req *pb.DownloadMediaRequest, stream pb.GatewayService_DownloadMediaServer) error {
	defer recoverPanic("DownloadMedia")

	cli, err := g.clientFor(req.GetAccountKey())
	if err != nil {
		return err
	}

	chunkWriter := func(p []byte) error {
		// media.Download 已经在 chunkWriter 调用前 copy 出独立 buffer，
		// 这里直接把 slice 塞进 pb 即可，gRPC 序列化期间复用安全。
		return stream.Send(&pb.DownloadMediaChunk{Chunk: p})
	}

	if err := g.media.Download(stream.Context(), cli, req.GetMedia(), chunkWriter); err != nil {
		return mediaErrToStatus(err, false)
	}
	return nil
}

// mediaErrToStatus 把 internal/media + whatsmeow 媒体错误映射到 gRPC code。
//
// upload=true 表示来自 SendMedia 路径；upload=false 表示 DownloadMedia。
// 两条路径共用大部分 sentinel，只有少数（如 ErrMissingDownloadInfo）只下行有意义。
//
// 映射规则：
//   - 客户端入参错（init 缺/空字段/JID/kind/超限）         → INVALID_ARGUMENT
//   - 客户端声明长度与实际收到的 chunk 总和不一致           → FAILED_PRECONDITION
//     （区别于纯入参错；客户端可重发完整文件而不必改入参）
//   - 服务端文件过大 / 资源不足                             → RESOURCE_EXHAUSTED
//   - whatsmeow 校验失败（sha256 / hmac / enc sha256 / 截断） → DATA_LOSS
//     （服务端拿到的字节与服务器声明的密钥/摘要不符；不可重试）
//   - 其他（多为网络 / WebSocket 临时错误）                 → UNAVAILABLE
func mediaErrToStatus(err error, upload bool) error {
	_ = upload // 当前两路映射相同；保留参数留作未来分流。
	switch {
	case errors.Is(err, media.ErrMissingInitFrame),
		errors.Is(err, media.ErrUnexpectedInitFrame),
		errors.Is(err, media.ErrEmptyClientMsgID),
		errors.Is(err, media.ErrEmptyToJID),
		errors.Is(err, media.ErrInvalidJID),
		errors.Is(err, media.ErrUnsupportedMediaKind),
		errors.Is(err, media.ErrMissingMimeType),
		errors.Is(err, media.ErrMissingDownloadInfo):
		return status.Errorf(codes.InvalidArgument, "%v", err)
	case errors.Is(err, media.ErrDeclaredLengthMismatch):
		return status.Errorf(codes.FailedPrecondition, "%v", err)
	case errors.Is(err, media.ErrMediaTooLarge):
		return status.Errorf(codes.ResourceExhausted, "%v", err)
	case errors.Is(err, whatsmeow.ErrFileLengthMismatch),
		errors.Is(err, whatsmeow.ErrInvalidMediaSHA256),
		errors.Is(err, whatsmeow.ErrInvalidMediaEncSHA256),
		errors.Is(err, whatsmeow.ErrInvalidMediaHMAC),
		errors.Is(err, whatsmeow.ErrTooShortFile):
		return status.Errorf(codes.DataLoss, "%v", err)
	case errors.Is(err, whatsmeow.ErrUnknownMediaType),
		errors.Is(err, whatsmeow.ErrNoURLPresent):
		return status.Errorf(codes.InvalidArgument, "%v", err)
	default:
		return status.Errorf(codes.Unavailable, "%v", err)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// 辅助函数
// ─────────────────────────────────────────────────────────────────────────────

// accountState 从 accounts.Account 视图加上调用方提供的状态枚举
// 构建一个 AccountState proto（注册表在第一阶段不跟踪生命周期状态，
// 因此调用方根据其刚执行的操作来推断它）。
func accountState(acct *accounts.Account, state pb.AccountState_State) *pb.AccountState {
	if acct == nil {
		return &pb.AccountState{State: state}
	}
	out := &pb.AccountState{
		AccountKey: acct.Key,
		State:      state,
	}
	if !acct.JID.IsEmpty() {
		out.Jid = acct.JID.String()
	}
	return out
}

// computeState 将当前客户端状态映射到 AccountState 枚举，无需
// 任何注册表级别的状态机。由 EnsureAccount 使用，在任何显式
// Connect 之前报告一个合理的值。
func computeState(acct *accounts.Account) pb.AccountState_State {
	if acct == nil || acct.Client == nil {
		return pb.AccountState_STATE_UNSPECIFIED
	}
	if acct.JID.IsEmpty() {
		return pb.AccountState_STATE_UNPAIRED
	}
	if acct.Client.IsConnected() {
		return pb.AccountState_STATE_CONNECTED
	}
	return pb.AccountState_STATE_DISCONNECTED
}

// ─────────────────────────────────────────────────────────────────────────────
// 日志记录与 panic 安全
// ─────────────────────────────────────────────────────────────────────────────

// logJSON 将每行一个结构化事件写入 stderr。Python 管理器
// 读取 stderr 并转发给宿主日志记录器。
func logJSON(event string, fields map[string]any) {
	if fields == nil {
		fields = map[string]any{}
	}
	fields["event"] = event
	fields["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	b, err := json.Marshal(fields)
	if err != nil {
		// 回退到普通行，以免失去可见性。
		fmt.Fprintf(os.Stderr, `{"event":"log_marshal_error","error":%q}`+"\n", err.Error())
		return
	}
	fmt.Fprintln(os.Stderr, string(b))
}

// recoverPanic 将 RPC 处理程序中的任何 panic 转换为结构化日志行，
// 这样单个账号的异常行为就不会导致整个 sidecar 崩溃。
// 单 sidecar 模式意味着 panic 的波及范围是所有账号 ——
// 在实现每个账号的隔离之前，这是我们拥有的成本最低的缓解措施。
func recoverPanic(rpc string) {
	if r := recover(); r != nil {
		logJSON("panic", map[string]any{
			"rpc":   rpc,
			"value": fmt.Sprintf("%v", r),
			"stack": string(debug.Stack()),
		})
	}
}

func (g *gateway) SendReaction(ctx context.Context, req *pb.SendReactionRequest) (*pb.SendReactionResponse, error) {
	defer recoverPanic("SendReaction")
	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}
	acct, err := g.registry.Get(key)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
	}

	resp, err := g.sender.SendReaction(
		ctx,
		acct.Client,
		key,
		req.GetChatJid(),
		req.GetTargetMessageId(),
		req.GetTargetSenderJid(),
		req.GetEmoji(),
	)
	if err != nil {
		if errors.Is(err, messages.ErrEmptyToJID) || strings.Contains(err.Error(), "required") {
			return nil, status.Errorf(codes.InvalidArgument, "%v", err)
		}
		if errors.Is(err, messages.ErrInvalidJID) {
			return nil, status.Errorf(codes.InvalidArgument, "invalid to_jid: %v", err)
		}
		return nil, status.Errorf(codes.Internal, "whatsmeow error: %v", err)
	}
	if resp.ServerTimestamp == nil {
		resp.ServerTimestamp = timestamppb.Now()
	}
	return resp, nil
}

func (g *gateway) SendEdit(ctx context.Context, req *pb.SendEditRequest) (*pb.SendEditResponse, error) {
	defer recoverPanic("SendEdit")
	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}
	acct, err := g.registry.Get(key)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
	}

	resp, err := g.sender.SendEdit(
		ctx,
		acct.Client,
		key,
		req.GetChatJid(),
		req.GetTargetMessageId(),
		req.GetNewText(),
	)
	if err != nil {
		if errors.Is(err, messages.ErrEmptyToJID) || errors.Is(err, messages.ErrEmptyBody) || strings.Contains(err.Error(), "required") {
			return nil, status.Errorf(codes.InvalidArgument, "%v", err)
		}
		if errors.Is(err, messages.ErrInvalidJID) {
			return nil, status.Errorf(codes.InvalidArgument, "invalid chat_jid: %v", err)
		}
		return nil, status.Errorf(codes.Internal, "whatsmeow error: %v", err)
	}
	if resp.ServerTimestamp == nil {
		resp.ServerTimestamp = timestamppb.Now()
	}
	return resp, nil
}

func (g *gateway) SendRevoke(ctx context.Context, req *pb.SendRevokeRequest) (*pb.SendRevokeResponse, error) {
	defer recoverPanic("SendRevoke")
	key := req.GetAccountKey()
	if key == "" {
		return nil, status.Error(codes.InvalidArgument, "account_key is required")
	}
	acct, err := g.registry.Get(key)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "account %q not registered", key)
	}

	resp, err := g.sender.SendRevoke(
		ctx,
		acct.Client,
		key,
		req.GetChatJid(),
		req.GetTargetMessageId(),
		req.GetTargetSenderJid(),
	)
	if err != nil {
		if errors.Is(err, messages.ErrEmptyToJID) || strings.Contains(err.Error(), "required") {
			return nil, status.Errorf(codes.InvalidArgument, "%v", err)
		}
		if errors.Is(err, messages.ErrInvalidJID) {
			return nil, status.Errorf(codes.InvalidArgument, "invalid chat_jid: %v", err)
		}
		return nil, status.Errorf(codes.Internal, "whatsmeow error: %v", err)
	}
	if resp.ServerTimestamp == nil {
		resp.ServerTimestamp = timestamppb.Now()
	}
	return resp, nil
}
