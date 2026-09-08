"""Offline checks for Realtime tool calling and streaming device playback.

No DashScope key, libopus encoder, or network connection is required.
"""

import asyncio
import base64
import json
import os
import sys
import types

from aiohttp import WSMsgType


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

try:
    import opuslib  # noqa: F401
except Exception:
    # The test replaces Session.device_encoder and never decodes audio.  Keep
    # it runnable on developer machines that have the Python package but not
    # the libopus shared library; production still requires real libopus.
    opuslib_stub = types.ModuleType("opuslib")
    opuslib_stub.APPLICATION_AUDIO = 0
    opuslib_stub.Encoder = object
    opuslib_stub.Decoder = object
    sys.modules["opuslib"] = opuslib_stub

from app.mcp_bridge import McpBridge
from app.omni_client import OmniClient
from app.session import Session


def test_emotion_only_comes_from_assistant_response():
    user_event = {
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "你开心吗？",
        "emotion": "sad",
    }
    assistant_event = {
        "type": "response.audio_transcript.delta",
        "delta": "开心呀！",
        "emotion": "happy",
    }

    assert OmniClient._extract_response_emotion(
        user_event["type"], user_event) is None
    assert OmniClient._extract_response_emotion(
        assistant_event["type"], assistant_event) == "happy"


class FakeWebSocket:
    closed = False

    def __init__(self):
        self.text_messages = []
        self.binary_messages = []

    async def send_str(self, value):
        self.text_messages.append(json.loads(value))

    async def send_bytes(self, value):
        self.binary_messages.append(value)


class FakeEncoder:
    def encode(self, pcm, frame_ms):
        assert frame_ms == 60
        assert len(pcm) == 2880  # 24kHz, mono, 16-bit, 60ms
        return b"fake-opus"


class ImmediateMcp:
    def __init__(self):
        self.tools = [{
            "name": "self.lamp.turn_on",
            "description": "Turn on the lamp",
            "inputSchema": {"type": "object", "properties": {}},
        }]
        self._next_id = 10

    def make_omni_tools(self):
        bridge = McpBridge(lambda _value: None)
        bridge.tools = self.tools
        return bridge.make_omni_tools()

    def make_tools_call(self, name, arguments, call_id):
        self._next_id += 1
        return {
            "type": "mcp",
            "payload": {"id": self._next_id, "params": {"name": name, "arguments": arguments}},
        }

    def register_pending(self, _request_id, future):
        asyncio.get_event_loop().call_soon(
            future.set_result,
            {"content": [{"type": "text", "text": "true"}], "isError": False},
        )


class FakeOmni:
    api_key = "test-key"

    async def chat_stream(self, _pcm, tools=None, tool_handler=None):
        assert tools and tools[0]["function"]["name"] == "self.lamp.turn_on"
        result = await tool_handler({
            "type": "tool_call",
            "id": "call-lamp-1",
            "name": "self.lamp.turn_on",
            "arguments": {},
        })
        assert json.loads(result) == {"result": ["true"]}

        yield {"type": "input_text", "text": "请打开灯"}
        pcm24 = b"\x01\x00" * 1440
        yield {"type": "audio", "audio_b64": base64.b64encode(pcm24[:1200]).decode(), "sample_rate": 24000}
        yield {"type": "audio", "audio_b64": base64.b64encode(pcm24[1200:]).decode(), "sample_rate": 24000}
        yield {"type": "text", "text": "已打开"}
        yield {"type": "done"}


class InterruptibleFakeOmni:
    """Pause mid-response so playback can be interrupted deterministically."""

    api_key = "test-key"

    def __init__(self):
        self.reached_interrupt_point = asyncio.Event()
        self.continue_response = asyncio.Event()

    async def chat_stream(self, _pcm, tools=None, tool_handler=None):
        yield {"type": "input_text", "text": "请把刚才的话说完"}
        yield {"type": "text", "text": "前半句"}
        frame = b"\x03\x00" * 1440
        for _ in range(4):
            yield {
                "type": "audio",
                "audio_b64": base64.b64encode(frame).decode(),
                "sample_rate": 24000,
            }
        self.reached_interrupt_point.set()
        await self.continue_response.wait()
        yield {"type": "text", "text": "，后半句。"}
        yield {
            "type": "audio",
            "audio_b64": base64.b64encode(frame).decode(),
            "sample_rate": 24000,
        }
        yield {"type": "done"}


class FarewellFakeOmni:
    api_key = "test-key"

    def __init__(self):
        self.reset_count = 0

    async def chat_stream(self, _pcm, tools=None, tool_handler=None):
        names = {tool["function"]["name"] for tool in tools}
        assert "server.conversation.end" in names
        result = await tool_handler({
            "type": "tool_call",
            "id": "call-end-1",
            "name": "server.conversation.end",
            "arguments": {},
        })
        assert json.loads(result)["accepted"]
        yield {"type": "input_text", "text": "你可以退下了"}
        yield {"type": "text", "text": "好的，需要时再叫我。"}
        yield {"type": "done"}

    async def reset_conversation(self):
        self.reset_count += 1


class FakeRealtimeMessage:
    type = WSMsgType.TEXT

    def __init__(self, payload):
        self.data = json.dumps(payload)


class FakeRealtimeWebSocket:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.response_creates = 0
        self.events = [
            {"type": "session.updated"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "打开灯",
            },
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-lamp-2",
                "name": "self.lamp.turn_on",
                "arguments": "{}",
            },
            {"type": "response.done", "response": {"output": []}},
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def send_json(self, value):
        self.sent.append(value)
        if value.get("type") == "response.create":
            self.response_creates += 1
            if self.response_creates == 2:
                pcm24 = b"\x02\x00" * 1440
                self.events.extend([
                    {"type": "response.audio_transcript.done", "transcript": "已打开"},
                    {"type": "response.audio.delta", "delta": base64.b64encode(pcm24).decode()},
                    {"type": "response.done", "response": {"output": []}},
                ])

    async def receive(self):
        assert self.events, "Realtime client waited for an unexpected event"
        return FakeRealtimeMessage(self.events.pop(0))

    async def close(self):
        self.closed = True


class FakeHttpSession:
    closed = False

    def __init__(self, websocket):
        self.websocket = websocket
        self.connect_calls = 0

    def ws_connect(self, *_args, **_kwargs):
        self.connect_calls += 1
        return self.websocket


async def test_realtime_tool_event_loop():
    config = {
        "dashscope": {
            "api_key": "test-key",
            "model": "qwen3.5-omni-flash-realtime",
            "workspace_id": "test-workspace",
            "realtime_url": "wss://{workspace}.example/realtime",
            "language": "zh",
            "voice": "Ethan",
            "instructions": "test",
            "input_sample_rate": 16000,
            "output_sample_rate": 24000,
        },
    }
    websocket = FakeRealtimeWebSocket()
    client = OmniClient(config)
    effective_instructions = client.effective_instructions()
    assert "必须每次重新调用对应的摄像头/人脸检测工具" in effective_instructions
    assert "不得使用、推测或复用历史对话中的图片" in effective_instructions
    assert client.conversation_timeout == 600.0
    config["dashscope"]["conversation_timeout_minutes"] = 2
    assert client.conversation_timeout == 120.0
    http_session = FakeHttpSession(websocket)
    client._session = http_session
    calls = []

    async def tool_handler(call):
        calls.append(call)
        return json.dumps({"result": ["true"]})

    tools = [{
        "type": "function",
        "function": {
            "name": "self.lamp.turn_on",
            "description": "Turn on the lamp",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    events = [event async for event in client.chat_stream(
        b"\x00\x00" * 1600, tools=tools, tool_handler=tool_handler)]

    assert calls == [{"type": "tool_call", "id": "call-lamp-2", "name": "self.lamp.turn_on", "arguments": {}}]
    assert [event["type"] for event in events] == ["input_text", "text", "audio", "done"]
    assert events[1]["text"] == "已打开"
    assert websocket.sent[0]["session"]["tools"] == tools
    assert websocket.sent[0]["session"]["input_audio_transcription"]["language"] == "zh"
    assert any(item.get("type") == "conversation.item.create" for item in websocket.sent)
    assert websocket.response_creates == 2

    # A second turn on the same device reuses the model WebSocket while still
    # issuing a fresh session update and response request.
    websocket.events.extend([
        {"type": "session.updated"},
        {"type": "response.done", "response": {"output": []}},
    ])
    second_events = [event async for event in client.chat_stream(b"\x00\x00" * 1600)]
    assert [event["type"] for event in second_events] == ["done"]
    assert http_session.connect_calls == 1
    event_ids = [item["event_id"] for item in websocket.sent if "event_id" in item]
    assert len(event_ids) == len(set(event_ids))

    # If the provider closes between turns, reconnect and inject the text
    # history collected from input/output transcription events.
    websocket.closed = True
    replacement = FakeRealtimeWebSocket()
    replacement.events = [
        {"type": "session.updated"},
        {"type": "response.done", "response": {"output": []}},
    ]
    http_session.websocket = replacement
    third_events = [event async for event in client.chat_stream(b"\x00\x00" * 1600)]
    assert [event["type"] for event in third_events] == ["done"]
    assert http_session.connect_calls == 2
    assert "User: 打开灯" in replacement.sent[0]["session"]["instructions"]
    assert "Assistant: 已打开" in replacement.sent[0]["session"]["instructions"]

    # Omni can speak Dutch, Urdu, Hebrew, and Persian, while the auxiliary ASR
    # language hint cannot. These options must still work via ASR auto-detect.
    client.config["dashscope"]["language"] = "fa"
    replacement.closed = True
    persian_ws = FakeRealtimeWebSocket()
    persian_ws.events = [
        {"type": "session.updated"},
        {"type": "response.done", "response": {"output": []}},
    ]
    http_session.websocket = persian_ws
    persian_events = [event async for event in client.chat_stream(b"\x00\x00" * 1600)]
    assert [event["type"] for event in persian_events] == ["done"]
    transcription = persian_ws.sent[0]["session"]["input_audio_transcription"]
    assert "language" not in transcription
    assert "فارسی" in persian_ws.sent[0]["session"]["instructions"]


async def test_playback_prebuffer():
    config = {
        "dashscope": {"output_sample_rate": 24000},
        "vad": {"silence_duration_ms": 400, "energy_threshold": 100},
        # Exercise the firmware-compatible minimum separately from the
        # public-service default (900 ms) used to smooth model jitter.
        "audio": {"tts_startup_buffer_ms": 240},
    }
    ws = FakeWebSocket()
    session = Session(ws, config, FakeOmni(), "prebuffer-device")
    session.device_encoder = FakeEncoder()
    state = {
        "pcm": bytearray(),
        "started": False,
        "started_at": None,
        "audio_duration_s": 0.0,
        "next_frame_send_at": None,
    }
    frame = b"\x01\x00" * 1440

    await session._stream_omni_audio({
        "type": "audio",
        "audio_b64": base64.b64encode(frame * 3).decode(),
        "sample_rate": 24000,
    }, state)
    assert not ws.text_messages
    assert not ws.binary_messages

    await session._stream_omni_audio({
        "type": "audio",
        "audio_b64": base64.b64encode(frame).decode(),
        "sample_rate": 24000,
    }, state)
    assert [m.get("state") for m in ws.text_messages] == ["start"]
    assert len(ws.binary_messages) == 4


async def test_interrupt_mutes_audio_but_preserves_complete_text():
    config = {
        "dashscope": {"output_sample_rate": 24000},
        "vad": {"silence_duration_ms": 400, "energy_threshold": 100},
        "audio": {"tts_startup_buffer_ms": 240},
    }
    ws = FakeWebSocket()
    omni = InterruptibleFakeOmni()
    persisted = []
    session = Session(
        ws, config, omni, "interrupt-device",
        turn_recorder=lambda *args: persisted.append(args),
    )
    session.device_encoder = FakeEncoder()

    session.omni_busy = True
    turn_task = asyncio.create_task(
        session._run_omni_turn_task(b"\x00\x00" * 1600))
    await asyncio.wait_for(omni.reached_interrupt_point.wait(), timeout=1)
    assert len(ws.binary_messages) == 4

    await session._abort_speaking()
    assert not turn_task.done(), "interrupt must not cancel model generation"
    assert session._suppress_current_audio
    audio_count_at_interrupt = len(ws.binary_messages)

    omni.continue_response.set()
    await asyncio.wait_for(turn_task, timeout=1)

    assert len(ws.binary_messages) == audio_count_at_interrupt
    assert persisted == [(
        "interrupt-device", "请把刚才的话说完", "前半句，后半句。",
    )]
    assert any(
        message.get("type") == "tts" and message.get("state") == "stop"
        for message in ws.text_messages
    )
    assert any(
        message.get("type") == "tts"
        and message.get("state") == "sentence_start"
        and message.get("text") == "前半句，后半句。"
        for message in ws.text_messages
    )
    await session.close()


async def test_model_farewell_enters_standby_after_text_is_saved():
    config = {
        "dashscope": {"output_sample_rate": 24000},
        "vad": {"silence_duration_ms": 400, "energy_threshold": 100},
    }
    ws = FakeWebSocket()
    omni = FarewellFakeOmni()
    events = []

    def record_turn(*args):
        events.append(("record", args))
        return {"conversation_id": 7, "ended_conversation_id": None}

    def end_conversation(device_id):
        events.append(("end", device_id))
        return 7

    session = Session(
        ws, config, omni, "farewell-device",
        turn_recorder=record_turn,
        conversation_ender=end_conversation,
    )
    session.device_encoder = FakeEncoder()
    await session._run_omni_turn(b"\x00\x00" * 1600)

    assert events == [
        ("record", ("farewell-device", "你可以退下了", "好的，需要时再叫我。")),
        ("end", "farewell-device"),
    ]
    assert omni.reset_count == 1
    assert not session.listening
    assert ws.text_messages[-1] == {"type": "system", "command": "standby"}
    await session.close()


async def test_direct_mcp_bench_call():
    sent = []
    bridge = None

    async def send_json(message):
        sent.append(message)
        await asyncio.sleep(0)
        bridge.on_device_mcp({
            "id": message["payload"]["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        })

    bridge = McpBridge(send_json)
    result = await bridge.call_tool("self.chassis.go_forward", {"speed": 30}, timeout=1)
    assert result["content"][0]["text"] == "ok"
    assert sent[0]["payload"]["method"] == "tools/call"
    assert sent[0]["payload"]["params"] == {
        "name": "self.chassis.go_forward", "arguments": {"speed": 30},
    }


async def main():
    await test_realtime_tool_event_loop()
    await test_direct_mcp_bench_call()
    await test_playback_prebuffer()
    await test_interrupt_mutes_audio_but_preserves_complete_text()
    await test_model_farewell_enters_standby_after_text_is_saved()

    config = {
        "dashscope": {"output_sample_rate": 24000},
        "vad": {"silence_duration_ms": 400, "energy_threshold": 100},
    }
    ws = FakeWebSocket()
    persisted = []
    session = Session(
        ws, config, FakeOmni(), "test-device",
        turn_recorder=lambda *args: persisted.append(args),
    )
    session.device_encoder = FakeEncoder()
    session.set_mcp(ImmediateMcp())

    await session._run_omni_turn(b"\x00\x00" * 1600)

    mcp_calls = [m for m in ws.text_messages if m.get("type") == "mcp"]
    tts_states = [m.get("state") for m in ws.text_messages if m.get("type") == "tts"]
    assert mcp_calls and mcp_calls[0]["payload"]["params"]["name"] == "self.lamp.turn_on"
    assert tts_states == ["sentence_start", "start", "stop"]
    assistant_updates = [m.get("text") for m in ws.text_messages
                         if m.get("type") == "tts" and m.get("state") == "sentence_start"]
    assert assistant_updates == ["已打开"]
    assert persisted == [("test-device", "请打开灯", "已打开")]
    assert len(ws.binary_messages) == 1
    assert ws.binary_messages[0].endswith(b"fake-opus")
    assert not session.speaking
    print("ALL OMNI PIPELINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
