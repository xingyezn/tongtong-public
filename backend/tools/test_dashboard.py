"""本地验证 dashboard（不依赖 opuslib）。"""
import asyncio
import base64
import logging
import sys
from aiohttp import web, test_utils
from aiohttp.client import ClientSession

sys.path.insert(0, ".")
from app.dashboard import BroadcastLogHandler, Dashboard  # noqa: E402


class FakeHttpApi:
    device_tokens = {"AA:BB:CC": "tok1", "DD:EE:FF": "tok2"}


class FakeMcp:
    tools = [
        {
            "name": "self.chassis.go_forward",
            "description": "Drive forward",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "self.lamp.turn_on",
            "description": "Turn lamp on",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "self.upgrade_firmware",
            "description": "Must not be testable",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    async def call_tool(self, name, arguments, timeout):
        return {"content": [{"type": "text", "text": "{}:{}".format(name, arguments.get("speed"))}]}


class FakeSession:
    device_id = "AA:BB:CC"
    session_id = "sess-123"
    bin_version = 3
    listening = True
    speaking = False
    omni_busy = False
    connected_at = 100.0
    mcp = FakeMcp()
    debug_mode = False

    async def send_json(self, value):
        self.last_sent = value

    def begin_e2e_test(self, allowed_tool_names):
        assert "self.lamp.turn_on" in allowed_tool_names
        self.e2e_future = asyncio.get_running_loop().create_future()
        self.e2e_future.set_result({
            "tool_calls": [{"name": "self.lamp.turn_on", "arguments": {},
                            "result": {"isError": False}}],
            "error": None,
        })
        return self.e2e_future

    def cancel_e2e_test(self, future):
        self.e2e_cancelled = True


async def main():
    config = {
        "server": {"public_ws_url": "ws://x/ws"},
        "dashscope": {"api_key": "", "model": "qwen-omni-turbo",
                      "base_url": "https://dashscope.example", "output_sample_rate": 24000},
        "devices": {"enabled": False},
    }
    sessions = {"AA:BB:CC": FakeSession()}
    log_handler = BroadcastLogHandler()
    log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log_handler.emit(logging.LogRecord(
        "aiohttp.access", logging.INFO, __file__, 1,
        '127.0.0.1 "GET /api/status HTTP/1.1" 200 123', (), None,
    ))
    log_handler.emit(logging.LogRecord(
        "mcp", logging.INFO, __file__, 1, "MCP tools list updated", (), None,
    ))
    log_handler.emit(logging.LogRecord(
        "session", logging.INFO, __file__, 1, "device connected", (), None,
    ))
    entries = log_handler.snapshot()
    assert [entry["category"] for entry in entries] == ["periodic", "mcp", "device"]
    assert [entry["category"] for entry in log_handler.events_after(entries[0]["id"])] == ["mcp", "device"]
    print("log categories + independent periodic buffer : ok")
    log_handler.attach(asyncio.get_event_loop())
    dash = Dashboard(config, sessions, FakeHttpApi(), log_handler)

    app = web.Application()
    dash.add_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8099)
    await site.start()

    async with ClientSession() as c:
        r = await c.get("http://127.0.0.1:8099/")
        html = await r.text()
        assert "Tongtong Backend Monitor" in html, "html title missing"
        print("GET / :", r.status, "ok, html len", len(html))

        r = await c.get("http://127.0.0.1:8099/api/status")
        data = await r.json()
        assert data["health"]["status"] == "ok"
        assert len(data["devices"]) == 1
        assert data["devices"][0]["device_id"] == "AA:BB:CC"
        assert data["devices"][0]["online"] is True
        assert len(data["ota_requests"]) == 2
        assert data["config"]["api_key_configured"] is False
        print("GET /api/status : ok ->", {k: data[k] for k in ("health", "devices", "ota_requests")})
        print("config:", data["config"])

        r = await c.get("http://127.0.0.1:8099/api/test/tools?device_id=AA:BB:CC")
        tools = await r.json()
        assert r.status == 200
        assert [tool["name"] for tool in tools["tools"]] == [
            "self.chassis.go_forward", "self.lamp.turn_on"
        ]

        r = await c.post("http://127.0.0.1:8099/api/test/mcp", json={
            "device_id": "AA:BB:CC", "name": "self.chassis.go_forward",
            "arguments": {"speed": 30},
        })
        assert r.status == 409

        r = await c.post("http://127.0.0.1:8099/api/test/mode", json={
            "device_id": "AA:BB:CC", "enabled": True,
        })
        mode = await r.json()
        assert r.status == 200 and mode["debug_mode"] is True
        assert sessions["AA:BB:CC"].last_sent == {"type": "debug_mode", "enabled": True}

        r = await c.post("http://127.0.0.1:8099/api/test/mcp", json={
            "device_id": "AA:BB:CC", "name": "self.chassis.go_forward",
            "arguments": {"speed": 30},
        })
        result = await r.json()
        assert r.status == 200
        assert result["result"]["content"][0]["text"] == "self.chassis.go_forward:30"

        pcm = b"\x00" * (16000 * 60 // 1000 * 2)
        r = await c.post("http://127.0.0.1:8099/api/test/conversation", json={
            "device_id": "AA:BB:CC", "pcm_b64": base64.b64encode(pcm).decode("ascii"),
            "expected_tool": "self.lamp.turn_on",
        })
        conversation = await r.json()
        assert r.status == 200 and conversation["matched"] is True
        assert conversation["tool_calls"][0]["name"] == "self.lamp.turn_on"

        r = await c.post("http://127.0.0.1:8099/api/test/mcp", json={
            "device_id": "AA:BB:CC", "name": "self.upgrade_firmware", "arguments": {},
        })
        assert r.status == 403
        print("GET /api/test/tools + direct MCP + E2E voice API : ok")

    # 测试 echo
    async with ClientSession() as c:
        ws = await c.ws_connect("ws://127.0.0.1:8099/ws/echo")
        await ws.send_bytes(b"hello-audio")
        msg = await ws.receive()
        assert msg.type == 2  # BINARY
        assert msg.data == b"hello-audio"
        await ws.close()
        print("GET /ws/echo : ok (bytes echoed)")

    await runner.cleanup()
    print("ALL DASHBOARD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
