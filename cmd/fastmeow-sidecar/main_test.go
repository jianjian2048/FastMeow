// main_test.go：cmd/fastmeow-sidecar 包的轻量单元测试。
//
// 这里只测纯函数（如 isSoftEvent）；涉及 gRPC server 启动、Listener 选择、
// auth-token 握手等的集成路径由 Python 端 conftest 拉起 sidecar 真实
// 进程进行 e2e 验证。
package main

import (
	"testing"

	pb "github.com/jianjian2048/fastmeow/gen/go/fastmeow/v1"
)

// TestIsSoftEvent 校验 Phase 4.2 引入的 soft-event 判别函数：
// Receipt / Presence / ChatPresence 必须返回 true（默认不递送，需
// 调用方在 StreamEventsRequest.include_soft_events 显式 opt-in）；
// 其它已知事件 / nil oneof / Unknown 必须返回 false。
func TestIsSoftEvent(t *testing.T) {
	cases := []struct {
		name string
		evt  *pb.StreamEventsResponse
		want bool
	}{
		{
			name: "receipt is soft",
			evt: &pb.StreamEventsResponse{
				Event: &pb.StreamEventsResponse_Receipt{Receipt: &pb.ReceiptEvent{}},
			},
			want: true,
		},
		{
			name: "presence is soft",
			evt: &pb.StreamEventsResponse{
				Event: &pb.StreamEventsResponse_Presence{Presence: &pb.PresenceEvent{}},
			},
			want: true,
		},
		{
			name: "chat_presence is soft",
			evt: &pb.StreamEventsResponse{
				Event: &pb.StreamEventsResponse_ChatPresence{ChatPresence: &pb.ChatPresenceEvent{}},
			},
			want: true,
		},
		{
			name: "message is hard",
			evt: &pb.StreamEventsResponse{
				Event: &pb.StreamEventsResponse_Message{Message: &pb.MessageEvent{}},
			},
			want: false,
		},
		{
			name: "connected is hard",
			evt: &pb.StreamEventsResponse{
				Event: &pb.StreamEventsResponse_Connected{Connected: &pb.ConnectedEvent{}},
			},
			want: false,
		},
		{
			name: "unknown is hard (deliver so Python sees what's flowing)",
			evt: &pb.StreamEventsResponse{
				Event: &pb.StreamEventsResponse_Unknown{Unknown: &pb.UnknownEvent{GoType: "foo"}},
			},
			want: false,
		},
		{
			// nil oneof 走 default 分支 —— 防御性兜底，不应被识别为 soft。
			name: "nil oneof is hard",
			evt:  &pb.StreamEventsResponse{},
			want: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isSoftEvent(tc.evt); got != tc.want {
				t.Errorf("isSoftEvent: got %v want %v", got, tc.want)
			}
		})
	}
}
