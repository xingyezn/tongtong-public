"""Long-term-memory extraction and persistence checks without network access."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from app.account_store import AccountStore  # noqa: E402
from app.memory_service import MemoryService  # noqa: E402


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        content = {"memories": [{
            "category": "基本信息", "label": "姓名", "value": "小明",
        }]}
        return json.dumps({"choices": [{"message": {
            "content": json.dumps(content, ensure_ascii=False),
        }}]}, ensure_ascii=False)


class FakeSession:
    closed = False

    def post(self, *_args, **_kwargs):
        return FakeResponse()

    async def close(self):
        self.closed = True


async def run_test():
    with tempfile.TemporaryDirectory() as tmp:
        store = AccountStore(Path(tmp) / "memory.db")
        user = store.register_user("memory-user", "strong-pass-123")
        device = store.touch_device("AA:BB:CC:99", "client")
        store.bind_device_by_code(user["id"], device["binding_code"])
        result = store.record_turn(device["device_id"], "我叫小明", "你好，小明")
        conversation_id = result["conversation_id"]
        store.end_conversation(device["device_id"])
        config = {
            "dashscope": {"api_key": "test", "workspace_id": "workspace"},
            "memory": {"enabled": True, "model": "qwen3.8-max"},
        }
        service = MemoryService(config, store)
        service._session = FakeSession()
        saved = await service.summarize_conversation(conversation_id)
        assert saved[0]["label"] == "姓名"
        assert store.memory_prompt(user["id"]) == "- 姓名：小明"
        assert store.conversation_for_memory(conversation_id)["conversation"][
            "memory_updated_at"] is not None
        await service.close()
        store.close()
    print("ALL MEMORY SERVICE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run_test())
