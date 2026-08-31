# MCP 网络台架测试

本测试通道绕过麦克风、唤醒词、VAD 与模型，直接完成：

`测试命令行 -> 已认证后端 -> WebSocket -> 设备 MCP 工具 -> JSON-RPC 回包`

它适合验证电机、灯、WebSocket 与 MCP 工具参数。它**不**验证模型是否能从自然语言正确挑选工具；该项应在台架测试通过后另行进行。

## 安全前提

- 仅在经过 Dashboard 登录认证的会话中可用。
- 后端只允许 `self.chassis.*` 与 `self.lamp.*` 工具，不能通过此接口调用升级、配置或其他用户专用工具。
- 测试电机时必须让车轮悬空，并先使用低速、短时参数。

## 操作

1. 当前开发固件在首次启动时默认进入调试测试模式：软件麦克风采集与唤醒词关闭，设备会自动建立并保持 WebSocket。Dashboard 的设备状态应显示“在线”，不需要按触摸键。

2. Dashboard 的“调试测试模式”卡片可切换状态；操作会写入设备本地 NVS，重启后仍然有效。只有此模式下后端才接受直连台架命令；退出该模式后恢复普通语音交互。若已退出调试模式，需要按住板载触摸键或通过语音唤醒一次，才能重新上线并从 Dashboard 启用它。

3. 在开发电脑上查询该设备可测试的工具（密码会隐藏输入）：

   ```bash
   cd backend
   python tools/mcp_bench_test.py --base-url https://your-server.example \
     --device-id <dashboard-device-id>
   ```

4. 以低速测试前进一秒：

   ```bash
   python tools/mcp_bench_test.py --base-url https://your-server.example \
     --device-id <dashboard-device-id> --tool self.chassis.go_forward \
     --arguments '{"speed":30,"duration_ms":1000}'
   ```

5. 返回 JSON 中出现设备的 `result` 即代表后端已收到对应 MCP 回包。遇到异常可直接调用 `self.chassis.stop`。

本功能需要后端版本包含 `/api/test/tools`、`/api/test/mode` 与 `/api/test/mcp` 三个已认证接口。首次上线后，调试模式会维护持久连接，因此不需要每次测试都触摸设备。

## Dashboard 日志筛选

- 日志按设备、MCP、模型、系统、普通请求和周期状态请求分类显示。
- 面板自动刷新产生的 `/api/status` 请求默认隐藏，并在后端使用独立的小容量缓冲区保存；它不会再挤掉设备与 MCP 调试信息。
- 需要排查页面刷新、鉴权或接口访问时，再勾选“普通请求”或“周期状态请求”。
