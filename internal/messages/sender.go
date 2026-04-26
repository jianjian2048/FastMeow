// Package messages implements the outbound side of the gateway: SendText
// for now, with hooks for media types in subsequent milestones. It owns
// the (account_key, client_msg_id) dedup cache that lets the Python SDK
// safely retry a SendMessage RPC across transient transport failures
// without risking double-delivery on WhatsApp.
//
// Dedup semantics
//
// Phase 1 cache:
//   - Key: "<account_key>\x00<client_msg_id>" (NUL-separated to keep
//     account_key and client_msg_id from colliding when they happen to
//     concatenate to the same string).
//   - Value: the successful SendMessageResponse.
//   - Capacity: 10_000 entries, evicted LRU.
//   - TTL: 5 minutes per entry; expired entries are treated as cache
//     misses and re-sent.
//
// Cache scope:
//   - Process-local. A sidecar restart loses the cache; that is
//     acceptable in Phase 1 because Python persists nothing about
//     in-flight sends across crashes either, and a fresh sidecar will
//     re-deliver at most once per Python retry.
//   - Only successful sends are cached. Failed sends are NOT cached so
//     Python's retry actually retries.
//
// We deliberately do NOT cache by message body hash. client_msg_id is
// the only correctness-bearing key — body-hash dedup would silently
// suppress legitimate re-sends of identical content (e.g. an automated
// hourly "OK" reply).
package messages

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	lru "github.com/hashicorp/golang-lru/v2"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// Defaults match the values documented in the proto file.
const (
	DefaultDedupCapacity = 10_000
	DefaultDedupTTL      = 5 * time.Minute
)

// Errors surfaced to gRPC handlers. Wrap with status.Error in the server
// layer to map onto gRPC codes.
var (
	ErrEmptyClientMsgID = errors.New("messages: client_msg_id is required")
	ErrEmptyToJID       = errors.New("messages: to_jid is required")
	ErrEmptyBody        = errors.New("messages: text body is required")
	ErrInvalidJID       = errors.New("messages: invalid jid")
)

// Sender is the entry point for outbound messages. One instance per
// sidecar; safe for concurrent use across all accounts (whatsmeow.Client
// itself serialises sends per-account internally).
type Sender struct {
	dedup *lru.Cache[string, dedupEntry]
	ttl   time.Duration
	now   func() time.Time // overridable for tests
}

type dedupEntry struct {
	resp     *pb.SendMessageResponse
	cachedAt time.Time
}

// NewSender constructs a Sender with the default dedup capacity and TTL.
// Pass capacity<=0 / ttl<=0 to use the defaults.
func NewSender(capacity int, ttl time.Duration) (*Sender, error) {
	if capacity <= 0 {
		capacity = DefaultDedupCapacity
	}
	if ttl <= 0 {
		ttl = DefaultDedupTTL
	}
	cache, err := lru.New[string, dedupEntry](capacity)
	if err != nil {
		return nil, fmt.Errorf("messages: build dedup cache: %w", err)
	}
	return &Sender{
		dedup: cache,
		ttl:   ttl,
		now:   time.Now,
	}, nil
}

// SendText sends a plain-text message via cli to the given recipient.
// The (accountKey, clientMsgID) tuple keys the dedup cache. If a fresh
// successful send for the same tuple is in cache, the cached response
// is returned with deduped=true and the WhatsApp send is skipped.
//
// accountKey is purely for cache scoping; it is NOT validated against
// the cli passed in. Caller (the gRPC server) is responsible for
// supplying matching values.
func (s *Sender) SendText(
	ctx context.Context,
	cli *whatsmeow.Client,
	accountKey string,
	toJID string,
	clientMsgID string,
	body string,
	replyToMessageID string,
) (*pb.SendMessageResponse, error) {
	if cli == nil {
		return nil, errors.New("messages: nil whatsmeow.Client")
	}
	if clientMsgID == "" {
		return nil, ErrEmptyClientMsgID
	}
	if toJID == "" {
		return nil, ErrEmptyToJID
	}
	if body == "" {
		return nil, ErrEmptyBody
	}

	// Dedup cache check.
	key := dedupKey(accountKey, clientMsgID)
	if cached, ok := s.dedup.Get(key); ok {
		if s.now().Sub(cached.cachedAt) <= s.ttl {
			// Clone so callers can mutate without poisoning the cache,
			// and re-stamp deduped=true even if the cached entry was
			// originally returned with false (it always is).
			out := proto.Clone(cached.resp).(*pb.SendMessageResponse)
			out.Deduped = true
			return out, nil
		}
		// Expired; remove and fall through to a real send.
		s.dedup.Remove(key)
	}

	jid, err := types.ParseJID(toJID)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidJID, toJID, err)
	}

	msg := buildTextMessage(body, replyToMessageID)

	// We pass clientMsgID through to whatsmeow as the message ID. This
	// gives us end-to-end idempotency: even if two sidecars in a future
	// HA setup both saw the same retry, WhatsApp's own dedup would
	// collapse them. Using a UUID-shaped clientMsgID is recommended;
	// whatsmeow accepts any non-empty string here.
	extra := whatsmeow.SendRequestExtra{ID: types.MessageID(clientMsgID)}

	resp, err := cli.SendMessage(ctx, jid, msg, extra)
	if err != nil {
		return nil, fmt.Errorf("messages: SendMessage to %s: %w", jid, err)
	}

	out := &pb.SendMessageResponse{
		MessageId:       resp.ID,
		ServerTimestamp: timestamppb.New(resp.Timestamp),
		Deduped:         false,
	}

	s.dedup.Add(key, dedupEntry{resp: out, cachedAt: s.now()})
	return out, nil
}

// buildTextMessage constructs a whatsmeow text-only payload. If replyTo
// is non-empty we wrap it in ExtendedTextMessage to carry the reply
// reference, otherwise the simpler Conversation field is enough. Both
// shapes render identically on the receiver side.
func buildTextMessage(body, replyTo string) *waE2E.Message {
	if replyTo == "" {
		return &waE2E.Message{Conversation: proto.String(body)}
	}
	return &waE2E.Message{
		ExtendedTextMessage: &waE2E.ExtendedTextMessage{
			Text: proto.String(body),
			ContextInfo: &waE2E.ContextInfo{
				StanzaID: proto.String(replyTo),
			},
		},
	}
}

// dedupKey composes the cache key. See package comment for why we use
// NUL-separation.
func dedupKey(accountKey, clientMsgID string) string {
	var b strings.Builder
	b.Grow(len(accountKey) + 1 + len(clientMsgID))
	b.WriteString(accountKey)
	b.WriteByte(0)
	b.WriteString(clientMsgID)
	return b.String()
}
