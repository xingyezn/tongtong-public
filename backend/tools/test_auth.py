"""本地验证多用户注册、登录与会话 cookie。"""
import asyncio
import sys
import tempfile
from pathlib import Path

from aiohttp import ClientSession, web

sys.path.insert(0, ".")
from app.account_store import AccountStore  # noqa: E402
from app.dashboard import BroadcastLogHandler, Dashboard  # noqa: E402


class FakeHttpApi:
    device_tokens = {}


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(Path(tmp) / "accounts.db")
        config = {
            "server": {"public_ws_url": "ws://x/ws"},
            "dashscope": {"api_key": "", "model": "m", "language": "zh",
                          "voice": "Ethan", "instructions": "test",
                          "realtime_url": "wss://example", "output_sample_rate": 24000},
            "devices": {"enabled": False},
            "vad": {},
            "dashboard": {"session_ttl": 86400, "registration_enabled": True},
        }
        log_handler = BroadcastLogHandler()
        log_handler.attach(asyncio.get_event_loop())
        dash = Dashboard(config, {}, FakeHttpApi(), log_handler, account_store=store)
        app = web.Application()
        dash.add_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8098).start()

        async with ClientSession() as client:
            r = await client.get("http://127.0.0.1:8098/", allow_redirects=False)
            assert r.status == 302 and "/login" in r.headers["Location"]
            r = await client.get("http://127.0.0.1:8098/api/status")
            assert r.status == 401

            r = await client.post("http://127.0.0.1:8098/register", data={
                "username": "alice", "password": "strong-pass-123",
            }, allow_redirects=False)
            assert r.status == 302
            auth_cookie = r.headers["Set-Cookie"].split(";", 1)[0]

            r = await client.get("http://127.0.0.1:8098/api/me",
                                 headers={"Cookie": auth_cookie})
            assert r.status == 200 and (await r.json())["username"] == "alice"

            r = await client.post("http://127.0.0.1:8098/login", data={
                "username": "alice", "password": "wrong-pass",
            })
            assert "用户名或密码错误" in await r.text()

            r = await client.get("http://127.0.0.1:8098/logout",
                                 headers={"Cookie": auth_cookie}, allow_redirects=False)
            assert r.status == 302
            r = await client.get("http://127.0.0.1:8098/api/me",
                                 headers={"Cookie": auth_cookie})
            assert r.status == 401

            r = await client.post("http://127.0.0.1:8098/login", data={
                "username": "alice", "password": "strong-pass-123",
            }, allow_redirects=False)
            assert r.status == 302 and "tongtong_auth" in r.headers.get("Set-Cookie", "")

        await runner.cleanup()
        store.close()
    print("ALL MULTI-USER AUTH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
