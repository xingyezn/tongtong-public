"""阿里云百炼 Qwen3.5-Omni-Realtime 实时语音客户端（WebSocket）。

端到端语音对话：设备音频进 -> 模型音频出（省掉 ASR + TTS）。
使用 DashScope Realtime API（OpenAI Realtime 兼容协议）：
  - wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=...
  - 客户端事件: session.update / input_audio_buffer.append / input_audio_buffer.commit / response.create
  - 服务端事件: response.audio.delta(音频) / response.audio_transcript.delta(文本) / response.done
"""

import asyncio
import base64
import json
import re
import logging
import time
from typing import Optional

import aiohttp

from .mcp_bridge import normalize_model_tool_categories
from .music_service import MUSIC_TOOL_INSTRUCTIONS

log = logging.getLogger("omni")
model_debug_log = logging.getLogger("model_debug")

LANGUAGE_PROMPTS = {
    "auto": "Detect the user's language and reply in the same language.",
    "zh": "始终使用中文（普通话）理解并回答用户。",
    "en": "Always understand and reply in English.",
    "fr": "Comprends et réponds toujours en français.",
    "de": "Verstehe und antworte immer auf Deutsch.",
    "ru": "Всегда понимай и отвечай на русском языке.",
    "it": "Comprendi e rispondi sempre in italiano.",
    "es": "Comprende y responde siempre en español.",
    "pt": "Entenda e responda sempre em português.",
    "ja": "常に日本語で理解し、日本語で回答してください。",
    "ko": "항상 한국어로 이해하고 한국어로 답변하세요.",
    "th": "โปรดทำความเข้าใจและตอบเป็นภาษาไทยเสมอ",
    "id": "Selalu pahami dan jawab dalam bahasa Indonesia.",
    "ar": "افهم المستخدم وأجب دائمًا باللغة العربية.",
    "vi": "Luôn hiểu và trả lời bằng tiếng Việt.",
    "tr": "Her zaman Türkçe anlayın ve yanıtlayın.",
    "fi": "Ymmärrä ja vastaa aina suomeksi.",
    "pl": "Zawsze rozumiej i odpowiadaj po polsku.",
    "hi": "उपयोगकर्ता को हमेशा हिंदी में समझें और उत्तर दें।",
    "nl": "Begrijp en antwoord altijd in het Nederlands.",
    "cs": "Vždy rozuměj a odpovídej česky.",
    "ur": "صارف کو ہمیشہ اردو میں سمجھیں اور جواب دیں۔",
    "fil": "Laging umunawa at sumagot sa Tagalog.",
    "sv": "Förstå och svara alltid på svenska.",
    "da": "Forstå og svar altid på dansk.",
    "he": "הבן את המשתמש ותמיד השב בעברית.",
    "is": "Skildu og svaraðu alltaf á íslensku.",
    "ms": "Sentiasa fahami dan jawab dalam Bahasa Melayu.",
    "no": "Forstå og svar alltid på norsk.",
    "fa": "همیشه کاربر را به فارسی درک کن و پاسخ بده.",
}
# The auxiliary qwen3-asr-flash-realtime transcript is narrower than the
# Qwen3.5-Omni speech-output language set. For the remaining languages, omit
# the hint and let ASR auto-detect instead of sending an unsupported value.
TRANSCRIPTION_LANGUAGE_CODES = {
    "zh", "en", "fr", "de", "ru", "it", "es", "pt", "ja", "ko", "th",
    "id", "ar", "vi", "tr", "fi", "pl", "hi", "cs", "fil", "sv", "da",
    "is", "ms", "no",
}
MAX_CONVERSATION_HISTORY_TURNS = 20

DEFAULT_TOOL_INSTRUCTIONS = (
    "实时视觉规则：涉及摄像头当前画面、当前人数、是否有人、‘看看我是谁’、"
    "‘都有谁’、‘有几个人’、人脸身份或人脸位置，以及‘再看一下’、‘重新看’、"
    "‘重新确认’、‘你确定吗’等追问时，必须优先调用云端工具 server.face.recognize_current。"
    "该工具会重新拍摄当前帧并调用云端人脸识别服务；每次提问都必须重新调用，"
    "不得使用、推测或复用历史对话中的图片、工具结果、人数或身份，必须以本次新结果回答。"
)

FACE_TOOL_INSTRUCTIONS = (
    "人脸管理工具规则：server.face.register_current 仅用于用户明确要求的录入，"
    "调用前必须确认姓名，并重新拍摄当前画面；画面必须恰好有 1 张人脸，否则停止录入。"
    "server.face.recognize_current 用于判断当前画面是否有人脸、人数和已登记身份；"
    "‘看看我是谁’、‘都有谁’、‘有几个人’、‘看下当前画面’及‘再看/重新确认’等每次都必须调用。"
    "所有当前画面结果都必须来自本次新拍摄，严禁复用历史图片、历史识别结果或对话记忆。"
    "server.face.delete 只有在用户明确提供 face_id 后才能调用；它会重新拍摄并验证当前画面确实识别出该数据库人脸，"
    "验证失败不得删除。server.face.update 只有用户明确提供 face_id 时才能调用；修改文字资料不拍照，"
    "仅当用户明确要求替换照片时才使用 replace_image。禁止查询或暴露人脸列表、其他用户的人脸信息，"
    "也不得猜测 face_id。这些工具是服务器端人脸接口的实际调用，不要只根据记忆直接回答。"
)

WEATHER_TIME_TOOL_INSTRUCTIONS = (
    "天气和时间工具规则：当用户询问天气时，必须调用 server.weather.get，"
    "不得凭记忆猜测实时天气；调用前必须使用用户明确提供的城市或地区。"
    "当用户询问当前日期、时间或星期时，必须调用 server.time.now，"
    "默认使用 Asia/Shanghai，用户指定其他地区或时区时使用对应 IANA 时区。"
    "拿到工具结果后，用简洁自然的中文回答；如果未来7天有节假日、调休、传统节日或节气，必须一并报出。"
)


def motor_tool_instructions(config):
    defaults = config.get("motor_defaults", {})
    speed = defaults.get("speed", 85)
    duration_ms = defaults.get("duration_ms", 600)
    return (
        "终端动作规则：涉及前进、后退、左转或右转等底盘运动时，必须调用对应 MCP 工具，让终端完成动作；"
        "禁止只用文字回复或复用历史动作结果。不要把这些指令解释为需要用户直接控制电机。"
        "自然语言指令与工具的固定对应关系为：‘前进’调用 self.chassis.go_forward；"
        "‘后退’调用 self.chassis.go_back；‘左转’调用 self.chassis.turn_left；"
        "‘右转’调用 self.chassis.turn_right。"
        "识别到这些指令后，直接调用对应工具，不要来回确认。速度和持续时间由后端持久化配置统一注入，"
        "当前默认速度为 {}、默认持续时间为 {}ms；模型不需要理解、生成或修改这些底层参数。"
        "动作完成后再简短告知结果。"
    ).format(speed, duration_ms)


EMOTION_INSTRUCTIONS = (
    "表情输出规则：每次助手回复都必须选择一个与助手本轮回复语义匹配的表情，"
    "并在回复末尾附加机器可解析的 JSON 字段，格式必须严格为 "
    "{\\\"emotion\\\":\\\"happy\\\"}。"
    '实际输出示例：{"emotion":"happy"}。'
    "emotion 只能使用 neutral、happy、laughing、funny、sad、angry、"
    "crying、loving、embarrassed、surprised、shocked、thinking、winking、"
    "cool、relaxed、delicious、kissy、confident 之一。"
    "表情必须根据助手本轮实际回答选择，不得依据用户语音转写中的情绪字段，"
    "也不得复用上一轮表情；普通问候和积极回答使用 happy，疑问或思考使用 thinking，"
    "明确的错误、遗憾或安慰场景才使用 sad。不要把 JSON 字段读给用户。"
)

DEFAULT_GLOBAL_TOOL_INSTRUCTIONS = "\n\n".join((
    DEFAULT_TOOL_INSTRUCTIONS,
    motor_tool_instructions({"motor_defaults": {"speed": 85,
                                                "duration_ms": 600}}),
    FACE_TOOL_INSTRUCTIONS,
    WEATHER_TIME_TOOL_INSTRUCTIONS,
    EMOTION_INSTRUCTIONS,
))

GLOBAL_TOOL_RULE_CATEGORIES = {"general", "chassis", "camera", "gimbal_servo", "rgb_led"}


def default_global_tool_rules():
    return [
        {"id": "weather-time", "name": "天气和时间工具规则", "category": "general",
         "enabled": True, "content": WEATHER_TIME_TOOL_INSTRUCTIONS},
        {"id": "visual", "name": "实时视觉规则", "category": "camera",
         "enabled": True, "content": DEFAULT_TOOL_INSTRUCTIONS},
        {"id": "chassis", "name": "底盘运动规则", "category": "chassis",
         "enabled": True, "content": motor_tool_instructions({"motor_defaults": {"speed": 85, "duration_ms": 600}})},
        {"id": "face", "name": "人脸管理规则", "category": "camera",
         "enabled": True, "content": FACE_TOOL_INSTRUCTIONS},
        {"id": "emotion", "name": "表情输出规则", "category": "general",
         "enabled": True, "content": EMOTION_INSTRUCTIONS},
    ]


def compose_global_tool_instructions(rules, categories=None):
    enabled_categories = normalize_model_tool_categories(categories)
    result = []
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("enabled"):
            continue
        category = rule.get("category", "general")
        if category == "chassis" and not enabled_categories["chassis"]:
            continue
        if category == "camera" and not enabled_categories["camera"]:
            continue
        if category == "gimbal_servo" and not enabled_categories["gimbal_servo"]:
            continue
        if category == "rgb_led":
            continue
        content = str(rule.get("content", "")).strip()
        if content:
            result.append(content)
    return "\n\n".join(result)


def normalize_global_tool_rules(value, legacy=""):
    if (isinstance(value, list) and len(value) == 1 and
            isinstance(value[0], dict) and value[0].get("id") == "legacy"):
        legacy = value[0].get("content", legacy)
        value = None
    if isinstance(value, list):
        normalized = []
        for index, rule in enumerate(value):
            if not isinstance(rule, dict):
                continue
            category = rule.get("category", "general")
            if category not in GLOBAL_TOOL_RULE_CATEGORIES:
                category = "general"
            content = str(rule.get("content", "")).strip()
            if not content:
                continue
            normalized.append({
                "id": str(rule.get("id") or "rule-{}".format(index + 1)),
                "name": str(rule.get("name") or "规则 {}".format(index + 1)),
                "category": category,
                "enabled": bool(rule.get("enabled", True)),
                "content": content,
            })
        if normalized:
            return normalized
    if legacy and legacy.strip():
        rules = []
        for index, content in enumerate(legacy.split("\n\n")):
            content = content.strip()
            if not content:
                continue
            if "self.chassis." in content:
                rule_id, name, category = "chassis", "底盘运动规则", "chassis"
            elif "server.face." in content or "face" in content.lower():
                rule_id, name, category = "face", "人脸管理规则", "camera"
            elif "emotion" in content.lower() or "表情" in content:
                rule_id, name, category = "emotion", "表情输出规则", "general"
            else:
                rule_id, name, category = "visual", "实时视觉规则", "camera"
            if any(rule.get("id") == rule_id for rule in rules):
                rule_id = "legacy-{}".format(index + 1)
            rules.append({"id": rule_id, "name": name, "category": category,
                          "enabled": True, "content": content})
        if rules:
            return rules
    return default_global_tool_rules()


def filter_tool_instructions_for_categories(instructions, categories):
    """Remove rules for model-hidden device capability categories.

    The administrator keeps one shared rule editor.  Filtering at request
    time lets each device use its own enabled categories without mutating that
    shared text for every other device.
    """
    enabled = normalize_model_tool_categories(categories)
    blocked_markers = []
    if not enabled["chassis"]:
        blocked_markers.extend(("self.chassis.", "终端动作规则", "电机工具规则"))
    if not enabled["camera"]:
        # The server-side face functions always capture a fresh camera frame,
        # so their visual rules belong to the camera category as well.
        blocked_markers.extend(("server.face.", "实时视觉规则", "人脸工具规则",
                                "人脸管理工具规则"))
    if not enabled["backend"]:
        blocked_markers.extend(("server.weather.", "server.time.", "server.music.",
                                "Backend music tools:"))
    if not blocked_markers:
        return instructions
    paragraphs = str(instructions or "").split("\n\n")
    return "\n\n".join(
        paragraph for paragraph in paragraphs
        if not any(marker in paragraph for marker in blocked_markers)
    )


class _PersistentRealtimeContext:
    """Reuse one Realtime WebSocket for the lifetime of a device session."""

    def __init__(self, owner, http_session, url, headers):
        self.owner = owner
        self.http_session = http_session
        self.url = url
        self.headers = headers

    async def __aenter__(self):
        now = asyncio.get_running_loop().time()
        ws = self.owner._ws
        expired = (
            ws is not None
            and self.owner._last_activity > 0
            and now - self.owner._last_activity > self.owner.conversation_timeout
        )
        reusable = (
            ws is not None
            and not getattr(ws, "closed", False)
            and not expired
            and self.owner._ws_url == self.url
        )
        if not reusable:
            await self.owner._reset_realtime()
            connection = self.http_session.ws_connect(
                self.url, headers=self.headers, timeout=120,
                max_msg_size=64 * 1024 * 1024, heartbeat=30)
            # aiohttp returns an awaitable request context manager; lightweight
            # test doubles may return the websocket directly.
            ws = await connection if hasattr(connection, "__await__") else connection
            self.owner._ws = ws
            self.owner._ws_url = self.url
            self.owner._connection_reused = False
            log.info("omni: connected persistent realtime conversation")
        else:
            self.owner._connection_reused = True
            log.info("omni: reusing persistent realtime conversation")
        self.owner._last_activity = now
        return ws

    async def __aexit__(self, exc_type, exc, tb):
        self.owner._last_activity = asyncio.get_running_loop().time()
        # A clean response.done leaves the socket ready for the next turn.
        # Transport/protocol failures discard it so the next turn reconnects.
        if exc_type is not None or getattr(self.owner._ws, "closed", False):
            await self.owner._reset_realtime()
        return False


class OmniClient:
    """百炼 Qwen3.5-Omni-Realtime 实时语音客户端。"""

    def __init__(self, config: dict, audio_codec=None, conversation_memory=None):
        self.config = config
        self.api_key = config["dashscope"]["api_key"]
        self.output_rate = config["dashscope"]["output_sample_rate"]   # 24000
        self.input_rate = config["dashscope"]["input_sample_rate"]     # 16000
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws = None
        self._ws_url = None
        self._last_activity = 0.0
        self._event_seq = 0
        self._connection_reused = False
        self._conversation_memory = conversation_memory if conversation_memory is not None else {
            "turns": [],
            "last_activity": 0.0,
        }

    def new_for_session(self, conversation_memory=None, config=None):
        """Create an isolated model client for one device WebSocket session."""
        return OmniClient(config or self.config, conversation_memory=conversation_memory)

    @property
    def model(self) -> str:
        # 动态读取，支持面板在线切换模型
        return self.config["dashscope"].get("model", "qwen3.5-omni-flash-realtime")

    @property
    def workspace(self) -> str:
        return self.config["dashscope"].get("workspace_id", "")

    @property
    def realtime_url_tpl(self) -> str:
        return self.config["dashscope"].get("realtime_url", "")

    @property
    def voice(self) -> str:
        # 动态读取，支持面板在线切换音色
        return self.config["dashscope"].get("voice", "Ethan")

    @property
    def language(self) -> str:
        language = self.config["dashscope"].get("language", "zh")
        return language if language in LANGUAGE_PROMPTS else "auto"

    @property
    def instructions(self) -> str:
        # 人物设定（system prompt），支持面板在线修改
        return self.config["dashscope"].get(
            "instructions",
            "你是童童，一个友好、热情的语音助手。请用简短自然的中文口语回答。",
        )

    def effective_instructions(self, include_history: bool = False) -> str:
        tool_rules = self.config.get("dashscope", {}).get("tool_rules")
        if isinstance(tool_rules, list):
            tool_instructions = compose_global_tool_instructions(
                tool_rules, self.config.get("model_tool_categories", {}))
        else:
            tool_instructions = self.config.get("dashscope", {}).get(
                "tool_instructions") or DEFAULT_GLOBAL_TOOL_INSTRUCTIONS
            # Existing deployments may have a persisted global rule set created
            # before emotion output was introduced.
            if "emotion 只能使用" not in tool_instructions:
                tool_instructions = tool_instructions.rstrip() + "\n\n" + EMOTION_INSTRUCTIONS
        if ("server.weather.get" not in tool_instructions or
                "server.time.now" not in tool_instructions):
            tool_instructions = tool_instructions.rstrip() + "\n\n" + WEATHER_TIME_TOOL_INSTRUCTIONS
        if "server.music.play" not in tool_instructions:
            tool_instructions = tool_instructions.rstrip() + "\n\n" + MUSIC_TOOL_INSTRUCTIONS
        tool_instructions = filter_tool_instructions_for_categories(
            tool_instructions, self.config.get("model_tool_categories", {}))
        parts = [
            self.instructions.strip(),
            LANGUAGE_PROMPTS[self.language],
            tool_instructions.strip(),
        ]
        user_memory = self.config.get("dashscope", {}).get("user_memory_prompt", "")
        if user_memory:
            parts.append(
                "以下是用户确认或明确表达的长期信息。仅在相关时自然使用，"
                "不要逐条复述，也不要声称你在读取数据库：\n" + user_memory)
        turns = self._conversation_memory.get("turns", []) if include_history else []
        if turns:
            history_lines = [
                "The following is the recent conversation history. Continue it naturally and "
                "do not claim that you have no memory:"
            ]
            for turn in turns:
                history_lines.append("User: " + turn["user"])
                history_lines.append("Assistant: " + turn["assistant"])
            parts.append("\n".join(history_lines))
        return "\n\n".join(part for part in parts if part)

    def _expire_conversation_memory(self):
        last_activity = float(self._conversation_memory.get("last_activity", 0.0) or 0.0)
        if last_activity and time.time() - last_activity > self.conversation_timeout:
            self._conversation_memory["turns"] = []
            self._conversation_memory["last_activity"] = 0.0
            log.info("omni: conversation history expired after idle timeout")

    def _remember_turn(self, user_text: str, assistant_text: str):
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text:
            log.warning("omni: incomplete transcript; turn not added to conversation history")
            return
        turns = self._conversation_memory.setdefault("turns", [])
        turns.append({"user": user_text, "assistant": assistant_text})
        del turns[:-MAX_CONVERSATION_HISTORY_TURNS]
        self._conversation_memory["last_activity"] = time.time()
        log.info("omni: remembered conversation turn (history=%d)", len(turns))

    @property
    def conversation_timeout(self) -> float:
        """Idle timeout in seconds, read dynamically for dashboard updates."""
        try:
            minutes = float(self.config["dashscope"].get("conversation_timeout_minutes", 10))
        except (TypeError, ValueError):
            minutes = 10.0
        return max(1.0, min(120.0, minutes)) * 60.0

    @property
    def realtime_url(self) -> str:
        return self.realtime_url_tpl.replace("{workspace}", self.workspace) + "?model=" + self.model

    async def ensure_session(self):
        if self._session is None or self._session.closed:
            # The face service is accessed by its numeric LAN/public IP and
            # authenticates with a Flask session cookie.  aiohttp's default
            # cookie jar rejects cookies from IP hosts, so explicitly allow
            # them for this backend session.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120),
                cookie_jar=aiohttp.CookieJar(unsafe=True))
        return self._session

    async def close(self):
        await self._reset_realtime()
        if self._session and not self._session.closed:
            await self._session.close()

    async def reset_conversation(self):
        """Start the next wake cycle with no prior chat-session context."""
        self._conversation_memory["turns"] = []
        self._conversation_memory["last_activity"] = 0.0
        await self._reset_realtime()
        log.info("omni: conversation context reset for standby")

    async def _reset_realtime(self):
        ws = self._ws
        self._ws = None
        self._ws_url = None
        self._last_activity = 0.0
        if ws is not None and not getattr(ws, "closed", False):
            await ws.close()

    async def cancel_current_response(self):
        """Cancel model generation while keeping the reusable socket alive."""
        ws = self._ws
        if ws is None or getattr(ws, "closed", False):
            return
        try:
            await ws.send_json({
                "event_id": self._next_event_id("cancel"),
                "type": "response.cancel",
            })
            log.info("omni: current response cancelled")
        except Exception:
            log.exception("omni: failed to cancel response; resetting realtime socket")
            await self._reset_realtime()

    def _next_event_id(self, prefix: str) -> str:
        self._event_seq += 1
        return f"evt-{prefix}-{self._event_seq}"

    @staticmethod
    def _extract_emotion(value):
        """Extract an optional provider emotion from a Realtime event."""
        if isinstance(value, dict):
            emotion = value.get("emotion")
            if isinstance(emotion, str) and emotion.strip():
                return emotion.strip()
            for child in value.values():
                found = OmniClient._extract_emotion(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = OmniClient._extract_emotion(child)
                if found:
                    return found
        return None

    @staticmethod
    def _extract_response_emotion(event_type, value):
        """Extract emotion only from assistant response events.

        Realtime transcription events also contain an ``emotion`` field, but
        that field describes the user's input and must not drive the device's
        assistant expression.
        """
        if not isinstance(event_type, str) or not event_type.startswith("response."):
            return None
        return OmniClient._extract_emotion(value)

    @staticmethod
    def _split_inline_emotion(text):
        """Remove an emotion JSON fragment sometimes emitted in text."""
        if not isinstance(text, str):
            return text or "", None
        pattern = re.compile(
            r"\{\s*[\"']emotion[\"']\s*:\s*[\"']([^\"']+)[\"']\s*\}",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if not match:
            return text, None
        cleaned = (text[:match.start()] + text[match.end():]).strip()
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        return cleaned, match.group(1).strip()

    # ------------------------------------------------------------------
    # 单轮对话（手动模式：客户端控制语音起止）
    # 设备端已经用 VAD 判断了说话结束，这里把整段 PCM 发给模型，
    # 等模型返回完整音频后结束。
    # ------------------------------------------------------------------
    async def chat_stream(self, pcm: bytes, tools=None, tool_handler=None):
        """把一段 16k PCM 语音发给模型，流式产出音频/文本。

        pcm: 设备上传的 16k 单声道 PCM 字节
        tools: OpenAI-compatible function definitions derived from the ESP32
            MCP tools/list response.
        tool_handler: async callback that executes one device MCP call and
            returns its JSON-serialised result.

        yield 事件:
          {"type":"audio", "audio_b64": "...", "sample_rate": 24000}
          {"type":"text", "text": "..."}
          {"type":"done"}
          {"type":"error", "message": "..."}
        """
        if not self.api_key:
            yield {"type": "error", "message": "未配置 DASHSCOPE_API_KEY"}
            return
        if not self.workspace:
            yield {"type": "error", "message": "未配置 workspace_id"}
            return

        session = await self.ensure_session()
        debug_record = {
            "device_id": self.config.get("device_id", ""),
            "request": {},
            "response_events": [],
            "tool_results": [],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-WorkSpace": self.workspace,
        }
        url = self.realtime_url

        try:
            async with _PersistentRealtimeContext(self, session, url, headers) as ws:
                # 1. 会话配置：手动模式（客户端控制 VAD 结束），音频+文本输出
                # 注意: 必须显式 turn_detection=null 关闭服务端VAD，
                # 否则服务端自行管理音频缓冲，手动 commit 会报 "buffer too small"
                session_config = {
                    "modalities": ["text", "audio"],
                    "voice": self.voice,
                    "turn_detection": None,
                    "instructions": self.effective_instructions(
                        include_history=not self._connection_reused),
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime",
                    },
                }
                if self.language in TRANSCRIPTION_LANGUAGE_CODES:
                    session_config["input_audio_transcription"]["language"] = self.language
                if tools:
                    session_config["tools"] = tools

                session_update = {
                    "event_id": self._next_event_id("session"),
                    "type": "session.update",
                    "session": session_config,
                }
                debug_record["request"]["session_update"] = session_update
                debug_record["request"]["audio"] = {
                    "pcm_bytes": len(pcm),
                    "chunk_count": (len(pcm) + 3199) // 3200,
                    "base64": "<omitted to control log size>",
                }
                await ws.send_json(session_update)

                # 等 session.updated 就绪
                ready = False
                for _ in range(20):
                    msg = await asyncio.wait_for(ws.receive(), timeout=15)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        obj = json.loads(msg.data)
                        if obj.get("type") == "session.updated":
                            ready = True
                            break
                        if obj.get("type") == "error":
                            log.warning("omni session error: %s", obj)
                            break
                if not ready:
                    yield {"type": "error", "message": "会话配置失败"}
                    return

                # 2. 追加音频。设备端已经按实时速度采集完毕，因此这里快速
                # 送入模型，避免用户说完后再次等待约半段录音时长。
                CHUNK = 3200  # 100ms @ 16k
                for i in range(0, len(pcm), CHUNK):
                    part = pcm[i:i + CHUNK]
                    if len(part) < CHUNK:
                        part = part + b"\x00" * (CHUNK - len(part))
                    await ws.send_json({
                        "event_id": self._next_event_id("audio"),
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(part).decode(),
                    })

                # 3. 手动提交 + 请求响应
                commit_event = {
                    "event_id": self._next_event_id("commit"),
                    "type": "input_audio_buffer.commit",
                }
                response_event = {
                    "event_id": self._next_event_id("response"),
                    "type": "response.create",
                }
                debug_record["request"]["events"] = [commit_event, response_event]
                await ws.send_json(commit_event)
                await ws.send_json(response_event)
                log.info("omni: sent %d bytes audio, waiting response", len(pcm))

                # 4. 收响应：音频 delta / 文本 delta / function call。
                # 一个初始响应可以要求多个工具。先等待 response.done，再把
                # 所有结果写回并触发一次后续推理，避免多个 response.create 重叠。
                pending_tool_calls = []
                completed_call_ids = set()
                tool_round = 0
                user_transcript = ""
                assistant_text_parts = []
                assistant_transcript = ""
                assistant_text_streamed = False
                while True:
                    msg = await asyncio.wait_for(ws.receive(), timeout=60)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        obj = json.loads(msg.data)
                        t = obj.get("type", "")
                        if t != "response.audio.delta":
                            debug_record["response_events"].append(obj)
                        emotion = self._extract_response_emotion(t, obj)
                        if emotion:
                            yield {"type": "emotion", "emotion": emotion}
                        if t == "response.audio.delta":
                            yield {"type": "audio", "audio_b64": obj.get("delta", ""),
                                   "sample_rate": self.output_rate}
                        elif t in ("response.audio_transcript.delta", "response.text.delta"):
                            delta = obj.get("delta", "")
                            if delta:
                                assistant_text_parts.append(delta)
                                # Forward transcript deltas immediately so
                                # the device subtitle follows the audio
                                # instead of arriving only after response.done.
                                assistant_text_streamed = True
                                yield {"type": "text", "text": delta}
                        elif t in ("response.audio_transcript.done", "response.text.done"):
                            assistant_transcript = obj.get("transcript", obj.get("text", ""))
                            if assistant_transcript and not assistant_text_parts:
                                assistant_text_parts.append(assistant_transcript)
                                assistant_text_streamed = True
                                yield {"type": "text", "text": assistant_transcript}
                        elif t == "conversation.item.input_audio_transcription.completed":
                            user_transcript = obj.get("transcript", "").strip()
                            if user_transcript:
                                yield {"type": "input_text", "text": user_transcript}
                        elif t == "conversation.item.input_audio_transcription.failed":
                            log.warning("omni input transcription failed: %s", obj.get("error", {}))
                        elif t == "response.function_call_arguments.done":
                            call_id = obj.get("call_id")
                            if call_id and call_id not in completed_call_ids:
                                pending_tool_calls.append({
                                    "type": "tool_call",
                                    "id": call_id,
                                    "name": obj.get("name", ""),
                                    "arguments": self._parse_arguments(obj.get("arguments", "{}")),
                                })
                                completed_call_ids.add(call_id)
                        elif t == "response.done":
                            usage = self._extract_usage(obj)
                            if usage:
                                yield {"type": "usage", **usage}
                            # Some compatible gateways include function calls
                            # only in response.done. Accept that representation
                            # as a fallback while preferring the dedicated event.
                            for item in obj.get("response", {}).get("output", []):
                                if item.get("type") != "function_call":
                                    continue
                                call_id = item.get("call_id")
                                if call_id and call_id not in completed_call_ids:
                                    pending_tool_calls.append({
                                        "type": "tool_call",
                                        "id": call_id,
                                        "name": item.get("name", ""),
                                        "arguments": self._parse_arguments(item.get("arguments", "{}")),
                                    })
                                    completed_call_ids.add(call_id)

                            if pending_tool_calls:
                                tool_output_events = await self._complete_tool_calls(
                                    ws, pending_tool_calls, tool_handler)
                                debug_record["user_transcript"] = user_transcript
                                debug_record["assistant_text"] = (
                                    assistant_transcript or "".join(assistant_text_parts))
                                debug_record["tool_calls"] = pending_tool_calls
                                model_debug_log.info(json.dumps(
                                    debug_record, ensure_ascii=False))
                                debug_record = {
                                    "device_id": self.config.get("device_id", ""),
                                    "request": {
                                        "tool_output_events": tool_output_events,
                                    },
                                    "response_events": [],
                                    "tool_results": [],
                                }
                                pending_tool_calls = []
                                tool_round += 1
                                followup_response_event = {
                                    "event_id": self._next_event_id(
                                        "tool-response-{}".format(tool_round)),
                                    "type": "response.create",
                                    "response": {"modalities": ["text", "audio"]},
                                }
                                debug_record["request"].setdefault(
                                    "events", []).append(followup_response_event)
                                await ws.send_json(followup_response_event)
                                continue
                            assistant_text = assistant_transcript or "".join(assistant_text_parts)
                            assistant_text, inline_emotion = self._split_inline_emotion(
                                assistant_text)
                            if inline_emotion:
                                yield {"type": "emotion", "emotion": inline_emotion}
                            if assistant_text and not assistant_text_streamed:
                                yield {"type": "text", "text": assistant_text}
                            debug_record["user_transcript"] = user_transcript
                            debug_record["assistant_text"] = assistant_text
                            debug_record["tool_call_count"] = tool_round
                            model_debug_log.info(json.dumps(
                                debug_record, ensure_ascii=False))
                            debug_record = None
                            self._remember_turn(
                                user_transcript,
                                assistant_text,
                            )
                            yield {"type": "done"}
                            break
                        elif t == "error":
                            log.warning("omni error: %s", json.dumps(obj, ensure_ascii=False)[:300])
                            yield {"type": "error", "message": obj.get("error", {}).get("message", "omni error")}
                            break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        yield {"type": "error", "message": "omni 连接关闭"}
                        break
        except asyncio.TimeoutError:
            yield {"type": "error", "message": "omni 响应超时"}
        except Exception as e:
            log.error("omni ws error: %s", e)
            yield {"type": "error", "message": str(e)}
        finally:
            if debug_record is not None:
                model_debug_log.info(json.dumps(debug_record, ensure_ascii=False))

    @staticmethod
    def _parse_arguments(arguments):
        if isinstance(arguments, dict):
            return arguments
        if not arguments:
            return {}
        try:
            value = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _extract_usage(response_done):
        """Normalize OpenAI-compatible token usage without guessing values."""
        response = response_done.get("response", {}) if isinstance(response_done, dict) else {}
        usage = response.get("usage") or response_done.get("usage") or {}
        if not isinstance(usage, dict):
            return {}
        def value(*names):
            for name in names:
                raw = usage.get(name)
                if isinstance(raw, (int, float)) and raw >= 0:
                    return int(raw)
            return 0
        input_tokens = value("input_tokens", "prompt_tokens")
        output_tokens = value("output_tokens", "completion_tokens")
        total_tokens = value("total_tokens") or input_tokens + output_tokens
        if not (input_tokens or output_tokens or total_tokens):
            return {}
        return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": total_tokens}

    async def _complete_tool_calls(self, ws, calls, tool_handler):
        """Execute device tools, return each result, then let the model reply."""
        output_events = []
        for call in calls:
            if not call.get("name"):
                result = json.dumps({"error": "missing function name"})
            elif tool_handler is None:
                result = json.dumps({"error": "device tools unavailable"})
            else:
                try:
                    result = await tool_handler(call)
                except Exception as exc:
                    log.exception("device tool handler failed: %s", call.get("name"))
                    result = json.dumps({"error": str(exc)})
            if result is None:
                result = json.dumps({"error": "device tool did not return a result"})
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
            output_event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call["id"],
                    "output": result,
                },
            }
            output_events.append(output_event)
            await ws.send_json(output_event)
        return output_events
