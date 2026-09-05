"""本地验证 dashboard（不依赖 opuslib）。"""
import asyncio
import logging
import sys
from aiohttp import FormData, web, test_utils
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
            "name": "self.led.turn_on",
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
    camera_upload_token = "camera-upload-token"
    mcp = FakeMcp()

    async def send_json(self, value):
        self.last_sent = value


async def main():
    config = {
        "server": {"public_ws_url": "ws://x/ws"},
        "dashscope": {"api_key": "", "model": "qwen-omni-turbo",
                      "language": "zh", "base_url": "https://dashscope.example",
                      "output_sample_rate": 24000},
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
    dash._check_cookie = lambda _request: True

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

        r = await c.get("http://127.0.0.1:8099/api/model")
        model_config = await r.json()
        assert r.status == 200 and model_config["language"] == "zh"
        r = await c.post("http://127.0.0.1:8099/api/model", json={"language": "ja"})
        updated_model = await r.json()
        assert r.status == 200 and updated_model["language"] == "ja"
        for language in ("th", "ar", "fi", "nl", "ur", "fil", "he", "fa"):
            r = await c.post(
                "http://127.0.0.1:8099/api/model", json={"language": language})
            assert r.status == 200, language
        r = await c.post("http://127.0.0.1:8099/api/model", json={"language": "invalid"})
        assert r.status == 400
        print("model language get/set/validation : ok")

        photo_bytes = b"\xff\xd8dashboard-camera-test\xff\xd9"
        form = FormData()
        form.add_field("question", "camera test")
        form.add_field("file", photo_bytes, filename="camera.jpg", content_type="image/jpeg")
        r = await c.post("http://127.0.0.1:8099/api/camera/upload", data=form, headers={
            "Device-Id": "AA:BB:CC", "Authorization": "Bearer camera-upload-token",
        })
        upload = await r.json()
        assert r.status == 200 and upload["success"] is True
        r = await c.get("http://127.0.0.1:8099/api/camera/latest?device_id=AA:BB:CC")
        assert r.status == 200 and await r.read() == photo_bytes
        form = FormData()
        form.add_field("file", photo_bytes, filename="camera.jpg", content_type="image/jpeg")
        r = await c.post("http://127.0.0.1:8099/api/camera/upload", data=form, headers={
            "Device-Id": "AA:BB:CC", "Authorization": "Bearer invalid",
        })
        assert r.status == 401
        print("camera upload + authenticated preview : ok")

        r = await c.get("http://127.0.0.1:8099/api/test/tools?device_id=AA:BB:CC")
        tools = await r.json()
        assert r.status == 200
        assert [tool["name"] for tool in tools["tools"]] == [
            "self.chassis.go_forward", "self.led.turn_on"
        ]

        r = await c.post("http://127.0.0.1:8099/api/test/mcp", json={
            "device_id": "AA:BB:CC", "name": "self.chassis.go_forward",
            "arguments": {"speed": 30},
        })
        result = await r.json()
        assert r.status == 200
        assert result["result"]["content"][0]["text"] == "self.chassis.go_forward:30"

        r = await c.post("http://127.0.0.1:8099/api/test/mcp", json={
            "device_id": "AA:BB:CC", "name": "self.upgrade_firmware", "arguments": {},
        })
        assert r.status == 403
        print("GET /api/test/tools + direct MCP API : ok")

    await runner.cleanup()
    print("ALL DASHBOARD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
