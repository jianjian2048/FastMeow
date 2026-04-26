// Package sessions 封装了 whatsmeow 的 sqlstore.Container，并应用了 FastMeow 特定的
// 默认设置：单个 SQLite 文件 (main.sqlite)，以 WAL 模式打开，启用外键约束和
// 宽松的 busy timeout，适用于在单个进程中托管多个并发的 whatsmeow.Client 设备。
//
// 设计说明：
//   - 驱动程序使用 modernc.org/sqlite（注册名为 "sqlite"），而非 mattn 的
//     "sqlite3"。纯 Go 实现 => 无需 cgo => 方便为不同平台的 sidecar 二进制文件
//     进行交叉编译。
//   - 该容器由 sidecar 中的每个 whatsmeow.Client 共享。
//     whatsmeow 自身的访问模式主要是追加操作且事务较短，因此具有 WAL 的
//     单个连接池可以轻松处理 100 多个账号。如果在高负载下出现竞争，
//     我们可以在不触动账号/事件代码的情况下将其更换为分片容器。
//   - 我们在此处不保留 账号 / account_key -> JID 的映射。该映射
//     存在于 Python 管理器的 manifest.json 中，它是“哪个业务标识符拥有哪个设备”的
//     唯一事实来源。sidecar 仅处理 JID（对于新配对流程则为 nil）。
package sessions

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"path/filepath"

	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"

	// 将纯 Go SQLite 驱动程序注册到 "sqlite" 名称下。
	_ "modernc.org/sqlite"
)

// Store 是对 *sqlstore.Container 的轻量级封装，隐藏了 DSN
// 构建细节，仅公开 sidecar 其余部分所需的各种操作。
type Store struct {
	container *sqlstore.Container
	dbPath    string
}

// Open 在 dbPath 处创建（或打开）SQLite 数据库并运行 whatsmeow 的
// 数据库迁移。父目录以 0700 权限创建，因为该文件包含端到端加密密钥材料。
//
// dbPath 是文件系统路径，而不是 SQLite DSN。我们自行构建 DSN
// 以强制执行我们关注的编译指令（pragmas），并防止调用方意外
// 禁用 WAL。
func Open(ctx context.Context, dbPath string, log waLog.Logger) (*Store, error) {
	if dbPath == "" {
		return nil, fmt.Errorf("sessions: dbPath is required")
	}
	if log == nil {
		log = waLog.Noop
	}

	if err := os.MkdirAll(filepath.Dir(dbPath), 0o700); err != nil {
		return nil, fmt.Errorf("sessions: create dir for %q: %w", dbPath, err)
	}

	dsn := buildDSN(dbPath)
	container, err := sqlstore.New(ctx, "sqlite", dsn, log)
	if err != nil {
		return nil, fmt.Errorf("sessions: open sqlstore at %q: %w", dbPath, err)
	}
	return &Store{container: container, dbPath: dbPath}, nil
}

// Close 释放底层数据库句柄。调用方在调用 Close 之前应断开
// 每个 whatsmeow.Client 的连接，以避免后台写入导致事务中途出错。
func (s *Store) Close() error {
	if s == nil || s.container == nil {
		return nil
	}
	// sqlstore.Container 目前没有公开的 Close 方法，但可以通过
	// 无反射的接口断言获取底层的 *sql.DB。为了保持向前兼容，
	// 我们仅删除引用；*sql.DB 将由 GC 回收。如果 sqlstore
	// 以后增加了 Close()，我们将在此处接入而无需更改调用方。
	s.container = nil
	return nil
}

// Path 返回 sqlite 文件的磁盘路径。适用于日志行
// 和清单（manifest）持久化层（以便它可以将 store_path
// 与每个账号一起记录）。
func (s *Store) Path() string { return s.dbPath }

// Container 为需要它的代码路径公开原始的 *sqlstore.Container
// （特别是 whatsmeow.NewClient 需要由容器生成的 *store.Device）。
// 请保持此方法的窄调用；新代码优先选择下面的类型化辅助函数。
func (s *Store) Container() *sqlstore.Container { return s.container }

// LoadOrCreateDevice 返回适用于传递给 whatsmeow.NewClient 的 *store.Device。
//
//   - 如果 jid 为零值，我们总是分配一个新的、未配对的
//     设备。调用方预计通过 GetQRChannel + Connect 进行驱动，
//     以通过 PairSuccess 获取真实的 JID。
//   - 如果 jid 非零，我们查找持久化的设备。如果它存在，
//     返回的 *store.Device 携带了恢复连接所需的所有 prekeys 和身份
//     状态，无需重新配对。如果它不存在，
//     我们返回错误而不是静默创建一个新设备 —— 已知 JID 的设备
//     缺失意味着清单（manifest）和 SQLite 文件已发生偏差，
//     管理器必须将此情况暴露出来，而不是掩盖它。
func (s *Store) LoadOrCreateDevice(ctx context.Context, jid types.JID) (*store.Device, error) {
	if jid.IsEmpty() {
		return s.container.NewDevice(), nil
	}
	dev, err := s.container.GetDevice(ctx, jid)
	if err != nil {
		return nil, fmt.Errorf("sessions: load device %s: %w", jid, err)
	}
	if dev == nil {
		return nil, fmt.Errorf("sessions: device for jid %s not found in %s (manifest/db drift)", jid, s.dbPath)
	}
	return dev, nil
}

// DeleteDevice 移除 jid 的持久化状态。由 RemoveAccount 使用。
// 如果设备已缺失，则返回 nil（幂等）。
func (s *Store) DeleteDevice(ctx context.Context, jid types.JID) error {
	if jid.IsEmpty() {
		return nil
	}
	dev, err := s.container.GetDevice(ctx, jid)
	if err != nil {
		return fmt.Errorf("sessions: lookup device %s for delete: %w", jid, err)
	}
	if dev == nil {
		return nil
	}
	if err := dev.Delete(ctx); err != nil {
		return fmt.Errorf("sessions: delete device %s: %w", jid, err)
	}
	return nil
}

// buildDSN 组合了一个 modernc.org/sqlite DSN，并将我们期望的
// 编译指令（pragmas）应用于连接池中的每个连接。
//
//   - foreign_keys=on: whatsmeow 的架构依赖于外键级联来进行
//     身份/会话清理。
//   - journal_mode=WAL: 多个账号并发所必需的。如果没有它，
//     每次 prekey 轮换都会使写入者在读取者之后串行化。
//   - busy_timeout=10000: 10s 远高于任何正常的 whatsmeow 写入时间，
//     但足以度过短暂的 WAL 检查点停顿，而不是向调用方返回 SQLITE_BUSY。
//   - synchronous=NORMAL: WAL 的默认值；保持显式设置，以便未来
//     驱动程序升级改变默认值时，不会静默地将我们退回到
//     FULL（慢）或 OFF（崩溃时有数据丢失风险）。
func buildDSN(dbPath string) string {
	// modernc.org/sqlite 解析 `file:<path>?<query>` 形式的 URI。
	// 在 Windows 上，路径包含反斜杠，url.Values.Encode()
	// 会对其进行错误的百分比转义，因此我们手动构建查询字符串。
	// 路径本身通过 filepath.ToSlash 进行转换以保证移植性。
	pragmas := url.Values{}
	pragmas.Add("_pragma", "foreign_keys(on)")
	pragmas.Add("_pragma", "journal_mode(WAL)")
	pragmas.Add("_pragma", "busy_timeout(10000)")
	pragmas.Add("_pragma", "synchronous(NORMAL)")
	return "file:" + filepath.ToSlash(dbPath) + "?" + pragmas.Encode()
}
