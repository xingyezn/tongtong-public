"""Verify administrator RBAC and user/device management APIs."""

import asyncio
import sys
import tempfile
from pathlib import Path

from aiohttp import ClientSession, web

sys.path.insert(0, ".")
from app.account_store import AccountError, AccountStore  # noqa: E402
from app.dashboard import AUTH_COOKIE, BroadcastLogHandler, Dashboard  # noqa: E402


class FakeHttpApi:
    device_tokens = {}


class FakeWs:
    closed = False


class FakeSession:
    def __init__(self):
        self.ws = FakeWs()
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self):
        self.ws.closed = True


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(Path(tmp) / "admin.db")
        admin = store.ensure_admin("admin", "admin-test-password")
        alice = store.register_user("alice", "alice-test-password")
        bob = store.register_user("bob-user", "bob-test-password")
        device = store.touch_device("AA:BB:CC:DD", "client-a")
        store.bind_device(bob["id"], device["device_id"], device["binding_code"],
                          "bob-device", "Bob device")
        store.record_turn(device["device_id"], "测试", "测试回复",
                          usage={"input_tokens": 8, "output_tokens": 13,
                                 "total_tokens": 21})
        store.upsert_memory(bob["id"], "偏好", "颜色", "蓝色")

        assert admin["is_admin"] and store.authenticate(
            "admin", "admin-test-password")["is_admin"]
        assert store.user_can_access_device(admin["id"], device["device_id"])
        assert not store.user_can_access_device(alice["id"], device["device_id"])
        try:
            store.update_user_for_admin(admin["id"], admin["id"], is_admin=False)
            raise AssertionError("current admin could demote itself")
        except AccountError:
            pass

        config = {
            "environment": "test",
            "server": {"public_ws_url": "ws://127.0.0.1:8097/ws"},
            "dashscope": {"api_key": "", "model": "m", "language": "zh",
                          "voice": "Ethan", "instructions": "test",
                          "realtime_url": "wss://example", "output_sample_rate": 24000},
            "devices": {"enabled": False}, "vad": {},
            "dashboard": {"session_ttl": 86400, "registration_enabled": True},
        }
        handler = BroadcastLogHandler()
        handler.attach(asyncio.get_event_loop())
        sessions = {}
        dashboard = Dashboard(config, sessions, FakeHttpApi(), handler, account_store=store)
        app = web.Application()
        dashboard.add_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8097).start()

        admin_token = store.create_session(admin["id"], 3600)
        alice_token = store.create_session(alice["id"], 3600)
        admin_headers = {"Cookie": "{}={}".format(AUTH_COOKIE, admin_token)}
        alice_headers = {"Cookie": "{}={}".format(AUTH_COOKIE, alice_token)}
        async with ClientSession() as client:
            response = await client.get(
                "http://127.0.0.1:8097/api/admin/overview", headers=alice_headers)
            assert response.status == 403

            response = await client.get(
                "http://127.0.0.1:8097/api/admin/overview", headers=admin_headers)
            overview = await response.json()
            assert response.status == 200 and overview["environment"] == "test"
            assert overview["counts"] == {
                "users": 3, "admins": 1, "devices": 1, "online_devices": 0}

            response = await client.get(
                "http://127.0.0.1:8097/api/model?device_id=" + device["device_id"],
                headers=admin_headers)
            assert response.status == 200
            response = await client.get(
                "http://127.0.0.1:8097/api/memories?user_id=" + str(bob["id"]),
                headers=admin_headers)
            memories = await response.json()
            assert response.status == 200 and memories["memories"][0]["value"] == "蓝色"

            device_session = FakeSession()
            sessions[device["device_id"]] = device_session
            response = await client.post(
                "http://127.0.0.1:8097/api/admin/devices/switch-environment",
                headers=admin_headers,
                json={"device_id": device["device_id"], "environment": "test",
                      "ota_url": "http://192.168.1.20:8082/ota", "reboot": True})
            assert response.status == 202
            assert device_session.sent[-1]["command"] == "set_ota_url"
            response = await client.post(
                "http://127.0.0.1:8097/api/admin/devices/switch-environment",
                headers=admin_headers,
                json={"device_id": device["device_id"], "environment": "production",
                      "ota_url": "http://public.example/ota"})
            assert response.status == 400

            response = await client.post(
                "http://127.0.0.1:8097/api/admin/users/create",
                headers=admin_headers,
                json={"username": "support-admin", "password": "support-password",
                      "is_admin": True})
            support = await response.json()
            assert response.status == 201 and support["is_admin"]

            response = await client.post(
                "http://127.0.0.1:8097/api/admin/users/update",
                headers=admin_headers, json={"user_id": alice["id"], "is_active": False})
            assert response.status == 200 and not (await response.json())["is_active"]
            assert store.user_from_session(alice_token) is None

            response = await client.post(
                "http://127.0.0.1:8097/api/admin/devices/assign",
                headers=admin_headers,
                json={"device_id": device["device_id"], "owner_user_id": support["id"]})
            assigned = await response.json()
            assert response.status == 200 and assigned["owner_user_id"] == support["id"]

            response = await client.post(
                "http://127.0.0.1:8097/api/admin/devices/update",
                headers=admin_headers,
                json={"device_id": device["device_id"], "is_active": False})
            disabled = await response.json()
            assert response.status == 200 and not disabled["is_active"]
            assert device_session.ws.closed

            response = await client.get(
                "http://127.0.0.1:8097/api/admin/overview?user_id=" + str(support["id"]),
                headers=admin_headers)
            filtered = await response.json()
            assert response.status == 200 and len(filtered["devices"]) == 1

            response = await client.get(
                "http://127.0.0.1:8097/api/admin/overview", headers=admin_headers)
            usage_overview = await response.json()
            assert usage_overview["usage_by_user_device"][0]["total_tokens"] == 21

            response = await client.get(
                "http://127.0.0.1:8097/api/admin/overview", headers=admin_headers)
            overview = await response.json()
            assert len(overview["audit"]) == 5

        await runner.cleanup()
        store.close()
    print("ALL ADMINISTRATION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
