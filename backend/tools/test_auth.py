"""本地验证 dashboard 鉴权（启用密码时）。"""
import asyncio
import sys
from aiohttp import web, ClientSession

sys.path.insert(0, ".")
from app.dashboard import BroadcastLogHandler, Dashboard  # noqa: E402


class FakeHttpApi:
    device_tokens = {"AA:BB:CC": "tok1"}


class FakeSession:
    device_id = "AA:BB:CC"
    session_id = "sess-123"
    bin_version = 3
    listening = False
    speaking = False
    omni_busy = False
    connected_at = 100.0


async def main():
    config = {
        "server": {"public_ws_url": "ws://x/ws"},
        "dashscope": {"api_key": "", "model": "m",
                      "base_url": "b", "output_sample_rate": 24000},
        "devices": {"enabled": False},
        "dashboard": {"password": "test-pass-123", "session_ttl": 86400},
    }
    sessions = {"AA:BB:CC": FakeSession()}
    log_handler = BroadcastLogHandler()
    log_handler.attach(asyncio.get_event_loop())
    dash = Dashboard(config, sessions, FakeHttpApi(), log_handler)

    app = web.Application()
    dash.add_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8098)
    await site.start()

    async with ClientSession() as c:
        # 未登录访问 / 应 302 到 /login
        r = await c.get("http://127.0.0.1:8098/", allow_redirects=False)
        assert r.status == 302 and "/login" in r.headers["Location"], r.status
        print("no-auth GET / -> 302 /login : ok")

        # 未登录访问 /api/status 应 401
        r = await c.get("http://127.0.0.1:8098/api/status")
        assert r.status == 401
        print("no-auth GET /api/status -> 401 : ok")

        # 错误密码
        r = await c.post("http://127.0.0.1:8098/login", data={"password": "wrong"})
        assert "密码错误" in await r.text()
        print("wrong password -> 密码错误 : ok")

        # 正确密码 -> 302 + set-cookie
        r = await c.post("http://127.0.0.1:8098/login", data={"password": "test-pass-123"},
                         allow_redirects=False)
        assert r.status == 302
        set_cookie = r.headers.get("Set-Cookie", "")
        assert "tongtong_auth" in set_cookie
        print("correct password -> 302 + cookie : ok")

        # 手动提取 cookie（模拟浏览器保存）
        auth_cookie = set_cookie.split(";")[0]

        # 登录后访问首页和 API
        r = await c.get("http://127.0.0.1:8098/", headers={"Cookie": auth_cookie})
        assert "Tongtong Backend Monitor" in await r.text()
        print("auth GET / : ok")
        r = await c.get("http://127.0.0.1:8098/api/status", headers={"Cookie": auth_cookie})
        data = await r.json()
        assert data["health"]["status"] == "ok"
        print("auth GET /api/status : ok")

    await runner.cleanup()
    print("ALL AUTH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
