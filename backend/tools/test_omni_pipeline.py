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

        pcm24 = b"\x01\x00" * 1440
        yield {"type": "audio", "audio_b64": base64.b64encode(pcm24[:1200]).decode(), "sample_rate": 24000}
        yield {"type": "audio", "audio_b64": base64.b64encode(pcm24[1200:]).decode(), "sample_rate": 24000}
        yield {"type": "text", "text": "已打开"}
        yield {"type": "done"}


class FakeRealtimeMessage:
    type = WSMsgType.TEXT

    def __init__(self, payload):
        self.data = json.dumps(payload)


class FakeRealtimeWebSocket:
    def __init__(self):
        self.sent = []
        self.response_creates = 0
        self.events = [
            {"type": "session.updated"},
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
                    {"type": "response.audio.delta", "delta": base64.b64encode(pcm24).decode()},
                    {"type": "response.done", "response": {"output": []}},
                ])

    async def receive(self):
        assert self.events, "Realtime client waited for an unexpected event"
        return FakeRealtimeMessage(self.events.pop(0))


class FakeHttpSession:
    closed = False

    def __init__(self, websocket):
        self.websocket = websocket

    def ws_connect(self, *_args, **_kwargs):
        return self.websocket


async def test_realtime_tool_event_loop():
    config = {
        "dashscope": {
            "api_key": "test-key",
            "model": "qwen3.5-omni-flash-realtime",
            "workspace_id": "test-workspace",
            "realtime_url": "wss://{workspace}.example/realtime",
            "voice": "Ethan",
            "instructions": "test",
            "input_sample_rate": 16000,
            "output_sample_rate": 24000,
        },
    }
    websocket = FakeRealtimeWebSocket()
    client = OmniClient(config)
    client._session = FakeHttpSession(websocket)
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
    assert [event["type"] for event in events] == ["audio", "done"]
    assert websocket.sent[0]["session"]["tools"] == tools
    assert any(item.get("type") == "conversation.item.create" for item in websocket.sent)
    assert websocket.response_creates == 2


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

    config = {
        "dashscope": {"output_sample_rate": 24000},
        "vad": {"silence_duration_ms": 400, "energy_threshold": 100},
    }
    ws = FakeWebSocket()
    session = Session(ws, config, FakeOmni(), "test-device")
    session.device_encoder = FakeEncoder()
    session.set_mcp(ImmediateMcp())

    await session._run_omni_turn(b"\x00\x00" * 1600)

    mcp_calls = [m for m in ws.text_messages if m.get("type") == "mcp"]
    tts_states = [m.get("state") for m in ws.text_messages if m.get("type") == "tts"]
    assert mcp_calls and mcp_calls[0]["payload"]["params"]["name"] == "self.lamp.turn_on"
    assert tts_states == ["start", "stop"]
    assert len(ws.binary_messages) == 1
    assert ws.binary_messages[0].endswith(b"fake-opus")
    assert not session.speaking
    print("ALL OMNI PIPELINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
