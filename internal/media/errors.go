// Package media 实现了 sidecar 端的媒体上传 / 下载 RPC 处理逻辑。
//
// 它对 wire 协议本身保持无感（除 *pb.SendMediaInit / *pb.MediaInfo 等 DTO
// 的字段语义外）：所有错误以 sentinel 形式返回，gRPC 服务端层在
// cmd/fastmeow-sidecar 通过 mediaErrToStatus 统一映射成 codes.* 值。
//
// 此文件单独存放 sentinel 错误，便于 sidecar 层做 errors.Is 匹配
// 而不需要导入 handler.go 中的非导出符号。
package media

import "errors"

// 暴露给 gRPC 处理程序的 sentinel 错误。分两类：
//
// 上传校验类（→ INVALID_ARGUMENT）：
//   - ErrMissingInitFrame       SendMedia 第一帧不是 init
//   - ErrUnexpectedInitFrame    SendMedia 后续帧再次出现 init
//   - ErrEmptyClientMsgID       client_msg_id 为空（幂等键必备）
//   - ErrEmptyToJID             to_jid 为空
//   - ErrInvalidJID             to_jid 解析失败
//   - ErrUnsupportedMediaKind   kind 是 UNSPECIFIED 或未来枚举值
//   - ErrMissingMimeType        mime_type 为空（whatsmeow 需要它）
//   - ErrDeclaredLengthMismatch declared_file_length != 实际累积字节数
//
// 资源 / 上限类（→ FAILED_PRECONDITION 或 RESOURCE_EXHAUSTED）：
//   - ErrMediaTooLarge          实际累积字节超过 100 MiB 上限
//
// 下载校验类（→ INVALID_ARGUMENT）：
//   - ErrMissingDownloadInfo    DownloadMediaRequest.media 为空
//                               或缺少必要的 direct_path / media_key 等字段
//
// 注意：whatsmeow 自身的 sentinel（ErrUnknownMediaType / ErrNoURLPresent /
// ErrFileLengthMismatch / ErrInvalidMediaSHA256 / ErrInvalidMediaEncSHA256 /
// ErrInvalidMediaHMAC / ErrTooShortFile）会被 sidecar 层直接 errors.Is 命中
// 并映射；此处不重复定义。
var (
	ErrMissingInitFrame       = errors.New("media: first frame must be init")
	ErrUnexpectedInitFrame    = errors.New("media: init frame must appear once")
	ErrEmptyClientMsgID       = errors.New("media: client_msg_id is required")
	ErrEmptyToJID             = errors.New("media: to_jid is required")
	ErrInvalidJID             = errors.New("media: invalid jid")
	ErrUnsupportedMediaKind   = errors.New("media: unsupported media kind")
	ErrMissingMimeType        = errors.New("media: mime_type is required")
	ErrDeclaredLengthMismatch = errors.New("media: declared length mismatch")
	ErrMediaTooLarge          = errors.New("media: file exceeds 100 MiB limit")
	ErrMissingDownloadInfo    = errors.New("media: download info is incomplete")
)
