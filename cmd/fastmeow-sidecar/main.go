// fastmeow-sidecar is the Go process embedded inside the FastMeow Python
// wheel. It hosts whatsmeow.Client instances and exposes them to the Python
// SDK over a local gRPC API (see proto/fastmeow/v1/gateway.proto).
//
// Listener selection:
//   - POSIX (linux, darwin): Unix domain socket at the path passed via
//     --listen unix:///path/to/sock (created by Python supervisor in a
//     per-run temp dir).
//   - Windows: TCP loopback 127.0.0.1:<port> with --auth-token required on
//     every RPC; port chosen by the supervisor and passed via --listen
//     tcp://127.0.0.1:0 (0 = let the OS pick, sidecar prints the bound port
//     on stderr as JSON {"event":"listening","addr":"..."}).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
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
	"github.com/jianjian2048/fastmeow/internal/messages"
	"github.com/jianjian2048/fastmeow/internal/sessions"
)

// Protocol version is bumped whenever the wire contract changes in a
// breaking way. Python supervisor sends its expected version on Ping; we
// reject mismatches so a stale wheel never talks to a newer sidecar (or
// vice versa) silently.
const protocolVersion uint32 = 1

// Build-time injectable version strings.
var (
	sidecarVersion   = "0.1.0-dev"
	whatsmeowVersion = "unknown"
)

func main() {
	var (
		listenSpec = flag.String("listen", "",
			"listener spec: unix:///path or tcp://127.0.0.1:port")
		sidecarID = flag.String("sidecar-id", "default",
			"sidecar identifier; \"default\" in single-sidecar mode")
		sessionDir = flag.String("session-dir", "",
			"directory containing main.sqlite (created if missing); required")
		_ = flag.String("auth-token", "",
			"required bearer token for tcp listeners; ignored for unix sockets; reserved for next milestone")
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

	// Bootstrap the in-process layers in dependency order:
	//   sessions.Store  (sqlite container)
	//     -> events.Bus (translation + fan-out)
	//        -> accounts.Registry (client lifecycle, sink to bus)
	//           -> messages.Sender (outbound + dedup)
	//              -> gateway (gRPC handlers)
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

	lis, cleanup, err := newListener(*listenSpec)
	if err != nil {
		logJSON("fatal", map[string]any{"error": err.Error(), "listen": *listenSpec})
		os.Exit(1)
	}
	defer cleanup()

	logJSON("listening", map[string]any{"addr": lis.Addr().String()})

	srv := grpc.NewServer(
		// 16 MiB is well above any single-message payload we send today, but
		// leaves headroom for future media metadata. Real media bytes will
		// move via a streaming RPC, not unary.
		grpc.MaxRecvMsgSize(16*1024*1024),
		grpc.MaxSendMsgSize(16*1024*1024),
	)

	gw := newGateway(*sidecarID, store, registry, bus, sender, rootLog.Sub("gw"))
	pb.RegisterGatewayServiceServer(srv, gw)

	// Coordinated shutdown:
	//   1. SIGINT/SIGTERM or Shutdown RPC -> close gw.shutdownCh.
	//   2. Stop accepting new RPCs (GracefulStop drains in-flight calls).
	//   3. Disconnect all whatsmeow clients.
	//   4. Close the bus (terminates StreamEvents).
	//   5. If grace exceeded, hard-stop.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		select {
		case <-ctx.Done():
			logJSON("shutdown", map[string]any{"trigger": "signal"})
		case <-gw.shutdownCh:
			logJSON("shutdown", map[string]any{"trigger": "rpc"})
		}
		// Disconnect clients first so no new events land on the bus
		// after we close it. Then GracefulStop drains in-flight RPCs.
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
// Listener selection
// ─────────────────────────────────────────────────────────────────────────────

func newListener(spec string) (net.Listener, func(), error) {
	switch {
	case strings.HasPrefix(spec, "unix://"):
		path := strings.TrimPrefix(spec, "unix://")
		// Best-effort cleanup of a stale socket from a prior crashed run.
		// In a healthy lifecycle Python deletes the temp dir when it
		// reaps the sidecar.
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
	log waLog.Logger,
) *gateway {
	return &gateway{
		sidecarID:  sidecarID,
		store:      store,
		registry:   registry,
		bus:        bus,
		sender:     sender,
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

	// Detect "created" by whether the account was already in the registry
	// before this call. We check before EnsureAccount so a concurrent
	// caller racing us doesn't both report created=true.
	_, getErr := g.registry.Get(key)
	wasMissing := errors.Is(getErr, accounts.ErrNotFound)

	acct, err := g.registry.EnsureAccount(ctx, key, jid)
	if err != nil {
		switch {
		case errors.Is(err, accounts.ErrAlreadyExists):
			return nil, status.Errorf(codes.FailedPrecondition, "%v", err)
		default:
			// sessions.Store missing-device case is plain fmt.Errorf;
			// surface as FailedPrecondition (manifest/db drift).
			if strings.Contains(err.Error(), "manifest/db drift") {
				return nil, status.Errorf(codes.FailedPrecondition, "%v", err)
			}
			return nil, status.Errorf(codes.Internal, "ensure: %v", err)
		}
	}

	// Seed the bus's JID cache so events emitted before the first
	// PairSuccess (e.g. Connected for an already-paired resume flow)
	// carry account_jid.
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

	// Two flows depending on pairing state:
	//   - Unpaired (Store.ID == nil): GetQRChannel BEFORE Connect.
	//     whatsmeow's pairing protocol requires the channel to be open
	//     when the WebSocket handshake completes so the QR codes can be
	//     emitted. We pump the channel onto the bus in a background
	//     goroutine.
	//   - Paired: just Connect. No QR channel.
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

	// SendText may have lost the timestamp (whatsmeow returns zero on
	// some paths); ensure non-nil so downstream proto users don't NPE.
	if resp.ServerTimestamp == nil {
		resp.ServerTimestamp = timestamppb.Now()
	}
	return resp, nil
}

// ── StreamEvents ─────────────────────────────────────────────────────────────

func (g *gateway) StreamEvents(req *pb.StreamEventsRequest, stream pb.GatewayService_StreamEventsServer) error {
	defer recoverPanic("StreamEvents")

	// Bus enforces single subscriber; a second concurrent StreamEvents
	// will panic and be recovered above with a structured log line.
	ch := g.bus.Subscribe()
	ctx := stream.Context()

	for {
		select {
		case <-ctx.Done():
			return nil
		case evt, ok := <-ch:
			if !ok {
				// Bus closed during shutdown; clean EOF.
				return nil
			}
			if err := stream.Send(evt); err != nil {
				return err
			}
		}
	}
}

// ── Shutdown ─────────────────────────────────────────────────────────────────

func (g *gateway) Shutdown(_ context.Context, _ *pb.ShutdownRequest) (*pb.ShutdownResponse, error) {
	defer recoverPanic("Shutdown")

	g.shutdownMu.Lock()
	defer g.shutdownMu.Unlock()
	select {
	case <-g.shutdownCh:
		// Already closed; idempotent.
	default:
		close(g.shutdownCh)
	}
	return &pb.ShutdownResponse{}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

// accountState builds an AccountState proto from an accounts.Account view
// plus a caller-provided state enum (the registry doesn't track lifecycle
// state in Phase 1, so callers infer it from the operation they just did).
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

// computeState maps current client state to the AccountState enum without
// requiring any registry-level state machine. Used by EnsureAccount to
// report a sensible value before any explicit Connect.
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
// Logging & panic safety
// ─────────────────────────────────────────────────────────────────────────────

// logJSON writes one structured event per line to stderr. The Python
// supervisor reads stderr and forwards to the host logger.
func logJSON(event string, fields map[string]any) {
	if fields == nil {
		fields = map[string]any{}
	}
	fields["event"] = event
	fields["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	b, err := json.Marshal(fields)
	if err != nil {
		// Fall back to a plain line so we never lose visibility.
		fmt.Fprintf(os.Stderr, `{"event":"log_marshal_error","error":%q}`+"\n", err.Error())
		return
	}
	fmt.Fprintln(os.Stderr, string(b))
}

// recoverPanic turns any panic in an RPC handler into a structured log line
// so a single account's misbehaviour cannot crash the whole sidecar.
// Single-sidecar mode means the blast radius of a panic is all accounts —
// this is the cheapest mitigation we have until per-account isolation.
func recoverPanic(rpc string) {
	if r := recover(); r != nil {
		logJSON("panic", map[string]any{
			"rpc":   rpc,
			"value": fmt.Sprintf("%v", r),
			"stack": string(debug.Stack()),
		})
	}
}
