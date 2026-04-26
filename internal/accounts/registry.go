// Package accounts 负责 sidecar 进程中 whatsmeow.Client 实例的生命周期：
// 包括创建、连接、断开连接、移除，以及将稳定的业务标识符（"账号 / account_key"）
// 映射到活动客户端的注册表。
//
// 该包在设计上是与事件无关的。调用方传入一个 EventSink —— 一个普通的
// func(any) —— 注册表通过 AddEventHandler 将其附加到每个新客户端。
// events 包封装了该 sink，用于将 whatsmeow 事件转换为 proto 消息，
// 并将其分发给 gRPC 流订阅者；这种拆分使 accounts 无需了解任何 gRPC
// 或 proto 知识，并打破了原本不可避免的循环引用。
//
// 并发模型：
//   - 注册表操作（Ensure、Get、Disconnect、Remove、Snapshot）由
//     单个 RWMutex（读写互斥锁）保护。whatsmeow 内部的热路径（hot path）
//     不会触碰此锁；它仅保护映射（map）。
//   - 每个账号拥有一个 context.Context，其取消信号是
//     我们稍后可能添加的任何每个账号 goroutine（例如 QR pump、重试循环）的
//     权威关闭信号。Disconnect() 会取消它，Remove() 也会。
//   - whatsmeow.Client 自身的 AddEventHandler 是并发安全的；
//     我们附加的 EventSink 本身必须是非阻塞的（events 包通过
//     有界通道 + 溢出时丢弃最新数据的策略来保证这一点）。
package accounts

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"

	"github.com/jianjian2048/fastmeow/internal/sessions"
)

// EventSink 是注册表附加到每个 whatsmeow.Client 事件处理程序列表的
// 副作用回调。events 包为每个账号提供一个回调，以便将事件路由到
// 正确的 StreamEvents 订阅者。
//
// 实现必须是非阻塞的；whatsmeow 同步地从其
// 套接字读取 goroutine 派发事件，处理程序过慢会使
// 该账号的每一个其他事件都发生停顿。
type EventSink func(accountKey string, evt any)

// 暴露给 gRPC 处理程序的错误。
var (
	ErrNotFound      = errors.New("accounts: account not found")
	ErrAlreadyExists = errors.New("accounts: account already registered with a different jid")
)

// Account 是暴露给其他包的只读视图。在内部，我们
// 维护一个更丰富的结构（带有取消函数），但外部调用方仅
// 需要这些信息。
type Account struct {
	Key    string
	JID    types.JID // 对于新账号，在 PairSuccess 之前为零值
	Client *whatsmeow.Client
}

// Registry 是以 account_key 为键的活动客户端中心映射表。
type Registry struct {
	store *sessions.Store
	log   waLog.Logger
	sink  EventSink

	mu       sync.RWMutex
	accounts map[string]*entry
}

type entry struct {
	key    string
	client *whatsmeow.Client
	device *store.Device
	// ctx/cancel 用于限定每个账号 goroutine 的作用域（目前没有，但
	// QR pump 和任何未来的重连监控器都将存在于此处）。
	ctx    context.Context
	cancel context.CancelFunc
	// handlerID 是 AddEventHandler 为我们附加的 sink 返回的值，
	// 以备我们需要 RemoveEventHandler 时使用。目前没有用到，但
	// 存储它没有任何成本，且能避免未来的搜索。
	handlerID uint32
}

// NewRegistry 构造一个绑定到会话存储（sessions store）和
// 事件 sink 的空注册表。在早期引导阶段（在 events 包接入之前），
// sink 可能为 nil，但在生产环境路径中 EnsureAccount 运行之前必须设置它。
func NewRegistry(store *sessions.Store, sink EventSink, log waLog.Logger) *Registry {
	if log == nil {
		log = waLog.Noop
	}
	return &Registry{
		store:    store,
		log:      log,
		sink:     sink,
		accounts: make(map[string]*entry),
	}
}

// EnsureAccount 为 accountKey 返回一个活动的 whatsmeow.Client，
// 必要时会创建一个。
//
//   - jid == 零值 且 不存在现有条目：分配一个新设备用于
//     QR 配对。调用方应在 Connect 之前立即调用
//     GetQRChannel（通过更高级别的 RPC）。
//   - jid != 零值 且 不存在现有条目：从 sqlstore 加载
//     持久化的设备并围绕其构建客户端。这是“重启后恢复”的路径。
//   - 存在现有条目：按原样返回（幂等）。如果调用方传入的
//     jid 与存储的条目 jid 矛盾，我们返回 ErrAlreadyExists，
//     而不是静默地覆盖。
//
// 此处不调用 Connect。配对流程希望在 Connect 之前
// 获取 QR 通道；恢复流程希望在单独的代码路径上调用 Connect，
// 以便连接失败与注册失败能分别显现。调用方需显式组合
// Ensure + Connect。
func (r *Registry) EnsureAccount(ctx context.Context, accountKey string, jid types.JID) (*Account, error) {
	if accountKey == "" {
		return nil, fmt.Errorf("accounts: accountKey is required")
	}

	// 快速路径：已注册。
	r.mu.RLock()
	if e, ok := r.accounts[accountKey]; ok {
		r.mu.RUnlock()
		if !jid.IsEmpty() && !e.device.ID.IsEmpty() && !jidsEqual(*e.device.ID, jid) {
			return nil, fmt.Errorf("%w: key=%q stored=%s requested=%s",
				ErrAlreadyExists, accountKey, e.device.ID, jid)
		}
		return e.toAccount(), nil
	}
	r.mu.RUnlock()

	// 慢速路径：构建新条目。我们在写锁之外进行设备加载，
	// 以避免并发的 EnsureAccount 在磁盘 I/O 上串行化。
	device, err := r.store.LoadOrCreateDevice(ctx, jid)
	if err != nil {
		return nil, err
	}
	subLog := r.log.Sub(accountKey)
	client := whatsmeow.NewClient(device, subLog)

	accountCtx, cancel := context.WithCancel(context.Background())
	e := &entry{
		key:    accountKey,
		client: client,
		device: device,
		ctx:    accountCtx,
		cancel: cancel,
	}

	if r.sink != nil {
		key := accountKey // 为闭包捕获
		e.handlerID = client.AddEventHandler(func(evt any) {
			r.sink(key, evt)
		})
	}

	// 在写锁下重新检查，以防并发的 EnsureAccount 赢得了竞争。
	// 丢弃 `client` 的成本很低（尚未发生网络操作），
	// 而替代方案 —— 同一 accountKey 拥有重复的客户端 ——
	// 将是一个正确性方面的错误。
	r.mu.Lock()
	if existing, ok := r.accounts[accountKey]; ok {
		r.mu.Unlock()
		cancel()
		return existing.toAccount(), nil
	}
	r.accounts[accountKey] = e
	r.mu.Unlock()

	return e.toAccount(), nil
}

// Get 返回 key 对应的账号，或 ErrNotFound。只读。
func (r *Registry) Get(accountKey string) (*Account, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.accounts[accountKey]
	if !ok {
		return nil, ErrNotFound
	}
	return e.toAccount(), nil
}

// Snapshot 返回当前所有已注册账号的切片。适用于
// 未来的 ListAccounts RPC 和优雅关闭（需要断开
// 每个客户端的连接）。
func (r *Registry) Snapshot() []*Account {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Account, 0, len(r.accounts))
	for _, e := range r.accounts {
		out = append(out, e.toAccount())
	}
	return out
}

// Disconnect 断开活动网络连接，但在磁盘上保留设备
// 状态且保留注册表条目，以便随后的 EnsureAccount + Connect
// 可以恢复连接。幂等。
func (r *Registry) Disconnect(accountKey string) error {
	r.mu.RLock()
	e, ok := r.accounts[accountKey]
	r.mu.RUnlock()
	if !ok {
		return ErrNotFound
	}
	// whatsmeow.Client.Disconnect 本身是幂等的且内部加了锁，
	// 因此在不持有我们互斥锁的情况下调用它是可以的，
	// 且能避免在网络操作期间持有注册表锁。
	e.client.Disconnect()
	return nil
}

// Remove 断开客户端连接并从磁盘中删除其设备状态。
// 调用此方法后，使用相同 key 的 EnsureAccount 将从
// 新的 QR 配对流程开始。由 RemoveAccount RPC 使用。
//
// 如果没有注册此类账号，则返回 ErrNotFound。如果磁盘设备存在
// 但无法删除（例如数据库 I/O 失败），则返回底层的删除错误；
// 无论如何都会移除内存中的条目，以便调用方重试注册。
func (r *Registry) Remove(ctx context.Context, accountKey string) error {
	r.mu.Lock()
	e, ok := r.accounts[accountKey]
	if !ok {
		r.mu.Unlock()
		return ErrNotFound
	}
	delete(r.accounts, accountKey)
	r.mu.Unlock()

	e.client.Disconnect()
	e.cancel()

	if e.device != nil && e.device.ID != nil && !e.device.ID.IsEmpty() {
		return r.store.DeleteDevice(ctx, *e.device.ID)
	}
	return nil
}

// Shutdown 断开每个已注册客户端的连接。从 sidecar 的
// 优雅关闭路径中调用。尽力而为：错误不会被汇总，
// 因为在进程退出时无法对它们进行任何有用的处理。
func (r *Registry) Shutdown() {
	r.mu.Lock()
	entries := make([]*entry, 0, len(r.accounts))
	for _, e := range r.accounts {
		entries = append(entries, e)
	}
	r.accounts = make(map[string]*entry)
	r.mu.Unlock()

	for _, e := range entries {
		e.client.Disconnect()
		e.cancel()
	}
}

// toAccount 将内部条目映射到只读的 Account 视图。
// 调用方在调用时必须持有 r.mu（读锁或写锁），或者该条目
// 刚刚从映射表中被移除（这就是 Remove 所做的）。
func (e *entry) toAccount() *Account {
	a := &Account{Key: e.key, Client: e.client}
	if e.device != nil && e.device.ID != nil {
		a.JID = *e.device.ID
	}
	return a
}

// jidsEqual 比较两个 JID 时忽略设备后缀，因为 whatsmeow
// 在 store.Device 中存储带设备 ID 的 AD JID，但清单（manifest）
// 通常携带用户 JID。出于“你是否为我预期的账号”的目的，
// 我们将它们视为相同的身份。
func jidsEqual(a, b types.JID) bool {
	return a.User == b.User && a.Server == b.Server
}
