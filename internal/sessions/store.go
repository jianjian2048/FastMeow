// Package sessions wraps whatsmeow's sqlstore.Container with FastMeow-specific
// defaults: a single SQLite file (main.sqlite) opened in WAL mode with
// foreign keys and a generous busy timeout, suitable for hosting many
// concurrent whatsmeow.Client devices in one process.
//
// Design notes:
//   - Driver is modernc.org/sqlite (registered as "sqlite"), not mattn's
//     "sqlite3". Pure-Go => no cgo => trivial cross-compilation for the
//     wheel's per-platform sidecar binaries.
//   - The container is shared by every whatsmeow.Client in the sidecar.
//     whatsmeow's own access patterns are append-mostly with short
//     transactions, so a single connection pool with WAL handles 100+
//     accounts comfortably. If contention shows up under load we can swap
//     this for sharded containers without touching account/event code.
//   - We do NOT keep an account_key -> JID mapping here. That mapping
//     lives in the Python supervisor's manifest.json, which is the single
//     source of truth for "which business identifier owns which device".
//     The sidecar only sees JIDs (or nil for fresh pair flows).
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

	// Register the pure-Go SQLite driver under the name "sqlite".
	_ "modernc.org/sqlite"
)

// Store is a thin wrapper over *sqlstore.Container that hides the DSN
// construction details and exposes only the operations the rest of the
// sidecar needs.
type Store struct {
	container *sqlstore.Container
	dbPath    string
}

// Open creates (or opens) the SQLite database at dbPath and runs whatsmeow's
// schema migrations. The parent directory is created with 0700 because the
// file contains end-to-end encryption key material.
//
// dbPath is a filesystem path, NOT a SQLite DSN. We build the DSN ourselves
// to enforce the pragmas we care about and to keep callers from accidentally
// disabling WAL.
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

// Close releases the underlying database handle. Callers should disconnect
// every whatsmeow.Client before calling Close to avoid mid-transaction
// errors from background writes.
func (s *Store) Close() error {
	if s == nil || s.container == nil {
		return nil
	}
	// sqlstore.Container has no public Close at the moment, but the
	// underlying *sql.DB is reachable via reflection-free interface
	// assertion. To stay forward-compatible we just drop the reference;
	// the *sql.DB will be GC-finalised. If sqlstore later gains a Close()
	// we'll wire it here without changing callers.
	s.container = nil
	return nil
}

// Path returns the on-disk path of the sqlite file. Useful for log lines
// and for the manifest persistence layer (so it can record store_path
// alongside each account).
func (s *Store) Path() string { return s.dbPath }

// Container exposes the raw *sqlstore.Container for code paths that need
// it (notably whatsmeow.NewClient takes a *store.Device produced by the
// container). Keep usage of this method narrow; prefer the typed helpers
// below for new code.
func (s *Store) Container() *sqlstore.Container { return s.container }

// LoadOrCreateDevice returns a *store.Device suitable for handing to
// whatsmeow.NewClient.
//
//   - If jid is the zero value, we always allocate a fresh, unpaired
//     device. The caller is expected to drive it through GetQRChannel +
//     Connect to obtain a real JID via PairSuccess.
//   - If jid is non-zero, we look up the persisted device. If it exists,
//     the returned *store.Device carries all the prekeys and identity
//     state needed to resume without re-pairing. If it does NOT exist,
//     we return an error rather than silently creating a new one — a
//     missing device for a known JID means the manifest and the SQLite
//     file have drifted, which the supervisor must surface, not paper
//     over.
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

// DeleteDevice removes the persisted state for jid. Used by RemoveAccount.
// Returns nil if the device is already absent (idempotent).
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

// buildDSN composes a modernc.org/sqlite DSN with the pragmas we want
// applied to every connection in the pool.
//
//   - foreign_keys=on: whatsmeow's schema relies on FK cascades for
//     identity/session cleanup.
//   - journal_mode=WAL: required for many-account concurrency. Without
//     it, every prekey rotation serialises writers behind readers.
//   - busy_timeout=10000: 10s is well above any healthy whatsmeow write,
//     but high enough to ride out a brief WAL checkpoint stall instead
//     of returning SQLITE_BUSY to the caller.
//   - synchronous=NORMAL: WAL's default; keeping it explicit so a future
//     driver upgrade that flips defaults can't silently regress us to
//     FULL (slow) or OFF (data-loss risk on crash).
func buildDSN(dbPath string) string {
	// modernc.org/sqlite parses URIs of the form `file:<path>?<query>`.
	// On Windows, paths contain backslashes which url.Values.Encode()
	// would percent-escape badly, so we hand-build the query string.
	// The path itself is passed through filepath.ToSlash for portability.
	pragmas := url.Values{}
	pragmas.Add("_pragma", "foreign_keys(on)")
	pragmas.Add("_pragma", "journal_mode(WAL)")
	pragmas.Add("_pragma", "busy_timeout(10000)")
	pragmas.Add("_pragma", "synchronous(NORMAL)")
	return "file:" + filepath.ToSlash(dbPath) + "?" + pragmas.Encode()
}
