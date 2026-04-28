// handler.go 实现 internal/media 的核心 Send / Download 逻辑。
//
// 设计原则：
//   - 与 messages.Sender / receipts.Handler / presence.Handler 风格一致：
//     不持有 *whatsmeow.Client，由 sidecar gateway 层从 registry 取出并传入；
//     避免循环引用，单测无副作用（除临时文件 IO）。
//   - 媒体 RPC 是流式的，handler 暴露 plain reader / chunkWriter 接口，
//     由 gateway 层负责把 grpc.ServerStream 桥接到这两个抽象。这保持
//     handler 与 grpc 解耦：未来切换到 HTTP/2 stream 或本地共享内存时
//     不需要改 handler。
//   - 100 MiB 上限 + 256 KiB 分片是 wire 协议级常量；上传上限以实际累积
//     字节数判断（不信任 declared_file_length，但与之核对）。
//   - 并发控制：
//     1) 全局 semaphore（默认 4）防止 100 账号同时上传打爆带宽 / 内存；
//     2) per-account 互斥：同一账号的并发上传序列化，避免 whatsmeow
//        Upload state（如 cdn key 缓存）在并发下 race。
//        下载不做 per-account 串行 —— whatsmeow Download 是无状态读路径。
//   - 临时文件：上传 / 下载都先落到 rootDir/_media_tmp/<account_key>/
//     再交给 whatsmeow，确保超大文件不撑爆进程内存；defer 总会清理。
package media

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// Wire 协议级常量。任何调整都需要 bump protocolVersion + 同步 Python 端。
const (
	// MaxMediaBytes 是单个媒体文件的硬上限。WhatsApp 服务端实际更宽松
	// （部分文档 / 视频可达 2 GiB），但 100 MiB 在 100 账号并发场景下
	// 是内存 + 临时盘成本的合理折衷；超过需要单独决策。
	MaxMediaBytes = 100 * 1024 * 1024

	// ChunkSize 是 DownloadMedia 服务端流的单帧上限。256 KiB 远低于
	// gRPC 默认 4 MiB max_recv_msg_size，给 wire 元数据留余量；同时
	// 大到能让 Python 端 asyncio loop 有时间在帧间切换。
	ChunkSize = 256 * 1024
)

// Handler 持有跨账号共享的并发资源（global semaphore + per-account 锁表）
// 与临时文件根目录。每 sidecar 一个实例。所有方法对并发使用安全。
//
// 与其它 internal/* handler 不同的是：Handler 这里需要状态（信号量、
// 锁表、临时目录），所以构造函数接受 rootDir + globalLimit。
type Handler struct {
	// rootDir 是 _media_tmp 目录的父目录（通常等于 sessions/）。
	// 实际临时文件位于 rootDir/_media_tmp/<account_key>/。
	rootDir string

	// globalSem 限制跨所有账号同时进行的传输数量。容量来自构造参数
	// （默认 4）。下载与上传共用同一信号量，因为带宽 / 临时盘是共享资源。
	globalSem chan struct{}

	// mu 保护 accountMux 的读写。lockAccount 是热路径，
	// 这里用 sync.Mutex（非 RWMutex）因为锁表的读 + 写都是 O(1) 短路径。
	mu         sync.Mutex
	accountMux map[string]*sync.Mutex
}

// NewHandler 构造 Handler。globalLimit ≤ 0 时回退到默认 4。
//
// rootDir 通常由 sidecar 把 sessions 根目录传进来；Handler 在其下创建
// 子目录 "_media_tmp" 存放临时文件。子目录命名固定，便于运维清理
// （sidecar 重启 / 崩溃后残留可由外部脚本批量删）。
func NewHandler(rootDir string, globalLimit int) *Handler {
	if globalLimit <= 0 {
		globalLimit = 4
	}
	return &Handler{
		rootDir:    filepath.Join(rootDir, "_media_tmp"),
		globalSem:  make(chan struct{}, globalLimit),
		accountMux: make(map[string]*sync.Mutex),
	}
}

// lockAccount 取出（必要时新建）账号级互斥锁并加锁。返回 unlock 闭包。
//
// 设计：用 map[string]*sync.Mutex 而非 sync.Map —— Map 的 LoadOrStore
// 不能给 Mutex 提供初始化保证（会拷贝零值），且我们不需要无锁读路径。
// 锁表条目永不清理：单 sidecar 终生看到的 account_key 数量受限
// （通常 ≤ 100），不会无限膨胀。
func (h *Handler) lockAccount(key string) func() {
	h.mu.Lock()
	m, ok := h.accountMux[key]
	if !ok {
		m = &sync.Mutex{}
		h.accountMux[key] = m
	}
	h.mu.Unlock()
	m.Lock()
	return m.Unlock
}

// acquireGlobal / releaseGlobal 是全局信号量的 helper。
// 阻塞式 acquire（不携带 ctx 超时）—— 如果 100 账号同时尝试上传，
// 多余请求会排队等待；客户端可通过 ctx 超时主动取消。
func (h *Handler) acquireGlobal()  { h.globalSem <- struct{}{} }
func (h *Handler) releaseGlobal()  { <-h.globalSem }

// Send 处理一次完整的上传：校验 init → 落临时文件 → whatsmeow.UploadReader
// → 构造 *waE2E.Message → cli.SendMessage → 返回 server timestamp + msg id。
//
// body 由 gateway 层提供，背后是 client streaming 的 chunk 拼接（io.Pipe）。
// Send 不感知是否还有 chunk 在路上 —— 读到 EOF 就停。declared_file_length
// 仅用于校验（unzero 时必须等于实际累积字节）。
//
// 失败场景全部映射到本包的 sentinel 或 wrapper error；gateway 层负责把
// 它们转成 codes.*。Whatsmeow 自身的 sentinel（ErrUnknownMediaType / ...
// ErrFileLengthMismatch 等）会以 wrap 形式透传，gateway 用 errors.Is 命中。
func (h *Handler) Send(
	ctx context.Context,
	cli *whatsmeow.Client,
	init *pb.SendMediaInit,
	body io.Reader,
) (*pb.SendMediaResponse, error) {
	if init == nil {
		return nil, ErrMissingInitFrame
	}
	if init.GetClientMsgId() == "" {
		return nil, ErrEmptyClientMsgID
	}
	if init.GetToJid() == "" {
		return nil, ErrEmptyToJID
	}
	if init.GetMimeType() == "" {
		return nil, ErrMissingMimeType
	}
	if init.GetDeclaredFileLength() > MaxMediaBytes {
		return nil, ErrMediaTooLarge
	}

	// 提前 ParseJID 是为了在做任何 IO 之前就拒掉 InvalidArgument；
	// 这样客户端能更快收到错误，临时文件也无需创建。
	jid, err := types.ParseJID(init.GetToJid())
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidJID, init.GetToJid(), err)
	}

	// 提前把 MediaKind 转成 whatsmeow.MediaType；如果 kind 不支持，
	// 直接返回 ErrUnsupportedMediaKind，避免白白把 chunk 流到磁盘后才发现。
	mediaType, err := mediaTypeFromProto(init.GetKind())
	if err != nil {
		return nil, err
	}

	// nil-cli 防御断言放在所有纯参数校验之后，以便单测可以用 nil cli
	// 触达 init / kind / jid 校验路径而不必伪造 *whatsmeow.Client。
	if cli == nil {
		return nil, errors.New("media: nil whatsmeow.Client")
	}

	// per-account 串行 + 全局并发控制。
	unlock := h.lockAccount(init.GetAccountKey())
	defer unlock()

	h.acquireGlobal()
	defer h.releaseGlobal()

	tempDir := filepath.Join(h.rootDir, init.GetAccountKey())
	if err := os.MkdirAll(tempDir, 0o755); err != nil {
		return nil, fmt.Errorf("media: mkdir temp dir: %w", err)
	}
	file, err := os.CreateTemp(tempDir, "upload-*")
	if err != nil {
		return nil, fmt.Errorf("media: create temp file: %w", err)
	}
	defer func() {
		_ = file.Close()
		_ = os.Remove(file.Name())
	}()

	// LimitReader 多读 1 字节用于检测越界 —— 客户端 chunk 流可能比
	// declared_file_length 多 send，我们必须能识别这种偏离。
	n, err := io.Copy(file, io.LimitReader(body, MaxMediaBytes+1))
	if err != nil {
		return nil, fmt.Errorf("media: spool upload body: %w", err)
	}
	if n > MaxMediaBytes {
		return nil, ErrMediaTooLarge
	}
	if init.GetDeclaredFileLength() != 0 && uint64(n) != init.GetDeclaredFileLength() {
		return nil, fmt.Errorf("%w: declared=%d actual=%d",
			ErrDeclaredLengthMismatch, init.GetDeclaredFileLength(), n)
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("media: rewind temp file: %w", err)
	}

	// UploadReader 把同一个 *os.File 既当 plaintext 来源也当加密临时文件，
	// 上传完成后 file 内容已被加密内容覆盖（per whatsmeow 文档）。
	// 这无所谓 —— defer 会立即删除。
	upload, err := cli.UploadReader(ctx, file, file, mediaType)
	if err != nil {
		return nil, fmt.Errorf("media: upload: %w", err)
	}

	msg, err := buildMessage(init, upload)
	if err != nil {
		return nil, err
	}
	if quoted := init.GetQuotedMessageId(); quoted != "" {
		// quoted_message_id 仅在 init 提供时附加 ContextInfo.StanzaID。
		// 我们不试图重建 quoted 内容（whatsmeow 不要求；客户端只看到引用框架）。
		attachQuotedContext(msg, init.GetKind(), quoted)
	}

	extra := whatsmeow.SendRequestExtra{ID: types.MessageID(init.GetClientMsgId())}
	resp, err := cli.SendMessage(ctx, jid, msg, extra)
	if err != nil {
		return nil, fmt.Errorf("media: send message: %w", err)
	}
	return &pb.SendMediaResponse{
		MessageId:       resp.ID,
		ServerTimestamp: timestamppb.New(resp.Timestamp),
		// Phase 4.3 Batch 2 暂不实装服务端去重缓存（plan §2.11 留作 Batch 4
		// 决策点）。Deduped 字段保留 false；客户端可在 Python 层按
		// (account_key, client_msg_id) 自己幂等。
		Deduped: false,
	}, nil
}

// Download 处理一次完整的下载：校验 MediaInfo → 还原 *waE2E.XxxMessage
// → cli.DownloadToFile → 按 ChunkSize 分片回调 chunkWriter。
//
// chunkWriter 由 gateway 层注入，背后是 stream.Send。Download 不感知 grpc。
// 任何 chunkWriter 返回的错误立即终止下载并向上冒泡。
//
// 与 Send 不同，下载**不做 per-account 串行**：whatsmeow.Download 是无状态
// 的 https GET + 解密，单账号并发不会破坏内部状态；只受全局 semaphore 限。
func (h *Handler) Download(
	ctx context.Context,
	cli *whatsmeow.Client,
	media *pb.MediaInfo,
	chunkWriter func([]byte) error,
) error {
	if chunkWriter == nil {
		return errors.New("media: nil chunkWriter")
	}
	downloadable, err := downloadableFromProto(media)
	if err != nil {
		return err
	}
	if cli == nil {
		return errors.New("media: nil whatsmeow.Client")
	}

	h.acquireGlobal()
	defer h.releaseGlobal()

	if err := os.MkdirAll(h.rootDir, 0o755); err != nil {
		return fmt.Errorf("media: mkdir temp root: %w", err)
	}
	tempFile, err := os.CreateTemp(h.rootDir, "download-*")
	if err != nil {
		return fmt.Errorf("media: create download temp file: %w", err)
	}
	defer func() {
		_ = tempFile.Close()
		_ = os.Remove(tempFile.Name())
	}()

	if err := cli.DownloadToFile(ctx, downloadable, tempFile); err != nil {
		return fmt.Errorf("media: download: %w", err)
	}
	if _, err := tempFile.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("media: rewind downloaded file: %w", err)
	}

	reader := bufio.NewReaderSize(tempFile, ChunkSize)
	buf := make([]byte, ChunkSize)
	for {
		n, readErr := reader.Read(buf)
		if n > 0 {
			// 拷贝出独立 slice：chunkWriter 背后可能是 grpc.Stream.Send，
			// 它在 enqueue 后异步序列化，复用 buf 会导致后续帧覆盖前一帧。
			out := make([]byte, n)
			copy(out, buf[:n])
			if err := chunkWriter(out); err != nil {
				return err
			}
		}
		if errors.Is(readErr, io.EOF) {
			return nil
		}
		if readErr != nil {
			return fmt.Errorf("media: stream chunk: %w", readErr)
		}
	}
}

// attachQuotedContext 把 quoted_message_id 附加到对应的子消息 ContextInfo。
//
// 每种媒体子消息（ImageMessage / VideoMessage / ...）都各自持有独立的
// ContextInfo 字段。我们必须按 kind 分发；容器 *waE2E.Message 本身没有
// 通用 ContextInfo。
//
// 这里**只填 StanzaID**：whatsmeow 在发送时不强制要求 Participant、
// QuotedMessage 等字段（接收端 UI 只展示一个引用框架，没有原文也能渲染）。
// 完整的 reply 重建需要客户端从历史里找到原 message —— 超出 sidecar 职责。
func attachQuotedContext(msg *waE2E.Message, kind pb.MediaKind, quotedID string) {
	ctx := &waE2E.ContextInfo{StanzaID: proto.String(quotedID)}
	switch kind {
	case pb.MediaKind_MEDIA_KIND_IMAGE:
		if m := msg.GetImageMessage(); m != nil {
			m.ContextInfo = ctx
		}
	case pb.MediaKind_MEDIA_KIND_VIDEO:
		if m := msg.GetVideoMessage(); m != nil {
			m.ContextInfo = ctx
		}
	case pb.MediaKind_MEDIA_KIND_AUDIO:
		if m := msg.GetAudioMessage(); m != nil {
			m.ContextInfo = ctx
		}
	case pb.MediaKind_MEDIA_KIND_DOCUMENT:
		if m := msg.GetDocumentMessage(); m != nil {
			m.ContextInfo = ctx
		}
	case pb.MediaKind_MEDIA_KIND_STICKER:
		if m := msg.GetStickerMessage(); m != nil {
			m.ContextInfo = ctx
		}
	}
}
