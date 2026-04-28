// handler_test.go 覆盖 internal/media 中不依赖真实 *whatsmeow.Client
// 网络往返的逻辑：
//
//   - Send: init / kind / jid / mime / 上限 / declared length 等校验路径；
//   - Download: nil chunkWriter / 空 MediaInfo / 缺字段路径；
//   - converter: mediaTypeFromProto / buildMessage / downloadableFromProto
//     的字段映射正确性（含 sticker 不带 caption / audio voice_note 等约束）。
//
// 与 internal/receipts 测试一致：cli == nil 检查被有意放在所有纯参数校验
// 之后，因此用 nil cli 即可触达校验逻辑而不必伪造 whatsmeow.Client。
//
// 真实上传 / 下载的 happy path 通过 scripts/smoke_*.py 用真账号 smoke 验证；
// 这里只做单测层面能复现的部分。
package media

import (
	"context"
	"errors"
	"io"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// ── Send 校验路径 ─────────────────────────────────────────────────────────

func TestSend_NilInit(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, nil, strings.NewReader(""))
	if !errors.Is(err, ErrMissingInitFrame) {
		t.Fatalf("nil init: want ErrMissingInitFrame, got %v", err)
	}
}

func TestSend_EmptyClientMsgID(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey: "a",
		ToJid:      "12345@s.whatsapp.net",
		MimeType:   "image/jpeg",
		Kind:       pb.MediaKind_MEDIA_KIND_IMAGE,
	}, strings.NewReader(""))
	if !errors.Is(err, ErrEmptyClientMsgID) {
		t.Fatalf("want ErrEmptyClientMsgID, got %v", err)
	}
}

func TestSend_EmptyToJID(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey:  "a",
		ClientMsgId: "msg-1",
		MimeType:    "image/jpeg",
		Kind:        pb.MediaKind_MEDIA_KIND_IMAGE,
	}, strings.NewReader(""))
	if !errors.Is(err, ErrEmptyToJID) {
		t.Fatalf("want ErrEmptyToJID, got %v", err)
	}
}

func TestSend_EmptyMimeType(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey:  "a",
		ClientMsgId: "msg-1",
		ToJid:       "12345@s.whatsapp.net",
		Kind:        pb.MediaKind_MEDIA_KIND_IMAGE,
	}, strings.NewReader(""))
	if !errors.Is(err, ErrMissingMimeType) {
		t.Fatalf("want ErrMissingMimeType, got %v", err)
	}
}

func TestSend_DeclaredLengthExceedsLimit(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey:         "a",
		ClientMsgId:        "msg-1",
		ToJid:              "12345@s.whatsapp.net",
		MimeType:           "image/jpeg",
		Kind:               pb.MediaKind_MEDIA_KIND_IMAGE,
		DeclaredFileLength: MaxMediaBytes + 1,
	}, strings.NewReader(""))
	if !errors.Is(err, ErrMediaTooLarge) {
		t.Fatalf("want ErrMediaTooLarge, got %v", err)
	}
}

func TestSend_InvalidJID(t *testing.T) {
	// whatsmeow.ParseJID 极其宽松：没有 '@' 时整段吞为 server，不报错。
	// 真正能让它失败的只有 user 段含异常 '.'/':' 结构。这里用多 dots 触发
	// "unexpected number of dots in JID"。
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey:  "a",
		ClientMsgId: "msg-1",
		ToJid:       "a.b.c@s.whatsapp.net",
		MimeType:    "image/jpeg",
		Kind:        pb.MediaKind_MEDIA_KIND_IMAGE,
	}, strings.NewReader(""))
	if !errors.Is(err, ErrInvalidJID) {
		t.Fatalf("want ErrInvalidJID, got %v", err)
	}
}

func TestSend_UnsupportedKind(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey:  "a",
		ClientMsgId: "msg-1",
		ToJid:       "12345@s.whatsapp.net",
		MimeType:    "image/jpeg",
		Kind:        pb.MediaKind_MEDIA_KIND_UNSPECIFIED,
	}, strings.NewReader(""))
	if !errors.Is(err, ErrUnsupportedMediaKind) {
		t.Fatalf("want ErrUnsupportedMediaKind, got %v", err)
	}
}

func TestSend_NilCli_AfterValidation(t *testing.T) {
	// 所有纯参数校验通过后才会撞到 nil-cli；这条路径用于确认
	// 校验顺序（先 input，后 cli），保证未来重构不会破坏单测能力。
	h := NewHandler(t.TempDir(), 4)
	_, err := h.Send(context.Background(), nil, &pb.SendMediaInit{
		AccountKey:  "a",
		ClientMsgId: "msg-1",
		ToJid:       "12345@s.whatsapp.net",
		MimeType:    "image/jpeg",
		Kind:        pb.MediaKind_MEDIA_KIND_IMAGE,
	}, strings.NewReader(""))
	if err == nil || !strings.Contains(err.Error(), "nil whatsmeow.Client") {
		t.Fatalf("expected nil-cli error after validation, got %v", err)
	}
}

// ── Download 校验路径 ─────────────────────────────────────────────────────

func TestDownload_NilChunkWriter(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	err := h.Download(context.Background(), nil, &pb.MediaInfo{
		Kind:          pb.MediaKind_MEDIA_KIND_IMAGE,
		DirectPath:    "/v/t.enc/p",
		MediaKey:      []byte("k"),
		FileSha256:    []byte("s"),
		FileEncSha256: []byte("e"),
	}, nil)
	if err == nil || !strings.Contains(err.Error(), "nil chunkWriter") {
		t.Fatalf("expected nil-chunkWriter error, got %v", err)
	}
}

func TestDownload_NilMediaInfo(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	err := h.Download(context.Background(), nil, nil, func(_ []byte) error { return nil })
	if !errors.Is(err, ErrMissingDownloadInfo) {
		t.Fatalf("want ErrMissingDownloadInfo, got %v", err)
	}
}

func TestDownload_MissingDirectPath(t *testing.T) {
	h := NewHandler(t.TempDir(), 4)
	err := h.Download(context.Background(), nil, &pb.MediaInfo{
		Kind:          pb.MediaKind_MEDIA_KIND_IMAGE,
		MediaKey:      []byte("k"),
		FileSha256:    []byte("s"),
		FileEncSha256: []byte("e"),
	}, func(_ []byte) error { return nil })
	if !errors.Is(err, ErrMissingDownloadInfo) {
		t.Fatalf("want ErrMissingDownloadInfo (missing direct_path), got %v", err)
	}
}

// ── converter 字段映射 ───────────────────────────────────────────────────

func TestMediaTypeFromProto_AllKinds(t *testing.T) {
	cases := []struct {
		kind pb.MediaKind
		want whatsmeow.MediaType
	}{
		{pb.MediaKind_MEDIA_KIND_IMAGE, whatsmeow.MediaImage},
		{pb.MediaKind_MEDIA_KIND_VIDEO, whatsmeow.MediaVideo},
		{pb.MediaKind_MEDIA_KIND_AUDIO, whatsmeow.MediaAudio},
		{pb.MediaKind_MEDIA_KIND_DOCUMENT, whatsmeow.MediaDocument},
		// sticker 与 image 共享 MediaType（whatsmeow 内部约定）。
		{pb.MediaKind_MEDIA_KIND_STICKER, whatsmeow.MediaImage},
	}
	for _, c := range cases {
		got, err := mediaTypeFromProto(c.kind)
		if err != nil {
			t.Errorf("kind=%v: unexpected err %v", c.kind, err)
			continue
		}
		if got != c.want {
			t.Errorf("kind=%v: got %q want %q", c.kind, got, c.want)
		}
	}
}

func TestMediaTypeFromProto_Unspecified(t *testing.T) {
	_, err := mediaTypeFromProto(pb.MediaKind_MEDIA_KIND_UNSPECIFIED)
	if !errors.Is(err, ErrUnsupportedMediaKind) {
		t.Fatalf("want ErrUnsupportedMediaKind, got %v", err)
	}
}

func TestBuildMessage_Image_WithCaptionThumbnail(t *testing.T) {
	upload := whatsmeow.UploadResponse{
		URL: "https://mmg.whatsapp.net/x", DirectPath: "/v/t.enc/p",
		MediaKey: []byte("k"), FileEncSHA256: []byte("e"),
		FileSHA256: []byte("s"), FileLength: 12345,
	}
	init := &pb.SendMediaInit{
		Kind: pb.MediaKind_MEDIA_KIND_IMAGE, MimeType: "image/jpeg",
		Caption: "hi", ThumbnailJpeg: []byte("thumb"),
	}
	msg, err := buildMessage(init, upload)
	if err != nil {
		t.Fatal(err)
	}
	im := msg.GetImageMessage()
	if im == nil {
		t.Fatal("no ImageMessage")
	}
	if im.GetCaption() != "hi" {
		t.Errorf("caption: got %q", im.GetCaption())
	}
	if string(im.GetJPEGThumbnail()) != "thumb" {
		t.Errorf("thumbnail mismatch")
	}
	if im.GetURL() != upload.URL || im.GetDirectPath() != upload.DirectPath {
		t.Errorf("upload fields not propagated")
	}
}

func TestBuildMessage_Audio_VoiceNote(t *testing.T) {
	upload := whatsmeow.UploadResponse{URL: "u", DirectPath: "/p", MediaKey: []byte("k"),
		FileEncSHA256: []byte("e"), FileSHA256: []byte("s"), FileLength: 10}
	init := &pb.SendMediaInit{
		Kind: pb.MediaKind_MEDIA_KIND_AUDIO, MimeType: "audio/ogg", VoiceNote: true,
	}
	msg, err := buildMessage(init, upload)
	if err != nil {
		t.Fatal(err)
	}
	am := msg.GetAudioMessage()
	if am == nil || !am.GetPTT() {
		t.Fatalf("audio PTT not set: %+v", am)
	}
}

func TestBuildMessage_Sticker_NoCaption(t *testing.T) {
	upload := whatsmeow.UploadResponse{URL: "u", DirectPath: "/p", MediaKey: []byte("k"),
		FileEncSHA256: []byte("e"), FileSHA256: []byte("s"), FileLength: 10}
	init := &pb.SendMediaInit{
		Kind: pb.MediaKind_MEDIA_KIND_STICKER, MimeType: "image/webp",
		Caption: "should-be-ignored",
	}
	msg, err := buildMessage(init, upload)
	if err != nil {
		t.Fatal(err)
	}
	sm := msg.GetStickerMessage()
	if sm == nil {
		t.Fatal("no StickerMessage")
	}
	// 反向断言：StickerMessage 没有 caption 字段，buildMessage 应当忽略 init.caption 而不报错。
	// 这里只确认产物结构合法。
}

func TestBuildMessage_Document_FileName(t *testing.T) {
	upload := whatsmeow.UploadResponse{URL: "u", DirectPath: "/p", MediaKey: []byte("k"),
		FileEncSHA256: []byte("e"), FileSHA256: []byte("s"), FileLength: 10}
	init := &pb.SendMediaInit{
		Kind: pb.MediaKind_MEDIA_KIND_DOCUMENT, MimeType: "application/pdf", FileName: "report.pdf",
	}
	msg, err := buildMessage(init, upload)
	if err != nil {
		t.Fatal(err)
	}
	dm := msg.GetDocumentMessage()
	if dm == nil || dm.GetFileName() != "report.pdf" {
		t.Fatalf("document file_name not propagated: %+v", dm)
	}
}

func TestDownloadableFromProto_Image(t *testing.T) {
	info := &pb.MediaInfo{
		Kind: pb.MediaKind_MEDIA_KIND_IMAGE, MimeType: "image/jpeg",
		DirectPath: "/v/t/p", Url: "https://mmg/x", FileLength: 1234,
		MediaKey: []byte("k"), FileSha256: []byte("s"), FileEncSha256: []byte("e"),
	}
	dl, err := downloadableFromProto(info)
	if err != nil {
		t.Fatal(err)
	}
	im, ok := dl.(*waE2E.ImageMessage)
	if !ok {
		t.Fatalf("expected *waE2E.ImageMessage, got %T", dl)
	}
	// whatsmeow.GetMediaType 通过 proto descriptor name 推 MediaType；
	// 必须确认我们返回的是具体类型而非自定义包装。
	if got := whatsmeow.GetMediaType(im); got != whatsmeow.MediaImage {
		t.Errorf("GetMediaType returned %q (whatsmeow can't recognize)", got)
	}
	if im.GetDirectPath() != "/v/t/p" {
		t.Errorf("direct_path lost: %q", im.GetDirectPath())
	}
}

func TestDownloadableFromProto_AllKinds(t *testing.T) {
	cases := []struct {
		kind pb.MediaKind
		want whatsmeow.MediaType
	}{
		{pb.MediaKind_MEDIA_KIND_IMAGE, whatsmeow.MediaImage},
		{pb.MediaKind_MEDIA_KIND_VIDEO, whatsmeow.MediaVideo},
		{pb.MediaKind_MEDIA_KIND_AUDIO, whatsmeow.MediaAudio},
		{pb.MediaKind_MEDIA_KIND_DOCUMENT, whatsmeow.MediaDocument},
		{pb.MediaKind_MEDIA_KIND_STICKER, whatsmeow.MediaImage},
	}
	for _, c := range cases {
		// 每次循环都新建 MediaInfo —— 不能 deref-copy 共享 base，
		// 因为 *pb.MediaInfo 内嵌 protoimpl.MessageState{sync.Mutex}，
		// 复制锁会触发 go vet "copies lock value"。
		info := &pb.MediaInfo{
			Kind:          c.kind,
			MimeType:      "x/y",
			DirectPath:    "/p",
			MediaKey:      []byte("k"),
			FileSha256:    []byte("s"),
			FileEncSha256: []byte("e"),
		}
		dl, err := downloadableFromProto(info)
		if err != nil {
			t.Errorf("kind=%v unexpected err %v", c.kind, err)
			continue
		}
		if got := whatsmeow.GetMediaType(dl); got != c.want {
			t.Errorf("kind=%v: GetMediaType=%q want %q", c.kind, got, c.want)
		}
	}
}

// ── 工具：确认临时文件清理 ────────────────────────────────────────────────

// readerFromBytes 让校验路径的测试可以传 io.Reader 而不污染 import 列表。
var _ io.Reader = (*strings.Reader)(nil)
