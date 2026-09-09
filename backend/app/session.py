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
import secrets
import struct
import time
import uuid

from .opus_codec import OpusCodec, resample_pcm
from .omni_client import OmniClient
from .face_service import FaceService
from .mcp_bridge import is_model_tool_visible

log = logging.getLogger("session")

# 二进制协议版本
BIN_V1 = 1
BIN_V2 = 2
BIN_V3 = 3

# 上行 OPUS 参数（设备 hello 上报）
DEVICE_SAMPLE_RATE = 16000
DEVICE_FRAME_MS = 60
# Realtime model audio arrives in uneven network-sized chunks.  A 240 ms
# buffer only protects one chunk, which makes the device run dry whenever the
# next chunk is delayed briefly.  Keep enough audio ahead to absorb normal
# public-network and model scheduling jitter.  The value can be tuned per
# environment with ``audio.tts_startup_buffer_ms``.
TTS_STARTUP_BUFFER_MS = 900
TTS_MIN_STARTUP_BUFFER_MS = 240
TTS_MAX_STARTUP_BUFFER_MS = 1500
AI_DISPLAY_UPDATE_INTERVAL_S = 0.18
DEFAULT_MOTOR_SPEED = 85
DEFAULT_MOTOR_DURATION_MS = 600
# Covers WebSocket transit and the device's decoder/DMA pipeline after the
# model has finished generating its audio stream.
TTS_PLAYBACK_TRANSPORT_MARGIN_S = 0.20
# Device audio is packetized in 60 ms frames; two frames are the closest safe
# representation of the requested 100 ms post-playback discard window.
VAD_POST_PLAYBACK_DISCARD_FRAMES = 2  # 120 ms of simplex speaker tail
VAD_MIN_CONSECUTIVE_SPEECH_FRAMES = 3  # 180 ms of sustained energy
VAD_PREROLL_FRAMES = 5  # keep 300 ms before confirmed speech
CONVERSATION_END_TOOL_NAME = "server.conversation.end"
CONVERSATION_END_TOOL = {
    "type": "function",
    "function": {
        "name": CONVERSATION_END_TOOL_NAME,
        "description": (
            "仅当用户明确表示要结束当前对话、让助手退下或告别时调用。"
            "用户说‘先这样’、‘好的’、‘知道了’等普通结束语，或只是在讨论如何结束对话、引用别人的话时不要调用。"
            "调用成功后仍要给用户一句简短自然的告别回复；系统会等该回复的"
            "音频播放完、文本保存完之后再进入待命状态。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

FACE_TOOL_PREFIX = "server.face."
MOTOR_FALLBACK_COMMANDS = (
    ("self.chassis.turn_left", ("左转", "向左转", "往左转")),
    ("self.chassis.turn_right", ("右转", "向右转", "往右转")),
    ("self.chassis.go_forward", ("前进", "向前走", "往前走", "向前进")),
    ("self.chassis.go_back", ("后退", "向后走", "往后走", "向后退")),
)


def select_local_emotion(text):
    """Mirror the firmware text fallback for chat history labeling."""
    text = str(text or "")
    if any(mark in text for mark in ("？", "?", "吗", "怎么", "为什么")):
        return "thinking"
    if any(mark in text for mark in ("抱歉", "错误", "失败", "无法")):
        return "sad"
    if any(mark in text for mark in ("注意", "小心", "警告")):
        return "surprised"
    return "happy"


def detect_motor_fallback_tool(transcript):
    """Return a deterministic motor tool for an explicit user command.

    This is only a fallback for turns where the model returned no motor
    function call.  Negated phrases are ignored so that requests such as
    “不要前进” cannot move the chassis.
    """
    text = str(transcript or "").strip()
    if not text:
        return None
    for tool_name, phrases in MOTOR_FALLBACK_COMMANDS:
        for phrase in phrases:
            index = text.find(phrase)
            if index < 0:
                continue
            prefix = text[max(0, index - 4):index]
            if any(word in prefix for word in ("不要", "别", "不许", "禁止", "不能")):
                continue
            return tool_name
    return None


FACE_TOOLS = [
    {"type": "function", "function": {"name": "server.face.register_current",
     "description": "仅在用户明确要求录入人脸时调用。调用前必须先向用户确认姓名；确认后重新拍摄当前画面并上传。当前画面必须恰好检测到 1 张人脸，否则停止录入并告知用户。不得使用历史图片。",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "已向用户确认的姓名"},
         "external_id": {"type": "string", "description": "可选的外部编号"},
         "note": {"type": "string", "description": "可选备注"}},
         "required": ["name"]}}},
    {"type": "function", "function": {"name": "server.face.recognize_current",
     "description": "识别当前摄像头画面，判断是否有人脸、当前人数和已登记身份。每次调用都必须重新拍摄当前帧并请求云端服务；用户说‘看看我是谁’、‘都有谁’、‘有几个人’、‘再看一下’、‘重新确认’或类似追问时也必须重新调用。只能依据本次结果回答，绝不能复用、推测或引用历史图片、人数、身份或识别结果。",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "server.face.delete",
     "description": "删除已录入的人脸。只有用户明确提供 face_id 时才能调用；调用后会重新拍摄当前画面并识别，只有当前画面确实识别出该数据库 face_id 才允许删除，否则拒绝删除。禁止查询或猜测 face_id，也不能仅凭历史记录直接删除。",
     "parameters": {"type": "object", "properties": {
         "face_id": {"type": "integer", "description": "用户明确提供的数据库人脸 ID；不能猜测"}},
         "required": ["face_id"]}}},
    {"type": "function", "function": {"name": "server.face.update",
     "description": "修改已录入人脸的姓名、外部编号或备注。只有用户明确提供 face_id 时才能调用，不能查询或猜测 ID。普通资料修改不重新拍照；只有用户明确要求替换照片时才将 replace_image 设为 true，并重新拍摄当前画面。",
     "parameters": {"type": "object", "properties": {
         "face_id": {"type": "integer", "description": "用户明确提供的数据库人脸 ID"},
         "name": {"type": "string", "description": "新的姓名"},
         "external_id": {"type": "string", "description": "新的外部编号"},
         "note": {"type": "string", "description": "新的备注"},
         "replace_image": {"type": "boolean", "description": "是否用当前新拍照片替换样本"}},
         "required": ["face_id"]}}},
]


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

    def __init__(self, ws, config: dict, omni: OmniClient, device_id: str,
                 conversation_memory=None, binding_code=None, turn_recorder=None,
                 conversation_ender=None, conversation_ended_callback=None,
                 camera_photos=None):
        self.ws = ws
        self.config = config
        # Each device gets an isolated Realtime client while its text memory is
        # shared across reconnects for that same device only.
        self.omni = (omni.new_for_session(conversation_memory, config)
                     if hasattr(omni, "new_for_session") else omni)
        self.device_id = device_id
        self.binding_code = binding_code
        self.turn_recorder = turn_recorder
        self.conversation_ender = conversation_ender
        self.conversation_ended_callback = conversation_ended_callback
        self.camera_photos = camera_photos if camera_photos is not None else {}
        self.face_service = FaceService(config)
        self.session_id = uuid.uuid4().hex
        # This token only lives for the current WebSocket connection. It lets
        # the camera upload a photo without firmware storing a dashboard
        # cookie or a long-lived backend secret.
        self.camera_upload_token = secrets.token_urlsafe(24)
        # Camera uploads happen on a separate HTTP request while an MCP tool
        # call is in flight. These fields bridge that upload back to the
        # exact conversation turn that requested it.
        self._camera_capture_context = None
        self._pending_turn_photo_ids = []
        self.connected_at = time.time()

        self.bin_version = BIN_V3          # 默认 v3（可由 hello/配置覆盖）
        self.firmware_version = "?"
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
        self._mcp = None
        self._tools_cached = False
        self._motor_tool_called = False
        self.omni_busy = False
        self._latest_call_id = 0
        self._omni_task = None
        self._suppress_current_audio = False
        self._active_stream_state = None
        self._end_conversation_pending = False
        self._standby_after_response = False
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        # 后端 VAD（静音自动结束）：说话中/静音计时
        self._vad_speech = False
        self._vad_silence_frames = 0
        self._vad_started_at = 0.0
        self._vad_triggered = False            # 已触发 omni 防重复
        self._vad_candidate_frames = 0
        self._vad_max_energy = 0.0
        self._listen_discard_frames = 0
        self._next_listen_discard_frames = 0

    # VAD 参数（实时读取 config，可在监控面板动态调整，无需重启）
    def _vad_stop_silence_frames(self) -> int:
        ms = self.config.get("vad", {}).get("silence_duration_ms", 900)
        # 每帧约 60ms，向上取整保证至少 1 帧
        return max(1, ms // DEVICE_FRAME_MS)

    def _vad_energy_threshold(self) -> float:
        return float(self.config.get("vad", {}).get("energy_threshold", 120.0))

    def _apply_motor_wheel_swap(self, name, arguments):
        """Translate logical wheel commands for crossed motor wiring."""
        if not self.config.get("motor_defaults", {}).get("swap_wheels"):
            return name, arguments
        if name == "self.chassis.turn_left":
            return "self.chassis.turn_right", arguments
        if name == "self.chassis.turn_right":
            return "self.chassis.turn_left", arguments
        return name, arguments

    def _tts_startup_prebuffer_frames(self) -> int:
        """Return the number of 60 ms frames held before starting playback.

        Keeping this configurable allows a LAN deployment to favour response
        latency while the public test/production services favour continuity.
        The lower bound matches the firmware decoder's own prebuffer.
        """
        raw = self.config.get("audio", {}).get(
            "tts_startup_buffer_ms", TTS_STARTUP_BUFFER_MS)
        try:
            buffer_ms = int(raw)
        except (TypeError, ValueError):
            buffer_ms = TTS_STARTUP_BUFFER_MS
        buffer_ms = max(TTS_MIN_STARTUP_BUFFER_MS,
                        min(TTS_MAX_STARTUP_BUFFER_MS, buffer_ms))
        return max(1, (buffer_ms + DEVICE_FRAME_MS - 1) // DEVICE_FRAME_MS)

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
        features = self.config.get("features", {})
        await self.send_json({
            "type": "system",
            "command": "conversation_config",
            "automatic_interrupt": bool(features.get("automatic_interrupt", True)),
            "button_interrupt": bool(features.get("button_interrupt", True)),
            "double_click_end": bool(features.get("double_click_end", True)),
        })

    async def _keepalive_loop(self):
        """Keep idle device channels alive without affecting conversation state."""
        try:
            while True:
                await asyncio.sleep(45)
                await self.send_json({"type": "system", "command": "keepalive"})
        except asyncio.CancelledError:
            pass

    def camera_capabilities(self):
        """Return the per-connection camera upload capability for MCP init."""
        ws_url = self.config.get("server", {}).get("public_ws_url", "")
        if not isinstance(ws_url, str) or not ws_url:
            return {}
        if ws_url.startswith("wss://"):
            http_url = "https://" + ws_url[len("wss://"):]
        elif ws_url.startswith("ws://"):
            http_url = "http://" + ws_url[len("ws://"):]
        else:
            return {}
        base = http_url.rsplit("/", 1)[0] if http_url.endswith("/ws") else http_url.rstrip("/")
        return {"vision": {
            "url": base + "/api/camera/upload",
            "token": self.camera_upload_token,
        }}

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
            if self.binding_code:
                await self.send_json({
                    "type": "tts", "state": "sentence_start",
                    "text": "设备绑定码：{}".format(self.binding_code),
                })
        elif self.binding_code:
            # An unbound device may connect only so the screen can show its
            # one-time binding code. Audio and MCP commands stay disabled.
            log.info("unbound device %s message ignored: %s", self.device_id, mtype)
            return
        elif mtype == "listen":
            state = msg.get("state")
            log.info("device %s listen state=%s mode=%s", self.device_id, state, msg.get("mode", ""))
            if state == "start":
                self.listening = True
                self.up_pcm.clear()
                self._reset_vad_state()
                self._listen_discard_frames = self._next_listen_discard_frames
                self._next_listen_discard_frames = 0
                if self._listen_discard_frames:
                    log.info("device %s suppressing first %d post-playback audio frames",
                             self.device_id, self._listen_discard_frames)
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
            reason = msg.get("reason", "button")
            features = self.config.get("features", {})
            allowed = (features.get("automatic_interrupt", True)
                       if reason in ("wake_word", "wake_word_detected", "automatic")
                       else features.get("button_interrupt", True))
            if allowed:
                await self._abort_speaking()
        elif mtype == "conversation" and msg.get("action") == "end":
            if not self.config.get("features", {}).get("double_click_end", True):
                return
            await self.request_end_conversation()
        elif mtype == "mcp":
            if self._mcp:
                self._mcp.on_device_mcp(msg.get("payload", {}))
        else:
            log.info("device msg type=%s ignored", mtype)

    def _handle_hello(self, msg: dict):
        version = msg.get("version")
        if version in (1, 2, 3):
            self.bin_version = version
        firmware_version = msg.get("firmware_version")
        if isinstance(firmware_version, str) and firmware_version.strip():
            self.firmware_version = firmware_version.strip()[:64]
        features = msg.get("features", {})
        # 设备 hello 上报 sample_rate=16000, frame_duration=60（固定）
        log.info("device %s hello, firmware_version=%s, bin_version=%d, features=%s",
                 self.device_id, self.firmware_version, self.bin_version, features)

    def _reset_vad_state(self):
        self._vad_speech = False
        self._vad_silence_frames = 0
        self._vad_triggered = False
        self._vad_candidate_frames = 0
        self._vad_max_energy = 0.0
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
        if not self.listening or self._vad_triggered:
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
        if self._listen_discard_frames > 0:
            self._listen_discard_frames -= 1
            return
        self.up_pcm.extend(pcm)

        # 后端 VAD：按帧计算能量，跟踪说话/静音状态
        if not self._vad_triggered:
            energy = self._rms(bytes(pcm))
            self._vad_max_energy = max(self._vad_max_energy, energy)
            if energy > self._vad_energy_threshold():
                self._vad_candidate_frames += 1
                if (not self._vad_speech and self._vad_candidate_frames >=
                        VAD_MIN_CONSECUTIVE_SPEECH_FRAMES):
                    self._vad_speech = True
                    log.info("device %s sustained speech confirmed after %d frames",
                             self.device_id, self._vad_candidate_frames)
                if self._vad_speech:
                    self._vad_silence_frames = 0
            else:
                if self._vad_speech:
                    self._vad_silence_frames += 1
                    if self._vad_silence_frames >= self._vad_stop_silence_frames():
                        self._maybe_auto_stop()
                        return
                else:
                    # 还没检测到说话：忽略静音，避免长静音误触发
                    self._vad_candidate_frames = 0

            if not self._vad_speech:
                keep_bytes = len(pcm) * VAD_PREROLL_FRAMES
                if len(self.up_pcm) > keep_bytes:
                    del self.up_pcm[:-keep_bytes]

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

    async def _run_omni_turn_task(self, pcm: bytes):
        try:
            await self._run_omni_turn(pcm)
        except Exception:
            log.exception("omni turn failed")
        finally:
            self.omni_busy = False
            self._active_stream_state = None
            if self._end_conversation_pending:
                self._end_conversation_pending = False
                self.up_pcm.clear()
                await self._enter_standby()
            elif self.up_pcm and (self._vad_triggered or not self.listening):
                # A user may finish speaking while the interrupted response is
                # still completing silently. Submit that buffered utterance now.
                self._maybe_start_omni()

    async def _run_omni_turn(self, pcm: bytes):
        self._suppress_current_audio = False
        self._standby_after_response = False
        self._pending_turn_photo_ids = []
        # 无百炼 Key 时走回环模式，验证完整链路（说话→上行→下行→播放）
        if not self.omni.api_key:
            await self._echo_mode(pcm)
            return

        log.info("omni turn: %d bytes PCM (%.1fs)", len(pcm), len(pcm) / DEVICE_SAMPLE_RATE / 2)

        # 音频增量直接切成设备需要的 60ms Opus 帧下发。不要等待
        # response.done，否则首句语音会被整段模型生成时间拖慢。
        stream_state = {
            "pcm": bytearray(),
            "started": False,
            "started_at": None,
            "audio_duration_s": 0.0,
            "next_frame_send_at": None,
            "model_audio_events": 0,
            "last_model_audio_at": None,
            "max_model_audio_gap_s": 0.0,
            "startup_prebuffer_frames": self._tts_startup_prebuffer_frames(),
        }
        self._active_stream_state = stream_state
        text_parts = []
        input_transcript = ""
        self._motor_tool_called = False
        token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        last_display_update = 0.0
        last_display_text = ""
        response_emotion = None
        response_emotion_source = None

        motor_defaults = self.config.get("motor_defaults", {})
        model_tool_categories = self.config.get("model_tool_categories", {})
        tools = (self._mcp.make_omni_tools(motor_defaults, model_tool_categories)
                 if self._mcp else [])
        if is_model_tool_visible("self.camera.take_photo", model_tool_categories):
            tools.extend(FACE_TOOLS)
        tools.append(CONVERSATION_END_TOOL)
        async for evt in self.omni.chat_stream(
                pcm, tools=tools, tool_handler=self._handle_tool_call):
            et = evt.get("type")
            if et == "error":
                log.error("omni error: %s", evt.get("message"))
                break
            elif et == "text":
                delta = evt.get("text", "")
                text_parts.append(delta)
                now = time.monotonic()
                if delta and now - last_display_update >= AI_DISPLAY_UPDATE_INTERVAL_S:
                    display_text = "".join(text_parts)
                    await self.send_json({
                        "type": "tts", "state": "sentence_start",
                        "text": display_text,
                    })
                    last_display_update = now
                    last_display_text = display_text
            elif et == "input_text":
                input_transcript = evt.get("text", "")
                if input_transcript:
                    log.info("user transcript: %s", input_transcript)
                    await self.send_json({"type": "stt", "text": input_transcript})
            elif et == "emotion":
                emotion = evt.get("emotion")
                if emotion:
                    response_emotion = emotion
                    response_emotion_source = "model"
                    log.info("omni emotion: %s", emotion)
                    await self.send_json({"type": "llm", "emotion": emotion})
            elif et == "usage":
                for key in token_usage:
                    try:
                        token_usage[key] += max(0, int(evt.get(key, 0) or 0))
                    except (TypeError, ValueError):
                        pass
            elif et == "audio":
                if not self._suppress_current_audio:
                    await self._stream_omni_audio(evt, stream_state)
            elif et == "done":
                break

        fallback_tool = (
            detect_motor_fallback_tool(input_transcript)
            if (not self._motor_tool_called and
                is_model_tool_visible("self.chassis.go_forward",
                                      model_tool_categories)) else None)
        if fallback_tool:
            log.warning(
                "model returned no motor MCP call; forcing fallback: %s -> %s",
                input_transcript, fallback_tool)
            await self._handle_tool_call({
                "id": "backend-motor-fallback",
                "name": fallback_tool,
                "arguments": {},
            })

        assistant_text = "".join(text_parts).strip()
        if assistant_text and not response_emotion:
            response_emotion = select_local_emotion(assistant_text)
            response_emotion_source = "local"
        if assistant_text and assistant_text != last_display_text:
            await self.send_json({
                "type": "tts", "state": "sentence_start", "text": assistant_text,
            })
        if assistant_text:
            log.info("omni reply: %s", assistant_text)
        if input_transcript and assistant_text and self.turn_recorder:
            try:
                # Preserve compatibility with integrations that record only
                # the three text fields until the provider exposes usage.
                photo_ids = list(self._pending_turn_photo_ids)
                self._pending_turn_photo_ids = []
                if photo_ids:
                    result = self.turn_recorder(
                        self.device_id, input_transcript, assistant_text,
                        token_usage if any(token_usage.values()) else None,
                        response_emotion, response_emotion_source, photo_ids)
                else:
                    result = (self.turn_recorder(
                        self.device_id, input_transcript, assistant_text, token_usage,
                        response_emotion, response_emotion_source)
                        if any(token_usage.values()) else self.turn_recorder(
                            self.device_id, input_transcript, assistant_text,
                            None, response_emotion, response_emotion_source))
                if result:
                    self._schedule_conversation_summary(
                        result.get("ended_conversation_id")
                        if isinstance(result, dict) else None)
            except Exception:
                log.exception("failed to persist conversation turn for %s", self.device_id)
        await self._finish_omni_audio(stream_state)
        if self._standby_after_response:
            await self._enter_standby()
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
                if self._suppress_current_audio:
                    break
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
        """Append one Realtime PCM delta and send frames with a startup buffer."""
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

        now = time.monotonic()
        last_audio_at = state.get("last_model_audio_at")
        if last_audio_at is not None:
            gap = now - last_audio_at
            state["max_model_audio_gap_s"] = max(
                state.get("max_model_audio_gap_s", 0.0), gap)
            # This is intentionally sampled at INFO: it identifies upstream
            # stalls without dumping audio payloads or device data to logs.
            if gap >= 0.25:
                log.info("omni audio chunk gap %.3fs (device=%s)", gap, self.device_id)
        state["last_model_audio_at"] = now
        state["model_audio_events"] = state.get("model_audio_events", 0) + 1
        state["pcm"].extend(pcm)
        await self._send_buffered_omni_frames(state)

    async def _send_buffered_omni_frames(self, state: dict, final: bool = False):
        """Send complete frames, bursting the startup jitter buffer.

        After the initial burst, deadlines advance from the previous deadline
        instead of from the actual send time.  A late send therefore catches up
        rather than permanently adding scheduler/network jitter to every frame.
        """
        pcm = state["pcm"]
        frame_bytes = self.server_sample_rate * DEVICE_FRAME_MS // 1000 * 2
        if final and pcm and len(pcm) % frame_bytes:
            pcm.extend(b"\x00" * (frame_bytes - len(pcm) % frame_bytes))

        complete_frames = len(pcm) // frame_bytes
        if complete_frames == 0:
            return
        startup_prebuffer_frames = state.get(
            "startup_prebuffer_frames", self._tts_startup_prebuffer_frames())
        if (not state["started"] and not final
                and complete_frames < startup_prebuffer_frames):
            return

        startup_burst = 0
        if not state["started"]:
            await self.send_json({"type": "tts", "state": "start"})
            state["started"] = True
            state["started_at"] = time.monotonic()
            state["next_frame_send_at"] = (
                state["started_at"] + DEVICE_FRAME_MS / 1000.0)
            self.speaking = True
            startup_burst = min(complete_frames, startup_prebuffer_frames)
            log.info(
                "tts playback start: %.2fs buffered (%d frames, device=%s)",
                startup_burst * DEVICE_FRAME_MS / 1000.0,
                startup_burst,
                self.device_id,
            )

        for frame_index in range(complete_frames):
            chunk = bytes(pcm[:frame_bytes])
            del pcm[:frame_bytes]
            next_send_at = state.get("next_frame_send_at")
            if frame_index >= startup_burst and next_send_at is not None:
                delay = next_send_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            opus = self.device_encoder.encode(chunk, DEVICE_FRAME_MS)
            await self.send_audio_opus(opus)
            state["audio_duration_s"] += len(chunk) / (self.server_sample_rate * 2)
            if frame_index >= startup_burst:
                state["next_frame_send_at"] += DEVICE_FRAME_MS / 1000.0

    async def _finish_omni_audio(self, state: dict):
        """Flush a partial PCM frame and stop device playback exactly once."""
        if self._suppress_current_audio:
            state["pcm"].clear()
            self.speaking = False
            return
        await self._send_buffered_omni_frames(state, final=True)

        if state.get("model_audio_events"):
            log.info(
                "omni audio stream: chunks=%d, max_gap=%.3fs, startup_buffer=%.2fs (device=%s)",
                state["model_audio_events"],
                state.get("max_model_audio_gap_s", 0.0),
                state.get("startup_prebuffer_frames", 0) * DEVICE_FRAME_MS / 1000.0,
                self.device_id,
            )

        # The model can emit audio faster than real time. Do not use tts stop
        # as a mere network-stream marker: wait until the amount of PCM already
        # sent could have been played on the device.
        if state["started"] and state["started_at"] is not None:
            elapsed = time.monotonic() - state["started_at"]
            remaining = state["audio_duration_s"] - elapsed
            if remaining > 0:
                delay = remaining + TTS_PLAYBACK_TRANSPORT_MARGIN_S
                log.info("waiting %.2fs before tts stop (audio=%.2fs, elapsed=%.2fs)",
                         delay, state["audio_duration_s"], elapsed)
                await asyncio.sleep(delay)
        self._next_listen_discard_frames = VAD_POST_PLAYBACK_DISCARD_FRAMES
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
        if name == CONVERSATION_END_TOOL_NAME:
            self._standby_after_response = True
            log.info("model requested standby after response: device=%s", self.device_id)
            return json.dumps({
                "accepted": True,
                "instruction": "请给出简短告别回复；播放和保存完成后系统将进入待命。",
            }, ensure_ascii=False)
        if isinstance(name, str) and name.startswith(FACE_TOOL_PREFIX):
            if not is_model_tool_visible(
                    "self.camera.take_photo",
                    self.config.get("model_tool_categories", {})):
                log.warning("model called disabled camera/face tool %s; ignored", name)
                return json.dumps(
                    {"error": "camera category is disabled for the model"},
                    ensure_ascii=False)
            if name == "server.face.list":
                log.warning("blocked model access to face list: device=%s", self.device_id)
                return json.dumps({"error": "face list is not available to the model"},
                                  ensure_ascii=False)
            return await self._handle_face_tool(
                name, arguments, capture_source="conversation")
        if not name or not self._mcp:
            return None

        if not is_model_tool_visible(name,
                                     self.config.get("model_tool_categories", {})):
            log.warning("model called disabled MCP category tool %s; ignored", name)
            return json.dumps({"error": "tool category is disabled for the model"},
                              ensure_ascii=False)

        # 先确认工具存在
        tool_names = {t.get("name") for t in self._mcp.tools}
        if name not in tool_names:
            log.warning("Omni 调用未知工具 %s，忽略", name)
            return json.dumps({"error": f"unknown tool {name}"})

        if isinstance(name, str) and name.startswith("self.chassis."):
            self._motor_tool_called = True
            defaults = self.config.get("motor_defaults", {})
            if "speed" not in arguments:
                arguments["speed"] = int(defaults.get(
                    "speed", DEFAULT_MOTOR_SPEED))
            if "duration_ms" not in arguments:
                arguments["duration_ms"] = int(defaults.get(
                    "duration_ms", DEFAULT_MOTOR_DURATION_MS))

        name, arguments = self._apply_motor_wheel_swap(name, arguments)
        req = self._mcp.make_tools_call(name, arguments, call_id)
        req_id = req["payload"]["id"]

        # 注册 future，等待设备回执（异步，不阻塞事件循环）
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._mcp.register_pending(req_id, fut)

        capture_context = None
        if name == "self.camera.take_photo":
            capture_context = self._begin_camera_capture(
                arguments.get("question", ""), "conversation")

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
        finally:
            if self._camera_capture_context is capture_context:
                self._camera_capture_context = None

        log.info("设备 tools/call 回执: %s", json.dumps(result)[:300])
        # result 形如 {"content":[{"type":"text","text":"true"}],"isError":false}
        if isinstance(result, dict) and "content" in result:
            texts = [c.get("text", "") for c in result["content"]
                     if isinstance(c, dict)]
            return json.dumps({"result": texts})
        return json.dumps({"result": result})

    def _begin_camera_capture(self, question, source):
        context = {
            "question": (question or "").strip()[:1000],
            "source": source,
            "created_at": time.time(),
        }
        self._camera_capture_context = context
        return context

    def claim_camera_upload_context(self, submitted_question=""):
        """Claim the in-flight capture context from the HTTP upload handler."""
        context = self._camera_capture_context
        if context is None:
            return None
        self._camera_capture_context = None
        result = dict(context)
        if submitted_question:
            result["question"] = submitted_question.strip()[:1000]
        return result

    def register_turn_photo(self, photo_id):
        try:
            photo_id = int(photo_id)
        except (TypeError, ValueError):
            return
        if photo_id > 0 and photo_id not in self._pending_turn_photo_ids:
            self._pending_turn_photo_ids.append(photo_id)

    async def _capture_current_photo(self, capture_source="manual", question=None):
        """Trigger a fresh device capture and return the uploaded JPEG."""
        if not self._mcp:
            return None, {"error": "device MCP unavailable"}
        tool_names = {t.get("name") for t in self._mcp.tools}
        if "self.camera.take_photo" not in tool_names:
            return None, {"error": "device camera capture tool unavailable"}
        question = question or "仅上传当前帧供服务器人脸操作，不需要进行视觉回答。"
        capture_context = self._begin_camera_capture(question, capture_source)
        req = self._mcp.make_tools_call(
            "self.camera.take_photo", {"question": question})
        previous_nonce = (self.camera_photos.get(self.device_id) or {}).get("nonce")
        req_id = req["payload"]["id"]
        fut = asyncio.get_event_loop().create_future()
        self._mcp.register_pending(req_id, fut)
        await self.send_json(req)
        try:
            result = await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError:
            return None, {"error": "device camera capture timeout"}
        finally:
            self._mcp._pending_calls.pop(req_id, None)
            if self._camera_capture_context is capture_context:
                self._camera_capture_context = None
        photo = self.camera_photos.get(self.device_id)
        if (not photo or not photo.get("data") or
                photo.get("nonce") == previous_nonce):
            return None, {"error": "fresh camera frame was not uploaded"}
        return photo["data"], {"capture": "fresh", "device_result": result}

    async def _handle_face_tool(self, name, arguments, capture_source="manual"):
        http_session = await self.omni.ensure_session()
        if name == "server.face.list":
            result = await self.face_service.request(http_session, "GET", "/api/faces")
            return json.dumps(result, ensure_ascii=False)
        if name == "server.face.register_current":
            image, capture = await self._capture_current_photo(capture_source)
            if image is None:
                return json.dumps(capture, ensure_ascii=False)
            fields = {key: arguments.get(key) for key in ("name", "external_id", "note")}
            result = await self.face_service.request(
                http_session, "POST", "/api/faces", image=image, fields=fields)
            result["capture"] = "fresh"
            return json.dumps(result, ensure_ascii=False)
        if name == "server.face.recognize_current":
            image, capture = await self._capture_current_photo(capture_source)
            if image is None:
                return json.dumps(capture, ensure_ascii=False)
            result = await self.face_service.request(
                http_session, "POST", "/api/recognize", image=image)
            result["capture"] = "fresh"
            return json.dumps(result, ensure_ascii=False)
        face_id = arguments.get("face_id")
        if not isinstance(face_id, int) or isinstance(face_id, bool):
            return json.dumps({"error": "face_id must be an integer"}, ensure_ascii=False)
        if name == "server.face.delete":
            image, capture = await self._capture_current_photo(capture_source)
            if image is None:
                return json.dumps(capture, ensure_ascii=False)
            recognition = await self.face_service.request(
                http_session, "POST", "/api/recognize", image=image)
            recognized = any(
                isinstance(item, dict) and item.get("id") == face_id
                for item in (recognition.get("faces") or []))
            if not recognized:
                return json.dumps({
                    "deleted": False,
                    "capture": "fresh",
                    "verification": recognition,
                    "error": "当前画面未识别出指定数据库人脸，已拒绝删除",
                }, ensure_ascii=False)
            result = await self.face_service.request(
                http_session, "DELETE", "/api/faces/{}".format(face_id))
            result["capture"] = "fresh"
            result["verified_face_id"] = face_id
            return json.dumps(result, ensure_ascii=False)
        if name == "server.face.update":
            fields = {key: arguments.get(key) for key in ("name", "external_id", "note")
                      if key in arguments}
            image = None
            if arguments.get("replace_image"):
                image, capture = await self._capture_current_photo(capture_source)
                if image is None:
                    return json.dumps(capture, ensure_ascii=False)
            result = await self.face_service.request(
                http_session, "PATCH", "/api/faces/{}".format(face_id),
                image=image, fields=fields if image is not None else None,
                json_body=None if image is not None else fields)
            if image is not None:
                result["capture"] = "fresh"
            return json.dumps(result, ensure_ascii=False)
        return json.dumps({"error": "unknown face tool"}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 打断
    # ------------------------------------------------------------------
    async def _abort_speaking(self):
        log.info("mute speaking; preserve response text: device=%s", self.device_id)
        was_active = self.speaking or self.omni_busy
        if was_active:
            self._suppress_current_audio = True
            self._next_listen_discard_frames = VAD_POST_PLAYBACK_DISCARD_FRAMES
            if self._active_stream_state is not None:
                self._active_stream_state["pcm"].clear()
        await self.send_json({"type": "tts", "state": "stop"})
        self.speaking = False

    async def request_end_conversation(self):
        """End by button/dashboard, preserving an in-flight response's text."""
        self.listening = False
        await self._abort_speaking()
        if self.omni_busy:
            self._end_conversation_pending = True
            return None
        return await self._enter_standby()

    async def _enter_standby(self):
        self._standby_after_response = False
        self._end_conversation_pending = False
        self.listening = False
        self.up_pcm.clear()
        await self.send_json({"type": "system", "command": "standby"})
        return await self._end_current_conversation()

    async def _end_current_conversation(self):
        conversation_id = (self.conversation_ender(self.device_id)
                           if self.conversation_ender else None)
        if hasattr(self.omni, "reset_conversation"):
            await self.omni.reset_conversation()
        self._schedule_conversation_summary(conversation_id)
        return conversation_id

    def _schedule_conversation_summary(self, conversation_id):
        if not conversation_id or not self.conversation_ended_callback:
            return
        try:
            result = self.conversation_ended_callback(conversation_id)
            if hasattr(result, "__await__"):
                asyncio.create_task(result)
        except Exception:
            log.exception("failed to schedule memory summary: conversation=%s",
                          conversation_id)

    async def close(self):
        keepalive = self._keepalive_task
        if keepalive and not keepalive.done():
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
        task = self._omni_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await self.send_json({"type": "tts", "state": "stop"})
        except Exception:
            pass
        try:
            await self.omni.close()
        except Exception:
            pass
        try:
            if self.ws is not None and not self.ws.closed:
                await self.ws.close()
        except Exception:
            pass
