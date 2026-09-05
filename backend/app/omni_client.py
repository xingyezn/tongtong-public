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
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger("omni")

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

    def new_for_session(self, conversation_memory=None):
        """Create an isolated model client for one device WebSocket session."""
        return OmniClient(self.config, conversation_memory=conversation_memory)

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
        parts = [self.instructions.strip(), LANGUAGE_PROMPTS[self.language]]
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
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        return self._session

    async def close(self):
        await self._reset_realtime()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _reset_realtime(self):
        ws = self._ws
        self._ws = None
        self._ws_url = None
        self._last_activity = 0.0
        if ws is not None and not getattr(ws, "closed", False):
            await ws.close()

    def _next_event_id(self, prefix: str) -> str:
        self._event_seq += 1
        return f"evt-{prefix}-{self._event_seq}"

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

        self._expire_conversation_memory()

        session = await self.ensure_session()
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

                await ws.send_json({
                    "event_id": self._next_event_id("session"),
                    "type": "session.update",
                    "session": session_config,
                })

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
                await ws.send_json({
                    "event_id": self._next_event_id("commit"),
                    "type": "input_audio_buffer.commit",
                })
                await ws.send_json({
                    "event_id": self._next_event_id("response"),
                    "type": "response.create",
                })
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
                while True:
                    msg = await asyncio.wait_for(ws.receive(), timeout=60)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        obj = json.loads(msg.data)
                        t = obj.get("type", "")
                        if t == "response.audio.delta":
                            yield {"type": "audio", "audio_b64": obj.get("delta", ""),
                                   "sample_rate": self.output_rate}
                        elif t in ("response.audio_transcript.delta", "response.text.delta"):
                            delta = obj.get("delta", "")
                            assistant_text_parts.append(delta)
                            yield {"type": "text", "text": delta}
                        elif t in ("response.audio_transcript.done", "response.text.done"):
                            assistant_transcript = obj.get("transcript", obj.get("text", ""))
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
                                await self._complete_tool_calls(ws, pending_tool_calls, tool_handler)
                                pending_tool_calls = []
                                tool_round += 1
                                await ws.send_json({
                                    "event_id": self._next_event_id(
                                        "tool-response-{}".format(tool_round)),
                                    "type": "response.create",
                                    "response": {"modalities": ["text", "audio"]},
                                })
                                continue
                            self._remember_turn(
                                user_transcript,
                                assistant_transcript or "".join(assistant_text_parts),
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

    async def _complete_tool_calls(self, ws, calls, tool_handler):
        """Execute device tools, return each result, then let the model reply."""
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
            await ws.send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call["id"],
                    "output": result,
                },
            })
