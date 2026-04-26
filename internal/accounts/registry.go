// Package accounts owns the lifecycle of whatsmeow.Client instances inside
// the sidecar process: creation, connection, disconnection, removal, and
// the registry that maps a stable business identifier ("account key") to
// a live client.
//
// The package is intentionally event-agnostic. Callers pass in an
// EventSink — a plain func(any) — that the registry attaches to every
// new client via AddEventHandler. The events package wraps that sink to
// translate whatsmeow events into proto messages and fan them out to
// gRPC stream subscribers; this split keeps accounts free of any gRPC
// or proto knowledge and breaks an otherwise inevitable import cycle.
//
// Concurrency model:
//   - Registry operations (Ensure, Get, Disconnect, Remove, Snapshot) are
//     guarded by a single RWMutex. The hot path inside whatsmeow itself
//     does not touch this mutex; it only protects the map.
//   - Each account owns a context.Context whose cancellation is the
//     authoritative shutdown signal for any per-account goroutines (e.g.
//     QR pump, retry loops) we may add later. Disconnect() cancels it,
//     Remove() does too.
//   - whatsmeow.Client's own AddEventHandler is safe for concurrent use;
//     the EventSink we attach must itself be non-blocking (the events
//     package guarantees this with a bounded channel + drop-newest
//     policy on overflow).
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

// EventSink is the side-effect callback the registry attaches to each
// whatsmeow.Client's event handler list. The events package supplies one
// per account so it can route events to the right StreamEvents subscriber.
//
// Implementations MUST NOT block; whatsmeow dispatches events
// synchronously from its socket-read goroutine and a slow handler stalls
// every other event for that account.
type EventSink func(accountKey string, evt any)

// Errors surfaced to gRPC handlers.
var (
	ErrNotFound      = errors.New("accounts: account not found")
	ErrAlreadyExists = errors.New("accounts: account already registered with a different jid")
)

// Account is the read-only view exposed to other packages. Internally we
// keep a richer struct (with the cancel func) but external callers only
// need this much.
type Account struct {
	Key    string
	JID    types.JID // zero value until PairSuccess for fresh accounts
	Client *whatsmeow.Client
}

// Registry is the central account-key-keyed map of live clients.
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
	// ctx/cancel scope per-account goroutines (none today, but the QR
	// pump and any future reconnect supervisor will live here).
	ctx    context.Context
	cancel context.CancelFunc
	// handlerID is the value AddEventHandler returned for the sink we
	// attached, in case we ever need RemoveEventHandler. We don't use
	// it today but storing it costs nothing and avoids a future search.
	handlerID uint32
}

// NewRegistry constructs an empty registry bound to a sessions store and
// an event sink. The sink may be nil during early bootstrap (before the
// events package is wired up) but must be set before EnsureAccount runs
// in production paths.
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

// EnsureAccount returns a live whatsmeow.Client for accountKey, creating
// one if necessary.
//
//   - jid == zero  AND  no existing entry: allocate a fresh device for
//     QR pairing. The caller is expected to immediately call
//     GetQRChannel (via a higher-level RPC) before Connect.
//   - jid != zero  AND  no existing entry: load the persisted device
//     from the sqlstore and build a client around it. This is the
//     "resume after restart" path.
//   - existing entry: returned as-is (idempotent). If the caller passed
//     a jid that contradicts the stored entry's jid we return
//     ErrAlreadyExists rather than silently overwriting.
//
// Connect is NOT called here. Pairing flows want to obtain the QR channel
// before Connect; resume flows want Connect on a separate code path so
// that connection failures surface distinctly from registration failures.
// Callers compose Ensure + Connect explicitly.
func (r *Registry) EnsureAccount(ctx context.Context, accountKey string, jid types.JID) (*Account, error) {
	if accountKey == "" {
		return nil, fmt.Errorf("accounts: accountKey is required")
	}

	// Fast path: already registered.
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

	// Slow path: build a new entry. We do the device load OUTSIDE the
	// write lock to avoid serialising every concurrent EnsureAccount on
	// disk I/O.
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
		key := accountKey // capture for closure
		e.handlerID = client.AddEventHandler(func(evt any) {
			r.sink(key, evt)
		})
	}

	// Re-check under the write lock in case a concurrent EnsureAccount
	// won the race. The cost of throwing away `client` is low (no
	// network has happened yet) and the alternative — duplicate clients
	// for the same accountKey — would be a correctness bug.
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

// Get returns the account for key, or ErrNotFound. Read-only.
func (r *Registry) Get(accountKey string) (*Account, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.accounts[accountKey]
	if !ok {
		return nil, ErrNotFound
	}
	return e.toAccount(), nil
}

// Snapshot returns a slice of every currently-registered account. Useful
// for a future ListAccounts RPC and for graceful shutdown which needs to
// disconnect every client.
func (r *Registry) Snapshot() []*Account {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Account, 0, len(r.accounts))
	for _, e := range r.accounts {
		out = append(out, e.toAccount())
	}
	return out
}

// Disconnect tears down the live network connection but keeps the device
// state on disk and the registry entry in place, so a subsequent
// EnsureAccount + Connect can resume. Idempotent.
func (r *Registry) Disconnect(accountKey string) error {
	r.mu.RLock()
	e, ok := r.accounts[accountKey]
	r.mu.RUnlock()
	if !ok {
		return ErrNotFound
	}
	// whatsmeow.Client.Disconnect is itself idempotent and locks
	// internally, so calling it without holding our mutex is fine and
	// avoids holding the registry lock across a network operation.
	e.client.Disconnect()
	return nil
}

// Remove disconnects the client AND deletes its device state from disk.
// After this call EnsureAccount with the same key will start from a
// fresh QR pairing flow. Used by the RemoveAccount RPC.
//
// Returns ErrNotFound if no such account is registered. Returns the
// underlying delete error if the on-disk device exists but cannot be
// removed (e.g. database I/O failure); the in-memory entry is removed
// regardless so the caller can retry registration.
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

// Shutdown disconnects every registered client. Called from the sidecar's
// graceful-shutdown path. Best-effort: errors are not aggregated because
// nothing useful can be done with them at process exit.
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

// toAccount projects an internal entry to the read-only Account view.
// Caller must hold r.mu (read or write) when calling, OR the entry must
// have just been removed from the map (which is what Remove does).
func (e *entry) toAccount() *Account {
	a := &Account{Key: e.key, Client: e.client}
	if e.device != nil && e.device.ID != nil {
		a.JID = *e.device.ID
	}
	return a
}

// jidsEqual compares two JIDs ignoring the device suffix, since whatsmeow
// stores the AD JID (with device id) in store.Device but the manifest
// usually carries the user JID. We treat them as the same identity for
// the purpose of "are you the account I expected".
func jidsEqual(a, b types.JID) bool {
	return a.User == b.User && a.Server == b.Server
}
