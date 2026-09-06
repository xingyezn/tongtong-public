"""AccountStore persistence and tenant-isolation checks."""
import tempfile
from pathlib import Path
import sys
import sqlite3
import time

sys.path.insert(0, ".")
from app.account_store import AccountStore  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "accounts.db"
        store = AccountStore(path)
        alice = store.register_user("alice", "strong-pass-123")
        bob = store.register_user("bob-user", "strong-pass-456")
        assert store.authenticate("ALICE", "strong-pass-123")["id"] == alice["id"]
        assert store.authenticate("alice", "bad-password") is None

        dev = store.touch_device("AA:BB:CC:DD", "client-a")
        assert len(dev["binding_code"]) == 8 and dev["binding_code"].isdigit()
        try:
            store.bind_device_by_code(alice["id"], "00000000")
            raise AssertionError("wrong binding code was accepted")
        except ValueError:
            pass
        bound = store.bind_device_by_code(alice["id"], dev["binding_code"])
        assert bound["name"] == "我的设备 1" and bound["identifier"].startswith("device-")
        assert store.user_owns_device(alice["id"], dev["device_id"])
        assert not store.user_owns_device(bob["id"], dev["device_id"])
        try:
            store.bind_device_by_code(bob["id"], dev["binding_code"])
            raise AssertionError("one-time binding code was reused")
        except ValueError:
            pass

        store.set_model_settings(alice["id"], dev["device_id"],
                                 {"model": "device-model", "language": "ja"})
        store.set_vad_settings(alice["id"], dev["device_id"],
                               {"silence_duration_ms": 300})
        assert store.record_turn(dev["device_id"], "hello", "world")
        assert store.list_conversations(alice["id"], dev["device_id"])[0]["user_text"] == "hello"
        chats = store.list_chat_sessions(alice["id"], dev["device_id"])
        assert len(chats) == 1 and chats[0]["message_count"] == 2
        messages = store.get_chat_messages(alice["id"], chats[0]["id"])["messages"]
        assert [(item["role"], item["content"]) for item in messages] == [
            ("user", "hello"), ("assistant", "world")]
        # Elapsed time no longer splits a chat. Only returning to standby
        # (end_conversation) creates the boundary.
        with store._lock, store._db:
            store._db.execute(
                "UPDATE chat_sessions SET last_message_at=? WHERE id=?",
                (time.time() - 7200, chats[0]["id"]))
        second_turn = store.record_turn(
            dev["device_id"], "still there", "yes", timeout_minutes=1)
        assert second_turn["conversation_id"] == chats[0]["id"]
        memory = store.upsert_memory(
            alice["id"], "偏好", "喜欢的颜色", "蓝色")
        assert store.memory_prompt(alice["id"]) == "- 喜欢的颜色：蓝色"
        store.update_memory(alice["id"], memory["id"], enabled=False)
        assert store.memory_prompt(alice["id"]) == ""
        features = store.set_device_features(alice["id"], dev["device_id"], {
            "memory_enabled": False, "automatic_interrupt": False})
        assert not features["memory_enabled"] and not features["automatic_interrupt"]
        assert features["button_interrupt"] and features["double_click_end"]
        ended_id = store.end_conversation(dev["device_id"])
        assert ended_id == chats[0]["id"]
        sourced = store.upsert_memory(
            alice["id"], "偏好", "常用问候", "hello",
            source_conversation_id=ended_id)
        assert any(item["id"] == sourced["id"] for item in store.list_memories(alice["id"]))
        deleted = store.soft_delete_conversation(alice["id"], ended_id)
        assert deleted["id"] == ended_id
        assert store.list_chat_sessions(alice["id"], dev["device_id"]) == []
        assert store.list_conversations(alice["id"], dev["device_id"]) == []
        assert all(item["id"] != sourced["id"] for item in store.list_memories(alice["id"]))
        assert store.conversation_for_memory(ended_id) is None
        assert store.list_deleted_chat_sessions()[0]["id"] == ended_id
        audit = store.get_chat_messages_for_admin(ended_id)
        assert audit["conversation"]["deleted_at"] is not None
        assert [item["content"] for item in audit["messages"]] == [
            "hello", "world", "still there", "yes"]
        try:
            store.get_chat_messages(alice["id"], ended_id)
            raise AssertionError("deleted conversation remained visible to its owner")
        except PermissionError:
            pass
        try:
            store.soft_delete_conversation(bob["id"], ended_id)
            raise AssertionError("cross-user conversation deletion was accepted")
        except PermissionError:
            pass

        next_chat = store.record_turn(dev["device_id"], "new wake", "new chat")
        assert next_chat["conversation_id"] != ended_id
        try:
            store.list_conversations(bob["id"], dev["device_id"])
            raise AssertionError("cross-user conversation access was accepted")
        except PermissionError:
            pass
        store.close()

        reopened = AccountStore(path)
        assert reopened.get_model_settings(dev["device_id"])["model"] == "device-model"
        assert reopened.get_vad_settings(dev["device_id"])["silence_duration_ms"] == 300
        reopened.unbind_device(alice["id"], dev["device_id"])
        assert reopened.device_owner_id(dev["device_id"]) is None
        assert len(reopened.get_device(dev["device_id"])["binding_code"]) == 8
        assert reopened.get_model_settings(dev["device_id"]) == {}
        reopened.close()

        legacy_path = Path(tmp) / "legacy.db"
        legacy = sqlite3.connect(legacy_path)
        legacy.execute("""
            CREATE TABLE devices (
                device_id TEXT PRIMARY KEY COLLATE NOCASE,
                client_id TEXT NOT NULL DEFAULT '',
                owner_user_id INTEGER,
                identifier TEXT COLLATE NOCASE,
                display_name TEXT,
                binding_code TEXT,
                model_settings TEXT NOT NULL DEFAULT '{}',
                vad_settings TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                bound_at REAL,
                UNIQUE(owner_user_id, identifier)
            )
        """)
        legacy.execute("""
            INSERT INTO devices(device_id,binding_code,created_at,last_seen)
            VALUES('legacy-device','123456',?,?)
        """, (time.time(), time.time()))
        legacy.commit()
        legacy.close()
        migrated = AccountStore(legacy_path)
        migrated_device = migrated.get_device("legacy-device")
        assert len(migrated_device["binding_code"]) == 8
        assert migrated_device["binding_code_expires_at"] > time.time()
        migrated.close()
    print("ALL ACCOUNT STORE TESTS PASSED")


if __name__ == "__main__":
    main()
