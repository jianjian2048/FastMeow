// converter.go 负责 proto DTO ↔ whatsmeow 内部类型之间的纯函数转换。
//
// 拆分到独立文件有两个好处：
//  1. 转换逻辑全部不依赖 *whatsmeow.Client，可以单测无副作用。
//  2. handler.go 专注 IO + 并发；converter.go 专注字段映射，
//     未来给媒体新增 metadata 字段时无需改动并发或临时文件路径。
package media

import (
	"fmt"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// mediaTypeFromProto 把 proto MediaKind 映射为 whatsmeow.MediaType。
//
// whatsmeow 的 MediaType 是字符串常量（"WhatsApp Image Keys" 等），用作
// HKDF 派生上传加密密钥的 info 段。任何无法识别的 kind（包括 UNSPECIFIED）
// 直接返回 ErrUnsupportedMediaKind —— 静默 fallback 会破坏端到端加密。
func mediaTypeFromProto(kind pb.MediaKind) (whatsmeow.MediaType, error) {
	switch kind {
	case pb.MediaKind_MEDIA_KIND_IMAGE:
		return whatsmeow.MediaImage, nil
	case pb.MediaKind_MEDIA_KIND_VIDEO:
		return whatsmeow.MediaVideo, nil
	case pb.MediaKind_MEDIA_KIND_AUDIO:
		return whatsmeow.MediaAudio, nil
	case pb.MediaKind_MEDIA_KIND_DOCUMENT:
		return whatsmeow.MediaDocument, nil
	case pb.MediaKind_MEDIA_KIND_STICKER:
		// whatsmeow 把 sticker 与 image 共用同一个 MediaType（"WhatsApp Image Keys"），
		// 因为它们走相同的上传路径。区分发生在消息构造阶段 —— 通过填充
		// *waE2E.StickerMessage 而非 *waE2E.ImageMessage。
		return whatsmeow.MediaImage, nil
	default:
		return "", fmt.Errorf("%w: %v", ErrUnsupportedMediaKind, kind)
	}
}

// buildMessage 在上传成功后用 *whatsmeow.UploadResponse 的字段
// 与原始 SendMediaInit 的元数据合成具体的 *waE2E.{Image,Video,Audio,
// Document,Sticker}Message，并装入一个 *waE2E.Message 容器。
//
// 字段约定：
//   - URL / DirectPath / MediaKey / FileSHA256 / FileEncSHA256 / FileLength
//     必须来自 UploadResponse；这些是 WhatsApp 服务端凭以下发给接收方
//     的「凭据」。
//   - Mimetype / FileName / Caption / JPEGThumbnail 来自客户端 init。
//   - VoiceNote 仅对 Audio 有效（PTT 字段）；对其它 kind 静默忽略。
//   - StickerMessage 不接受 Caption / JPEGThumbnail（whatsapp 协议如此）。
func buildMessage(init *pb.SendMediaInit, upload whatsmeow.UploadResponse) (*waE2E.Message, error) {
	switch init.GetKind() {
	case pb.MediaKind_MEDIA_KIND_IMAGE:
		m := &waE2E.ImageMessage{
			URL:           proto.String(upload.URL),
			DirectPath:    proto.String(upload.DirectPath),
			MediaKey:      upload.MediaKey,
			Mimetype:      proto.String(init.GetMimeType()),
			FileEncSHA256: upload.FileEncSHA256,
			FileSHA256:    upload.FileSHA256,
			FileLength:    proto.Uint64(upload.FileLength),
		}
		if cap := init.GetCaption(); cap != "" {
			m.Caption = proto.String(cap)
		}
		if thumb := init.GetThumbnailJpeg(); len(thumb) > 0 {
			m.JPEGThumbnail = thumb
		}
		return &waE2E.Message{ImageMessage: m}, nil

	case pb.MediaKind_MEDIA_KIND_VIDEO:
		m := &waE2E.VideoMessage{
			URL:           proto.String(upload.URL),
			DirectPath:    proto.String(upload.DirectPath),
			MediaKey:      upload.MediaKey,
			Mimetype:      proto.String(init.GetMimeType()),
			FileEncSHA256: upload.FileEncSHA256,
			FileSHA256:    upload.FileSHA256,
			FileLength:    proto.Uint64(upload.FileLength),
		}
		if cap := init.GetCaption(); cap != "" {
			m.Caption = proto.String(cap)
		}
		if thumb := init.GetThumbnailJpeg(); len(thumb) > 0 {
			m.JPEGThumbnail = thumb
		}
		return &waE2E.Message{VideoMessage: m}, nil

	case pb.MediaKind_MEDIA_KIND_AUDIO:
		m := &waE2E.AudioMessage{
			URL:           proto.String(upload.URL),
			DirectPath:    proto.String(upload.DirectPath),
			MediaKey:      upload.MediaKey,
			Mimetype:      proto.String(init.GetMimeType()),
			FileEncSHA256: upload.FileEncSHA256,
			FileSHA256:    upload.FileSHA256,
			FileLength:    proto.Uint64(upload.FileLength),
			PTT:           proto.Bool(init.GetVoiceNote()),
		}
		return &waE2E.Message{AudioMessage: m}, nil

	case pb.MediaKind_MEDIA_KIND_DOCUMENT:
		m := &waE2E.DocumentMessage{
			URL:           proto.String(upload.URL),
			DirectPath:    proto.String(upload.DirectPath),
			MediaKey:      upload.MediaKey,
			Mimetype:      proto.String(init.GetMimeType()),
			FileEncSHA256: upload.FileEncSHA256,
			FileSHA256:    upload.FileSHA256,
			FileLength:    proto.Uint64(upload.FileLength),
		}
		if name := init.GetFileName(); name != "" {
			m.FileName = proto.String(name)
		}
		if cap := init.GetCaption(); cap != "" {
			m.Caption = proto.String(cap)
		}
		return &waE2E.Message{DocumentMessage: m}, nil

	case pb.MediaKind_MEDIA_KIND_STICKER:
		m := &waE2E.StickerMessage{
			URL:           proto.String(upload.URL),
			DirectPath:    proto.String(upload.DirectPath),
			MediaKey:      upload.MediaKey,
			Mimetype:      proto.String(init.GetMimeType()),
			FileEncSHA256: upload.FileEncSHA256,
			FileSHA256:    upload.FileSHA256,
			FileLength:    proto.Uint64(upload.FileLength),
		}
		return &waE2E.Message{StickerMessage: m}, nil

	default:
		return nil, fmt.Errorf("%w: %v", ErrUnsupportedMediaKind, init.GetKind())
	}
}

// downloadableFromProto 把 *pb.MediaInfo 还原为具体的
// *waE2E.{Image,Video,Audio,Document,Sticker}Message 指针。
//
// 必须返回**具体类型**而非自定义 wrapper：whatsmeow.GetMediaType
// 通过 proto descriptor 的 message name 推断 MediaType，自定义 wrapper
// 会拿到空 MediaType 导致解密失败。这是 plan §4.5 sketch 中
// 隐含未写出的关键约束。
//
// 必填字段校验：DirectPath、MediaKey、FileSHA256、FileEncSHA256 任一缺失
// 都直接返回 ErrMissingDownloadInfo（whatsmeow 内部也会校验，但提前拦截
// 能给客户端更清晰的错误）。URL 不强制要求 —— whatsmeow 优先用 DirectPath。
func downloadableFromProto(info *pb.MediaInfo) (whatsmeow.DownloadableMessage, error) {
	if info == nil {
		return nil, ErrMissingDownloadInfo
	}
	if info.GetDirectPath() == "" ||
		len(info.GetMediaKey()) == 0 ||
		len(info.GetFileSha256()) == 0 ||
		len(info.GetFileEncSha256()) == 0 {
		return nil, ErrMissingDownloadInfo
	}

	mime := proto.String(info.GetMimeType())
	length := proto.Uint64(info.GetFileLength())
	directPath := proto.String(info.GetDirectPath())
	url := proto.String(info.GetUrl())

	switch info.GetKind() {
	case pb.MediaKind_MEDIA_KIND_IMAGE:
		return &waE2E.ImageMessage{
			URL:           url,
			DirectPath:    directPath,
			MediaKey:      info.GetMediaKey(),
			Mimetype:      mime,
			FileSHA256:    info.GetFileSha256(),
			FileEncSHA256: info.GetFileEncSha256(),
			FileLength:    length,
		}, nil
	case pb.MediaKind_MEDIA_KIND_VIDEO:
		return &waE2E.VideoMessage{
			URL:           url,
			DirectPath:    directPath,
			MediaKey:      info.GetMediaKey(),
			Mimetype:      mime,
			FileSHA256:    info.GetFileSha256(),
			FileEncSHA256: info.GetFileEncSha256(),
			FileLength:    length,
		}, nil
	case pb.MediaKind_MEDIA_KIND_AUDIO:
		return &waE2E.AudioMessage{
			URL:           url,
			DirectPath:    directPath,
			MediaKey:      info.GetMediaKey(),
			Mimetype:      mime,
			FileSHA256:    info.GetFileSha256(),
			FileEncSHA256: info.GetFileEncSha256(),
			FileLength:    length,
		}, nil
	case pb.MediaKind_MEDIA_KIND_DOCUMENT:
		return &waE2E.DocumentMessage{
			URL:           url,
			DirectPath:    directPath,
			MediaKey:      info.GetMediaKey(),
			Mimetype:      mime,
			FileSHA256:    info.GetFileSha256(),
			FileEncSHA256: info.GetFileEncSha256(),
			FileLength:    length,
		}, nil
	case pb.MediaKind_MEDIA_KIND_STICKER:
		return &waE2E.StickerMessage{
			URL:           url,
			DirectPath:    directPath,
			MediaKey:      info.GetMediaKey(),
			Mimetype:      mime,
			FileSHA256:    info.GetFileSha256(),
			FileEncSHA256: info.GetFileEncSha256(),
			FileLength:    length,
		}, nil
	default:
		return nil, fmt.Errorf("%w: %v", ErrUnsupportedMediaKind, info.GetKind())
	}
}
