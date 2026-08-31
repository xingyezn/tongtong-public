"""会话管理：每个设备一个 Session。

职责：
  - WS 握手（hello 应答）
  - 音频帧解析（v1/v2/v3）+ 编解码 + 重采样
  - 上行音频缓冲 -> 触发一次 Omni 对话（用户说完一句）
  - 下行 Omni 音频/文本 -> 设备（tts start/audio/tts stop）
  - MCP 工具调用桥接
"""

import asyncio
import binascii
import io
import json
import logging
import struct
import time
import uuid

from .opus_codec import OpusCodec, resample_pcm
from .omni_client import OmniClient

log = logging.getLogger("session")

# 二进制协议版本
BIN_V1 = 1
BIN_V2 = 2
BIN_V3 = 3

# 上行 OPUS 参数（设备 hello 上报）
DEVICE_SAMPLE_RATE = 16000
DEVICE_FRAME_MS = 60


class BinaryProtocolError(Exception):
    pass


def parse_binary_frame(data: bytes, version: int) -> bytes:
    """从设备收到的二进制帧里提取 OPUS payload。

    支持 v1(裸) / v2 / v3（见 websocket.md 第 3 节）。
    """
    if version == BIN_V1:
        return data
    elif version == BIN_V2:
        # uint16 version | uint16 type | uint32 reserved | uint32 timestamp | uint32 payload_size
        if len(data) < 16:
            raise BinaryProtocolError("v2 frame too short")
        fmt = ">HHIII"
        version_f, type_f, reserved, ts, size = struct.unpack_from(fmt, data, 0)
        payload = data[16:16 + size]
        return payload
    elif version == BIN_V3:
        # uint8 type | uint8 reserved | uint16 payload_size
        if len(data) < 4:
            raise BinaryProtocolError("v3 frame too short")
        type_f, reserved, size = struct.unpack_from(">BBH", data, 0)
        payload = data[4:4 + size]
        return payload
    else:
        raise BinaryProtocolError(f"unknown bin version {version}")


def build_binary_frame(payload: bytes, version: int) -> bytes:
    """把 OPUS payload 打包成二进制帧发给设备。"""
    if version == BIN_V1:
        return payload
    elif version == BIN_V2:
        return struct.pack(">HHIIII", version, 0, 0, 0, len(payload), 0) + payload
    elif version == BIN_V3:
        return struct.pack(">BBH", 0, 0, len(payload)) + payload
    raise BinaryProtocolError(f"unknown bin version {version}")


class Session:
    """单设备会话。"""

    def __init__(self, ws, config: dict, omni: OmniClient, device_id: str):
        self.ws = ws
        self.config = config
        self.omni = omni
        self.device_id = device_id
        self.session_id = uuid.uuid4().hex
        self.connected_at = time.time()

        self.bin_version = BIN_V3          # 默认 v3（可由 hello/配置覆盖）
        self.server_sample_rate = config["dashscope"]["output_sample_rate"]

        # 编解码
        self.device_decoder = OpusCodec(DEVICE_SAMPLE_RATE)     # 收设备 16k opus -> pcm
        # pcm -> 24k opus 发给设备。用 64kbps 高码率(默认VoIP仅~14kbps, 音质差有杂音)
        self.device_encoder = OpusCodec(self.server_sample_rate, bitrate=64000)

        # 上行缓冲（当前一句话的 PCM 16k）
        self.up_pcm = bytearray()
        self.up_pcm_max = DEVICE_SAMPLE_RATE * 30 * 2  # 30 秒上限

        # 控制
        self.listening = False
        self.speaking = False
        self.debug_mode = False
        self._mcp = None
        # One supervised virtual-microphone conversation may be active while
        # the device is in debug mode.  It observes the *model* tool calls;
        # it never invokes MCP directly.
        self._e2e_test = None
        self._tools_cached = False
        self.history = []          # 百炼对话历史
        self.max_history = 10
        self.omni_busy = False
        self._latest_call_id = 0
        self._omni_task = None

        # 后端 VAD（静音自动结束）：说话中/静音计时
        self._vad_speech = False
        self._vad_silence_frames = 0
        self._vad_started_at = 0.0
        self._vad_triggered = False            # 已触发 omni 防重复

    # VAD 参数（实时读取 config，可在监控面板动态调整，无需重启）
    def _vad_stop_silence_frames(self) -> int:
        ms = self.config.get("vad", {}).get("silence_duration_ms", 900)
        # 每帧约 60ms，向上取整保证至少 1 帧
        return max(1, ms // DEVICE_FRAME_MS)

    def _vad_energy_threshold(self) -> float:
        return float(self.config.get("vad", {}).get("energy_threshold", 120.0))

    @property
    def mcp(self):
        return self._mcp

    def set_mcp(self, mcp):
        self._mcp = mcp

    # ------------------------------------------------------------------
    # 发送辅助
    # ------------------------------------------------------------------
    async def send_json(self, obj: dict):
        if self.ws is None or self.ws.closed:
            return
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def send_audio_opus(self, opus: bytes):
        frame = build_binary_frame(opus, self.bin_version)
        if self.ws is None or self.ws.closed:
            return
        await self.ws.send_bytes(frame)

    async def send_hello_ack(self):
        await self.send_json({
            "type": "hello",
            "transport": "websocket",
            "session_id": self.session_id,
            "audio_params": {
                "format": "opus",
                "sample_rate": self.server_sample_rate,
                "channels": 1,
                "frame_duration": DEVICE_FRAME_MS,
            },
        })

    # ------------------------------------------------------------------
    # 入站处理
    # ------------------------------------------------------------------
    async def on_text(self, text: str):
        try:
            msg = json.loads(text)
        except Exception:
            log.warning("bad json from device: %.120s", text)
            return
        mtype = msg.get("type")
        if mtype == "hello":
            self._handle_hello(msg)
            await self.send_hello_ack()
        elif mtype == "listen":
            state = msg.get("state")
            log.info("device %s listen state=%s mode=%s", self.device_id, state, msg.get("mode", ""))
            if state == "start":
                self.listening = True
                self.up_pcm.clear()
                self._reset_vad_state()
            elif state == "stop":
                self.listening = False
                log.info("device %s listen stop, up_pcm=%d bytes", self.device_id, len(self.up_pcm))
                self._maybe_start_omni()
            elif state == "detect":
                # 唤醒词命中（声纹前置音频已发），忽略
                log.info("device %s wake word detect: %s", self.device_id, msg.get("text", ""))
                pass
        elif mtype == "abort":
            self.listening = False
            await self._abort_speaking()
        elif mtype == "mcp":
            if self._mcp:
                self._mcp.on_device_mcp(msg.get("payload", {}))
        else:
            log.info("device msg type=%s ignored", mtype)

    def _handle_hello(self, msg: dict):
        version = msg.get("version")
        if version in (1, 2, 3):
            self.bin_version = version
        features = msg.get("features", {})
        self.debug_mode = bool(features.get("debug_mode", False))
        # 设备 hello 上报 sample_rate=16000, frame_duration=60（固定）
        log.info("device %s hello, bin_version=%d, debug_mode=%s, features=%s",
                 self.device_id, self.bin_version, self.debug_mode, features)

    def _reset_vad_state(self):
        self._vad_speech = False
        self._vad_silence_frames = 0
        self._vad_triggered = False
        self._vad_started_at = time.time()

    @staticmethod
    def _rms(pcm: bytes) -> float:
        """计算 16bit PCM 的 RMS 能量。"""
        n = len(pcm) // 2
        if n == 0:
            return 0.0
        total = 0.0
        for i in range(0, n * 2, 2):
            s = int.from_bytes(pcm[i:i + 2], "little", signed=True)
            total += s * s
        return (total / n) ** 0.5

    def _maybe_auto_stop(self):
        """后端 VAD：说话结束后静音足够久 -> 自动触发 omni。
        auto 模式下设备不主动发 listen stop，由服务器端判断。
        """
        if not self.listening or self._vad_triggered or self.omni_busy:
            return
        # 有音频才可能判定结束（避免没说话就触发）
        if not self.up_pcm:
            return
        self._vad_triggered = True
        self.listening = False
        log.info("device %s auto stop by server VAD (%.1fs, %d bytes)",
                 self.device_id, time.time() - self._vad_started_at, len(self.up_pcm))
        self._maybe_start_omni()

    async def on_binary(self, data: bytes):
        if not self.listening:
            return
        try:
            opus = parse_binary_frame(data, self.bin_version)
        except BinaryProtocolError as e:
            log.warning("bin parse error: %s", e)
            return
        try:
            pcm = self.device_decoder.decode(opus, DEVICE_FRAME_MS)
        except Exception as e:
            log.warning("opus decode err: %s", e)
            return
        self.up_pcm.extend(pcm)

        # 后端 VAD：按帧计算能量，跟踪说话/静音状态
        if not self._vad_triggered:
            energy = self._rms(bytes(pcm))
            if energy > self._vad_energy_threshold():
                if not self._vad_speech:
                    self._vad_speech = True
                self._vad_silence_frames = 0
            else:
                if self._vad_speech:
                    self._vad_silence_frames += 1
                    if self._vad_silence_frames >= self._vad_stop_silence_frames():
                        self._maybe_auto_stop()
                        return
                else:
                    # 还没检测到说话：忽略静音，避免长静音误触发
                    pass

        if len(self.up_pcm) > self.up_pcm_max:
            # 防溢出：截断，只留最新
            del self.up_pcm[:len(self.up_pcm) - self.up_pcm_max]

    # ------------------------------------------------------------------
    # 一次 Omni 对话
    # ------------------------------------------------------------------
    def _maybe_start_omni(self):
        """设备 listen stop 时触发：把整句话送去 Omni。

        必须在独立 asyncio task 里跑——Omni 对话过程中可能等待设备
        tools/call 回执（await future），如果阻塞主消息循环，
        设备回执就永远处理不到，造成死锁/超时。
        """
        if not self.up_pcm or self.omni_busy:
            return
        pcm = bytes(self.up_pcm)
        self.up_pcm.clear()
        self.omni_busy = True
        self._omni_task = asyncio.create_task(self._run_omni_turn_task(pcm))

    def begin_e2e_test(self, allowed_tool_names):
        """Create an observer for one virtual-microphone Omni turn."""
        if self._e2e_test is not None:
            raise RuntimeError("an end-to-end voice test is already running")
        future = asyncio.get_running_loop().create_future()
        self._e2e_test = {
            "future": future,
            "tool_calls": [],
            "allowed_tool_names": set(allowed_tool_names),
        }
        return future

    def cancel_e2e_test(self, future):
        if self._e2e_test and self._e2e_test.get("future") is future:
            self._e2e_test = None

    def _finish_e2e_test(self, error=None):
        test = self._e2e_test
        if not test:
            return
        future = test["future"]
        if not future.done():
            future.set_result({"tool_calls": test["tool_calls"], "error": error})
        self._e2e_test = None

    async def _run_omni_turn_task(self, pcm: bytes):
        error = None
        try:
            await self._run_omni_turn(pcm)
        except Exception as exc:
            error = str(exc)
            log.exception("omni turn failed")
        finally:
            self.omni_busy = False
            self._finish_e2e_test(error)

    async def _run_omni_turn(self, pcm: bytes):
        # 无百炼 Key 时走回环模式，验证完整链路（说话→上行→下行→播放）
        if not self.omni.api_key:
            await self._echo_mode(pcm)
            return

        log.info("omni turn: %d bytes PCM (%.1fs)", len(pcm), len(pcm) / DEVICE_SAMPLE_RATE / 2)

        # 音频增量直接切成设备需要的 60ms Opus 帧下发。不要等待
        # response.done，否则首句语音会被整段模型生成时间拖慢。
        stream_state = {"pcm": bytearray(), "started": False}
        text_parts = []

        tools = self._mcp.make_omni_tools() if self._mcp else []
        if self._e2e_test is not None:
            allowed = self._e2e_test["allowed_tool_names"]
            tools = [tool for tool in tools if tool.get("function", {}).get("name") in allowed]
        async for evt in self.omni.chat_stream(
                pcm, tools=tools, tool_handler=self._handle_tool_call):
            et = evt.get("type")
            if et == "error":
                log.error("omni error: %s", evt.get("message"))
                break
            elif et == "text":
                text_parts.append(evt.get("text", ""))
            elif et == "audio":
                await self._stream_omni_audio(evt, stream_state)
            elif et == "done":
                break

        if text_parts:
            log.info("omni reply: %s", "".join(text_parts))
        await self._finish_omni_audio(stream_state)
        log.info("omni turn done, streamed=%s", stream_state["started"])

    async def _echo_mode(self, pcm: bytes):
        """回环模式：把设备上传的音频原样回放，模拟模型输入/输出。

        用于无 DASHSCOPE_API_KEY 时验证完整链路：
          设备说话 -> 上行OPUS -> 服务器解码16k -> 重采样24k -> 编码 -> tts下行 -> 设备播放

        该模式不模拟模型的 function_call，也绝不调用设备 MCP 工具；
        它只能用于验证音频传输和播放，不能作为语音控制测试的结论。
        """
        log.info("=== ECHO 模式：回放 %d 字节 PCM（%.1f 秒）===",
                 len(pcm), len(pcm) / DEVICE_SAMPLE_RATE / 2)

        # 回放音频（16k -> 24k -> opus 下发）
        await self.send_json({"type": "tts", "state": "start"})
        self.speaking = True
        if pcm:
            pcm24 = resample_pcm(pcm, DEVICE_SAMPLE_RATE, self.server_sample_rate)
            frame_bytes = self.server_sample_rate * DEVICE_FRAME_MS // 1000 * 2
            frame_dur = DEVICE_FRAME_MS / 1000.0
            for i in range(0, len(pcm24), frame_bytes):
                chunk = pcm24[i:i + frame_bytes]
                if len(chunk) < frame_bytes:
                    chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
                opus = self.device_encoder.encode(chunk, DEVICE_FRAME_MS)
                await self.send_audio_opus(opus)
                # 按帧节奏发送，给设备解码/播放时间，避免瞬间灌入导致被 tts stop 清空
                await asyncio.sleep(frame_dur)
        await self.send_json({"type": "tts", "state": "stop"})
        self.speaking = False
        log.info("=== ECHO 结束 ===")

    async def _stream_omni_audio(self, evt: dict, state: dict):
        """Append one Realtime PCM delta and immediately send complete frames."""
        import base64 as b64
        audio_b64 = evt.get("audio_b64")
        if not audio_b64:
            return
        try:
            raw = b64.b64decode(audio_b64)
        except Exception as e:
            log.warning("audio b64 decode err: %s", e)
            return

        sample_rate = evt.get("sample_rate", self.server_sample_rate)
        pcm = raw
        rate = sample_rate
        if raw[:4] == b"RIFF":
            from .opus_codec import wav_to_pcm
            try:
                pcm, rate = wav_to_pcm(raw)
            except Exception as e:
                log.warning("wav parse err: %s, treat as pcm", e)

        if rate != self.server_sample_rate:
            pcm = resample_pcm(pcm, rate, self.server_sample_rate)

        state["pcm"].extend(pcm)
        frame_bytes = self.server_sample_rate * DEVICE_FRAME_MS // 1000 * 2
        while len(state["pcm"]) >= frame_bytes:
            chunk = bytes(state["pcm"][:frame_bytes])
            del state["pcm"][:frame_bytes]
            if not state["started"]:
                await self.send_json({"type": "tts", "state": "start"})
                state["started"] = True
                self.speaking = True
            opus = self.device_encoder.encode(chunk, DEVICE_FRAME_MS)
            await self.send_audio_opus(opus)

    async def _finish_omni_audio(self, state: dict):
        """Flush a partial PCM frame and stop device playback exactly once."""
        pcm = state["pcm"]
        if pcm:
            frame_bytes = self.server_sample_rate * DEVICE_FRAME_MS // 1000 * 2
            chunk = bytes(pcm) + b"\x00" * (frame_bytes - len(pcm))
            if not state["started"]:
                await self.send_json({"type": "tts", "state": "start"})
                state["started"] = True
                self.speaking = True
            opus = self.device_encoder.encode(chunk, DEVICE_FRAME_MS)
            await self.send_audio_opus(opus)
            pcm.clear()
        await self.send_json({"type": "tts", "state": "stop"})
        self.speaking = False

    async def _play_sound_pcm_if_needed(self, pcm):
        """（预留）播放系统提示音"""
        pass

    async def _handle_tool_call(self, evt: dict):
        """Omni function_call -> MCP tools/call -> 设备执行 -> 等待回执 -> 返回结果文本。

        返回设备执行结果的 JSON 字符串，供回填 Omni history；
        失败/超时返回 None（调用方不再回填）。
        """
        name = evt.get("name")
        arguments = evt.get("arguments") or {}
        call_id = evt.get("id")
        if not name or not self._mcp:
            return None

        # 先确认工具存在
        tool_names = {t.get("name") for t in self._mcp.tools}
        if name not in tool_names:
            log.warning("Omni 调用未知工具 %s，忽略", name)
            return json.dumps({"error": f"unknown tool {name}"})

        req = self._mcp.make_tools_call(name, arguments, call_id)
        req_id = req["payload"]["id"]

        # 注册 future，等待设备回执（异步，不阻塞事件循环）
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._mcp.register_pending(req_id, fut)

        await self.send_json(req)
        log.info("MCP tools/call -> device: %s(%s)", name, arguments)

        # 等待设备回执，超时 10s（真实设备执行 GPIO 很快，网络往返为主）
        try:
            result = await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            log.warning("设备 tools/call 超时: %s", name)
            return json.dumps({"error": "device timeout"})
        except Exception as e:
            log.warning("设备 tools/call 异常: %s", e)
            return json.dumps({"error": str(e)})

        log.info("设备 tools/call 回执: %s", json.dumps(result)[:300])
        if self._e2e_test is not None:
            self._e2e_test["tool_calls"].append({
                "name": name,
                "arguments": arguments,
                "result": result,
            })
        # result 形如 {"content":[{"type":"text","text":"true"}],"isError":false}
        if isinstance(result, dict) and "content" in result:
            texts = [c.get("text", "") for c in result["content"]
                     if isinstance(c, dict)]
            return json.dumps({"result": texts})
        return json.dumps({"result": result})

    # ------------------------------------------------------------------
    # 打断
    # ------------------------------------------------------------------
    async def _abort_speaking(self):
        # TODO: 取消进行中的 Omni 流式任务
        log.info("abort speaking (TODO: cancel omni stream)")
        await self.send_json({"type": "tts", "state": "stop"})

    async def close(self):
        try:
            await self.send_json({"type": "tts", "state": "stop"})
        except Exception:
            pass
