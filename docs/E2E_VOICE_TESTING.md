# 端到端语音与 MCP 自动测试

此测试验证完整的用户语音控制路径：

`测试 WAV -> ESP32 虚拟麦克风 -> ESP32 Opus 上行 -> 后端 VAD -> Qwen-Omni -> 模型 function_call -> 设备 MCP -> JSON-RPC 回包 -> 模型回复`

它不同于 `MCP_BENCH_TESTING.md` 中的直连台架测试：后者跳过模型，只适合诊断 MCP 下半链路；本测试只有在模型实际选择并调用工具时才会通过。

## 前提

- 设备刷入开发固件并显示为 `debug_mode=true`、在线。
- 后端必须配置有效的 `DASHSCOPE_API_KEY`；没有 Key 时只会进入音频回环模式，不能验证模型推理。
- 测试音频为不超过 8 秒的 WAV；脚本会转换为 16 kHz、单声道、16 位 PCM。
- 默认只把灯控工具提供给模型。电机测试须明确加 `--allow-motion`，并先让车轮悬空。

## 示例：安全灯控用例

准备内容为“打开灯”的 WAV 后运行：

```bash
cd backend
python tools/e2e_voice_test.py --base-url https://your-server.example \
  --device-id <dashboard-device-id> --wav open-lamp.wav \
  --expected-tool self.lamp.turn_on
```

返回 `matched: true` 并包含设备 MCP `result`，才表示模型选择、设备执行和回包都成功。

## 电机用例

确认车轮悬空后，才可提供“前进”等 WAV，并显式允许底盘工具：

```bash
python tools/e2e_voice_test.py --base-url https://your-server.example \
  --device-id <dashboard-device-id> --wav forward.wav \
  --expected-tool self.chassis.go_forward --allow-motion
```

Dashboard 日志中应依次出现 `listen state=start`、`auto stop by server VAD`、`omni turn`、`MCP tools/call -> device`、`设备 tools/call 回执` 与 `E2E voice test completed`。
