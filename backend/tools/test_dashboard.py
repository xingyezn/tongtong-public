"""本地验证 dashboard（不依赖 opuslib）。"""
import asyncio
import sys
from aiohttp import web, test_utils
from aiohttp.client import ClientSession

sys.path.insert(0, ".")
from app.dashboard import BroadcastLogHandler, Dashboard  # noqa: E402


class FakeHttpApi:
    device_tokens = {"AA:BB:CC": "tok1", "DD:EE:FF": "tok2"}


class FakeSession:
    device_id = "AA:BB:CC"
    session_id = "sess-123"
    bin_version = 3
    listening = True
    speaking = False
    omni_busy = False
    connected_at = 100.0


async def main():
    config = {
        "server": {"public_ws_url": "ws://x/ws"},
        "dashscope": {"api_key": "", "model": "qwen-omni-turbo",
                      "base_url": "https://dashscope.example", "output_sample_rate": 24000},
        "devices": {"enabled": False},
    }
    sessions = {"AA:BB:CC": FakeSession()}
    log_handler = BroadcastLogHandler()
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
