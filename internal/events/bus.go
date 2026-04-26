// Package events translates whatsmeow event types into the proto
// StreamEventsResponse wire format and multiplexes them onto a single
// process-wide channel for the gRPC StreamEvents server-stream.
//
// Architecture
//
// One Bus per sidecar process. Every whatsmeow.Client registered through
// the accounts package has its event handler routed into bus.Sink, which:
//
//  1. assigns a monotonically-increasing seq number,
//  2. stamps observed_at,
//  3. translates the Go event to the proto oneof,
//  4. enqueues onto a single bounded channel.
//
// Phase 1 deliberately supports exactly one StreamEvents subscriber per
// sidecar — the Python supervisor — matching the proto comment
// ("Python opens exactly one StreamEvents call per sidecar"). A second
// concurrent Subscribe panics rather than silently splitting events.
//
// Backpressure
//
// The bus channel is bounded (default 1024). On overflow we drop the
// INCOMING event and emit a warning log line. We deliberately do NOT
// block whatsmeow's dispatch goroutine: blocking it stalls every other
// account's event delivery. The drop-newest policy is acceptable in
// Phase 1 because:
//
//  - We do not subscribe to soft events (presence/receipts), so
//    the channel only carries connection-state changes and inbound
//    messages, both of which are low-volume.
//  - Inbound messages are already delivered with at-most-once semantics
//    by whatsmeow itself; a Python-side reconciliation pass on reconnect
//    is the right way to recover from drops, not infinite buffering here.
//
// QR pairing
//
// QR codes do NOT come through AddEventHandler — whatsmeow exposes them
// via Client.GetQRChannel(ctx) which returns a typed channel of
// QRChannelItem. PumpQRChannel bridges that into the bus so subscribers
// see qr / pair_success / disconnected events through the same wire path
// as everything else.
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

// DefaultBufferSize is the depth of the bus channel. Sized for ~1 second
// of bursty inbound traffic across all accounts before we start dropping.
const DefaultBufferSize = 1024

// Bus is the single fan-in/fan-out point for sidecar-wide event flow.
type Bus struct {
	sidecarID string
	log       waLog.Logger

	// out is the only channel; subscribed exactly once.
	out chan *pb.StreamEventsResponse

	// seq is the monotonic counter assigned to every emitted event.
	seq atomic.Uint64

	// subscribed flips true on the first Subscribe call. Second call panics.
	subscribed atomic.Bool

	// jids holds the most recently observed JID per account_key, so we
	// can stamp account_jid on every outgoing event without forcing the
	// caller to plumb it through. Updated on PairSuccess.
	jidsMu sync.RWMutex
	jids   map[string]string

	// dropCount is incremented every time we drop an event due to a
	// full out channel. Logged periodically so the operator notices.
	dropCount atomic.Uint64
}

// NewBus constructs a Bus. sidecarID is stamped onto every event for
// future sharded mode; pass "default" in single mode.
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

// Subscribe returns the read end of the bus channel. May be called
// exactly once per Bus lifetime; a second call panics, since splitting
// the stream silently between two consumers would lose half the events
// for each.
func (b *Bus) Subscribe() <-chan *pb.StreamEventsResponse {
	if !b.subscribed.CompareAndSwap(false, true) {
		panic("events.Bus.Subscribe called more than once; only one StreamEvents subscriber is supported")
	}
	return b.out
}

// Close shuts the bus down. After Close, Sink is a no-op and the
// subscribed channel is closed (which terminates the gRPC server-stream
// cleanly with EOF).
func (b *Bus) Close() {
	defer func() {
		// out may already be closed if Close is called twice; recover
		// to keep the call idempotent.
		_ = recover()
	}()
	close(b.out)
}

// Sink is the EventSink callback the accounts package attaches to every
// whatsmeow.Client. It is called synchronously from whatsmeow's
// dispatchEvent goroutine, so it must NEVER block.
func (b *Bus) Sink(accountKey string, evt any) {
	resp := b.translate(accountKey, evt)
	if resp == nil {
		return
	}
	// Update cached JID before emit so the outgoing message carries it.
	if ps, ok := evt.(*events.PairSuccess); ok {
		b.setJID(accountKey, ps.ID.String())
		// Re-stamp now that we know the JID.
		resp.AccountJid = ps.ID.String()
	}
	b.emit(resp)
}

// PumpQRChannel reads QRChannelItem values from a whatsmeow QR channel
// and forwards them onto the bus until the channel closes or ctx is
// cancelled. Intended to be launched in its own goroutine immediately
// after Client.GetQRChannel returns (and BEFORE Client.Connect, per
// whatsmeow's pairing protocol).
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

// SetAccountJID lets the accounts package seed the JID cache for accounts
// that are loaded from disk already paired (so the very first event they
// emit after Connect carries account_jid). Optional.
func (b *Bus) SetAccountJID(accountKey string, jid types.JID) {
	if jid.IsEmpty() {
		return
	}
	b.setJID(accountKey, jid.String())
}

// ─────────────────────────────────────────────────────────────────────────────
// Internals
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
		// Channel full. Drop and log. We do NOT block whatsmeow's
		// dispatch goroutine — see package comment.
		n := b.dropCount.Add(1)
		// Log the first drop and every 100th after, to avoid log
		// flooding when the subscriber is gone for a long time.
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

// translate maps a whatsmeow event to a *pb.StreamEventsResponse. Returns
// nil for events we deliberately drop (Phase 1 ignores soft events).
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
			return nil // unsupported message type for Phase 1 (e.g. media-only)
		}
		base.Event = &pb.StreamEventsResponse_Message{Message: me}
		return base

	// Phase 1: explicitly drop these noisy soft-state events. They fire
	// in bursts after pairing (history sync can emit dozens) and Python
	// business code does not care about them in v1. Promoting any of
	// these to first-class proto events is a future protocol bump.
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
		// Phase 1: surface unrecognised events as UnknownEvent so the
		// Python side can see what's flowing without us having to
		// translate every variant up front. Production wheel may
		// switch this to a no-op behind a debug flag.
		base.Event = &pb.StreamEventsResponse_Unknown{
			Unknown: &pb.UnknownEvent{GoType: fmt.Sprintf("%T", evt)},
		}
		return base
	}
}

// qrItemToResp translates a whatsmeow.QRChannelItem (the QR-channel
// element type) into a StreamEventsResponse. Each item's Event field is
// "code" / "success" / "timeout" / "err" per the upstream API.
func (b *Bus) qrItemToResp(accountKey string, item whatsmeow.QRChannelItem) *pb.StreamEventsResponse {
	base := &pb.StreamEventsResponse{AccountKey: accountKey}

	switch item.Event {
	case "code":
		base.Event = &pb.StreamEventsResponse_Qr{
			Qr: &pb.QREvent{
				Code:        item.Code,
				TtlSeconds:  uint32(item.Timeout / time.Second),
			},
		}
		return base

	case "success":
		// Pairing succeeded; whatsmeow will also emit *events.PairSuccess
		// through the regular handler with the JID. We let that path
		// emit the proto PairSuccessEvent so we don't double-deliver.
		return nil

	case "timeout":
		// QR timed out; surface as a Disconnected with reason so the
		// Python side can present a "scan failed, try again" UI.
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

// messageEvent extracts the Phase 1 fields from *events.Message. Returns
// nil if the message has no representable text body (e.g. it's an
// image-only message; Phase 1 doesn't carry media).
func messageEvent(e *events.Message) *pb.MessageEvent {
	text, replyTo := extractText(e)
	if text == "" && replyTo == "" {
		// Pure non-text payload; skip for Phase 1 rather than emit
		// an empty MessageEvent that the Python side would have to
		// special-case.
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

// extractText pulls the displayable text + reply context out of a
// *events.Message, looking in both the simple Conversation field and the
// richer ExtendedTextMessage variant WhatsApp uses for replies, links
// previews, etc.
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

// loggedOutReason renders an *events.LoggedOut into the free-form reason
// string carried in the proto. We avoid leaking whatsmeow's internal
// ConnectFailureReason enum values directly so changes upstream don't
// quietly break our wire format.
func loggedOutReason(e *events.LoggedOut) string {
	if e == nil {
		return "logged_out"
	}
	if e.OnConnect {
		return fmt.Sprintf("logged_out_on_connect:%d", e.Reason)
	}
	return "logged_out"
}

// errClosedBus is returned (currently unused) if a future caller asks
// the bus for confirmation that emission succeeded after Close.
var errClosedBus = errors.New("events: bus closed")
var _ = errClosedBus
