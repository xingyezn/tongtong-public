"""本地验证按用户隔离的设备面板、配置、记录和硬件接口。"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, FormData, web

sys.path.insert(0, ".")
from app.account_store import AccountStore  # noqa: E402
from app.dashboard import AUTH_COOKIE, BroadcastLogHandler, Dashboard  # noqa: E402


class FakeHttpApi:
    device_tokens = {}


class FakeWs:
    closed = False


class FakeMcp:
    tools = [
        {"name": "self.chassis.go_forward", "description": "Drive forward",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "self.led.turn_on", "description": "Turn lamp on",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "self.upgrade_firmware", "description": "Must not be testable",
         "inputSchema": {"type": "object", "properties": {}}},
    ]

    async def call_tool(self, name, arguments, timeout):
        return {"content": [{"type": "text", "text": "{}:{}".format(
            name, arguments.get("speed"))}]}


class FakeSession:
    device_id = "aa:bb:cc:dd"
    session_id = "sess-123"
    bin_version = 3
    listening = True
    speaking = False
    omni_busy = False
    connected_at = time.time()
    camera_upload_token = "camera-upload-token"
    mcp = FakeMcp()
    ws = FakeWs()

    def __init__(self, config):
        self.config = config
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self):
        self.ws.closed = True


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(Path(tmp) / "dashboard.db")
        alice = store.register_user("alice", "strong-pass-123")
        bob = store.register_user("bob-user", "strong-pass-456")
        first = store.touch_device("AA:BB:CC:DD", "client-a")
        second = store.touch_device("11:22:33:44", "client-b")
        third = store.touch_device("55:66:77:88", "client-c")
        store.bind_device(alice["id"], first["device_id"], first["binding_code"],
                          "living-room", "客厅童童")
        store.bind_device(bob["id"], second["device_id"], second["binding_code"],
                          "private", "Bob device")
        store.record_turn(first["device_id"], "你好", "你好，我是童童。")

        config = {
            "server": {"public_ws_url": "ws://x/ws"},
            "dashscope": {
                "api_key": "", "model": "qwen3.5-omni-plus-realtime",
                "language": "zh", "voice": "Ethan", "instructions": "test",
                "conversation_timeout_minutes": 10,
                "realtime_url": "wss://dashscope.example", "output_sample_rate": 24000,
            },
            "devices": {"enabled": False},
            "vad": {"silence_duration_ms": 600, "energy_threshold": 600},
            "dashboard": {"session_ttl": 86400, "registration_enabled": True},
        }
        session = FakeSession({
            "dashscope": dict(config["dashscope"]), "vad": dict(config["vad"])
        })
        sessions = {first["device_id"]: session}
        log_handler = BroadcastLogHandler()
        log_handler.attach(asyncio.get_event_loop())
        dash = Dashboard(config, sessions, FakeHttpApi(), log_handler, account_store=store)
        app = web.Application()
        dash.add_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8099).start()

        token = store.create_session(alice["id"], 3600)
        headers = {"Cookie": "{}={}".format(AUTH_COOKIE, token)}
        async with ClientSession() as client:
            r = await client.get("http://127.0.0.1:8099/", headers=headers)
            html = await r.text()
            assert r.status == 200 and "设备绑定与管理" in html and "对话记录" in html

            r = await client.get("http://127.0.0.1:8099/api/status", headers=headers)
            data = await r.json()
            assert r.status == 200 and data["user"]["username"] == "alice"
            assert [d["device_id"] for d in data["devices"]] == [first["device_id"]]
            assert data["devices"][0]["name"] == "客厅童童"

            r = await client.post("http://127.0.0.1:8099/api/devices/bind",
                                  headers=headers, json={
                                      "binding_code": third["binding_code"],
                                  })
            assert r.status == 200 and (await r.json())["name"] == "我的设备 2"
            r = await client.post("http://127.0.0.1:8099/api/devices/update",
                                  headers=headers, json={
                                      "device_id": third["device_id"],
                                      "identifier": "study", "name": "书房童童",
                                  })
            assert r.status == 200 and (await r.json())["identifier"] == "study"
            r = await client.post("http://127.0.0.1:8099/api/devices/bind",
                                  headers=headers, json={
                                      "binding_code": second["binding_code"],
                                  })
            assert r.status == 400
            r = await client.post("http://127.0.0.1:8099/api/devices/unbind",
                                  headers=headers, json={"device_id": third["device_id"]})
            assert r.status == 200 and store.device_owner_id(third["device_id"]) is None

            model_url = "http://127.0.0.1:8099/api/model?device_id=" + first["device_id"]
            r = await client.get(model_url, headers=headers)
            assert r.status == 200 and (await r.json())["language"] == "zh"
            for language in ("ja", "th", "ar", "fi", "nl", "ur", "fil", "he", "fa"):
                payload = {
                    "device_id": first["device_id"], "model": "qwen3.5-omni-plus-realtime",
                    "language": language, "voice": "Ethan", "instructions": "device prompt",
                    "conversation_timeout_minutes": 12,
                }
                r = await client.post("http://127.0.0.1:8099/api/model",
                                      json=payload, headers=headers)
                assert r.status == 200, (language, await r.text())
            assert session.config["dashscope"]["language"] == "fa"

            r = await client.post("http://127.0.0.1:8099/api/vad", headers=headers,
                                  json={"device_id": first["device_id"],
                                        "silence_duration_ms": 350,
                                        "energy_threshold": 250})
            assert r.status == 200 and session.config["vad"]["silence_duration_ms"] == 350

            r = await client.get(
                "http://127.0.0.1:8099/api/conversations?device_id=" + first["device_id"],
                headers=headers)
            turns = (await r.json())["turns"]
            assert r.status == 200 and turns[0]["assistant_text"] == "你好，我是童童。"
            r = await client.get(
                "http://127.0.0.1:8099/api/conversations?device_id=" + first["device_id"],
                headers=headers)
            conversations = (await r.json())["conversations"]
            assert len(conversations) == 1 and conversations[0]["message_count"] == 2
            r = await client.get(
                "http://127.0.0.1:8099/api/conversations?conversation_id=" +
                str(conversations[0]["id"]), headers=headers)
            messages = (await r.json())["messages"]
            assert [message["role"] for message in messages] == ["user", "assistant"]

            store.end_conversation(first["device_id"])
            r = await client.post(
                "http://127.0.0.1:8099/api/conversations/delete",
                headers=headers, json={"conversation_id": conversations[0]["id"]})
            assert r.status == 200
            r = await client.get(
                "http://127.0.0.1:8099/api/conversations?device_id=" + first["device_id"],
                headers=headers)
            hidden = await r.json()
            assert hidden["conversations"] == [] and hidden["turns"] == []
            r = await client.get(
                "http://127.0.0.1:8099/api/conversations?conversation_id=" +
                str(conversations[0]["id"]), headers=headers)
            assert r.status == 403

            r = await client.post("http://127.0.0.1:8099/api/memories", headers=headers,
                                  json={"category": "偏好", "label": "颜色", "value": "蓝色"})
            assert r.status == 200 and (await r.json())["value"] == "蓝色"
            r = await client.get("http://127.0.0.1:8099/api/memories", headers=headers)
            memories = (await r.json())["memories"]
            assert len(memories) == 1 and memories[0]["enabled"]

            r = await client.post("http://127.0.0.1:8099/api/features", headers=headers,
                                  json={"device_id": first["device_id"],
                                        "memory_enabled": False,
                                        "automatic_interrupt": False})
            features = await r.json()
            assert r.status == 200 and not features["memory_enabled"]
            assert not features["automatic_interrupt"] and features["button_interrupt"]
            assert session.sent[-1]["command"] == "conversation_config"
            r = await client.get(
                "http://127.0.0.1:8099/api/conversations?device_id=" + second["device_id"],
                headers=headers)
            assert r.status == 403

            photo_bytes = b"\xff\xd8dashboard-camera-test\xff\xd9"
            form = FormData()
            form.add_field("file", photo_bytes, filename="camera.jpg", content_type="image/jpeg")
            r = await client.post("http://127.0.0.1:8099/api/camera/upload", data=form,
                                  headers={"Device-Id": first["device_id"],
                                           "Authorization": "Bearer camera-upload-token"})
            assert r.status == 200

            for _ in range(4):
                r = await client.post("http://127.0.0.1:8099/api/devices/bind",
                                      headers=headers, json={"binding_code": "99999999"})
                assert r.status == 400
            r = await client.post("http://127.0.0.1:8099/api/devices/bind",
                                  headers=headers, json={"binding_code": "99999999"})
            assert r.status == 429 and "Retry-After" in r.headers
            r = await client.get(
                "http://127.0.0.1:8099/api/camera/latest?device_id=" + first["device_id"],
                headers=headers)
            assert r.status == 200 and await r.read() == photo_bytes

            r = await client.get(
                "http://127.0.0.1:8099/api/test/tools?device_id=" + first["device_id"],
                headers=headers)
            assert [tool["name"] for tool in (await r.json())["tools"]] == [
                "self.chassis.go_forward", "self.led.turn_on"]
            r = await client.post("http://127.0.0.1:8099/api/test/mcp", headers=headers,
                                  json={"device_id": first["device_id"],
                                        "name": "self.chassis.go_forward",
                                        "arguments": {"speed": 30}})
            assert r.status == 200

        await runner.cleanup()
        store.close()
    print("ALL USER-ISOLATED DASHBOARD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
