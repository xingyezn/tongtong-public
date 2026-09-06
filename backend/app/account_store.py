"""Persistent users, device ownership, per-device settings, and conversations."""

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path


USERNAME_RE = re.compile(r"^[^\s/\\]{3,64}$")
DEVICE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9:_.-]{4,128}$")
BINDING_CODE_RE = re.compile(r"^[0-9]{8}$")
BINDING_CODE_TTL_SECONDS = 10 * 60
MODEL_SETTING_KEYS = {
    "model", "language", "voice", "instructions", "conversation_timeout_minutes",
}
VAD_SETTING_KEYS = {"silence_duration_ms", "energy_threshold"}
INTERRUPTION_SETTING_KEYS = {
    "automatic_interrupt", "button_interrupt", "double_click_end",
}


class AccountError(ValueError):
    """A user-correctable account or device validation error."""


class AccountStore:
    """Small SQLite repository used by the single-process aiohttp backend."""

    def __init__(self, database_path):
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self):
        with self._lock:
            self._db.close()

    def _create_schema(self):
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry
                    ON user_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY COLLATE NOCASE,
                    client_id TEXT NOT NULL DEFAULT '',
                    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    identifier TEXT COLLATE NOCASE,
                    display_name TEXT,
                    binding_code TEXT,
                    binding_code_expires_at REAL,
                    model_settings TEXT NOT NULL DEFAULT '{}',
                    vad_settings TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    bound_at REAL,
                    UNIQUE(owner_user_id, identifier)
                );
                CREATE INDEX IF NOT EXISTS idx_devices_owner
                    ON devices(owner_user_id);
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_user_device
                    ON conversation_turns(user_id, device_id, id DESC);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    last_message_at REAL NOT NULL,
                    ended_at REAL,
                    memory_updated_at REAL,
                    deleted_at REAL,
                    deleted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_device
                    ON chat_sessions(user_id, device_id, last_message_at DESC);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
                    ON chat_messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS user_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    value TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source_conversation_id INTEGER REFERENCES chat_sessions(id) ON DELETE SET NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, category, label)
                );
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_audit_created
                    ON admin_audit_log(created_at DESC);
            """)
            user_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(users)")
            }
            if "is_admin" not in user_columns:
                self._db.execute(
                    "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            if "is_active" not in user_columns:
                self._db.execute(
                    "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(devices)")
            }
            if "binding_code_expires_at" not in columns:
                self._db.execute(
                    "ALTER TABLE devices ADD COLUMN binding_code_expires_at REAL")
            if "memory_enabled" not in columns:
                self._db.execute(
                    "ALTER TABLE devices ADD COLUMN memory_enabled INTEGER NOT NULL DEFAULT 1")
            if "interruption_settings" not in columns:
                self._db.execute(
                    "ALTER TABLE devices ADD COLUMN interruption_settings TEXT NOT NULL DEFAULT '{}'")
            if "is_active" not in columns:
                self._db.execute(
                    "ALTER TABLE devices ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            turn_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(conversation_turns)")
            }
            for name in ("input_tokens", "output_tokens", "total_tokens"):
                if name not in turn_columns:
                    self._db.execute(
                        "ALTER TABLE conversation_turns ADD COLUMN {} INTEGER NOT NULL DEFAULT 0".format(name))
            chat_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(chat_sessions)")
            }
            if "deleted_at" not in chat_columns:
                self._db.execute(
                    "ALTER TABLE chat_sessions ADD COLUMN deleted_at REAL")
            if "deleted_by_user_id" not in chat_columns:
                self._db.execute(
                    "ALTER TABLE chat_sessions ADD COLUMN deleted_by_user_id INTEGER")
            self._repair_binding_codes_locked()
            self._db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_binding_code
                ON devices(binding_code) WHERE binding_code IS NOT NULL
            """)
            self._migrate_legacy_turns_locked()

    def _migrate_legacy_turns_locked(self):
        """Convert legacy paired turns once, grouping nearby turns as one chat."""
        migrated = self._db.execute(
            "SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        if migrated:
            return
        rows = self._db.execute("""
            SELECT * FROM conversation_turns ORDER BY user_id, device_id, created_at, id
        """).fetchall()
        active = {}
        for row in rows:
            key = (row["user_id"], row["device_id"])
            current = active.get(key)
            if not current or row["created_at"] - current[1] > 600:
                cursor = self._db.execute("""
                    INSERT INTO chat_sessions(user_id,device_id,title,started_at,last_message_at,ended_at)
                    VALUES(?,?,?,?,?,NULL)
                """, (row["user_id"], row["device_id"], row["user_text"][:40],
                      row["created_at"], row["created_at"]))
                current = [cursor.lastrowid, row["created_at"]]
                active[key] = current
            for role, content in (("user", row["user_text"]),
                                  ("assistant", row["assistant_text"])):
                self._db.execute("""
                    INSERT INTO chat_messages(conversation_id,role,content,created_at)
                    VALUES(?,?,?,?)
                """, (current[0], role, content, row["created_at"]))
            current[1] = row["created_at"]
            self._db.execute(
                "UPDATE chat_sessions SET last_message_at=? WHERE id=?",
                (row["created_at"], current[0]))

    def _new_binding_code_locked(self, reserved=None):
        reserved = reserved or set()
        while True:
            code = "{:08d}".format(secrets.randbelow(100_000_000))
            if code in reserved:
                continue
            row = self._db.execute(
                "SELECT 1 FROM devices WHERE binding_code = ?", (code,)
            ).fetchone()
            if row is None:
                return code

    def _repair_binding_codes_locked(self):
        """Upgrade old six-digit/non-expiring codes without touching bound devices."""
        now = time.time()
        rows = self._db.execute("""
            SELECT device_id, binding_code, binding_code_expires_at
            FROM devices WHERE owner_user_id IS NULL
        """).fetchall()
        reserved = set()
        for row in rows:
            code = row["binding_code"] or ""
            expires = row["binding_code_expires_at"] or 0
            if not BINDING_CODE_RE.fullmatch(code) or code in reserved or expires <= now:
                code = self._new_binding_code_locked(reserved)
                expires = now + BINDING_CODE_TTL_SECONDS
                self._db.execute("""
                    UPDATE devices SET binding_code=?, binding_code_expires_at=?
                    WHERE device_id=?
                """, (code, expires, row["device_id"]))
            reserved.add(code)

    @staticmethod
    def _password_hash(password, salt):
        return hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt),
            n=2 ** 14, r=8, p=1, dklen=32,
        ).hex()

    @staticmethod
    def _user_dict(row):
        if not row:
            return None
        keys = set(row.keys())
        result = {"id": row["id"], "username": row["username"]}
        if "is_admin" in keys:
            result["is_admin"] = bool(row["is_admin"])
        if "is_active" in keys:
            result["is_active"] = bool(row["is_active"])
        if "created_at" in keys:
            result["created_at"] = row["created_at"]
        return result

    @staticmethod
    def _device_dict(row):
        if not row:
            return None
        return {
            "device_id": row["device_id"],
            "client_id": row["client_id"],
            "owner_user_id": row["owner_user_id"],
            "is_active": bool(row["is_active"]) if "is_active" in set(row.keys()) else True,
            "owner_username": (row["owner_username"] or "")
                if "owner_username" in set(row.keys()) else "",
            "identifier": row["identifier"] or "",
            "name": row["display_name"] or "",
            "binding_code": row["binding_code"],
            "binding_code_expires_at": row["binding_code_expires_at"],
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
            "bound_at": row["bound_at"],
            "memory_enabled": bool(row["memory_enabled"]),
            "interruption_settings": AccountStore._decode_json(
                row["interruption_settings"]),
        }

    @staticmethod
    def _decode_json(value):
        try:
            decoded = json.loads(value or "{}")
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}

    def register_user(self, username, password, is_admin=False):
        username = (username or "").strip()
        if not USERNAME_RE.fullmatch(username):
            raise AccountError("用户名需为 3～64 个字符，且不能包含空格、斜杠")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AccountError("密码长度需为 8～128 个字符")
        salt = secrets.token_hex(16)
        password_hash = self._password_hash(password, salt)
        try:
            with self._lock, self._db:
                cursor = self._db.execute(
                    """INSERT INTO users(
                           username,password_salt,password_hash,is_admin,is_active,created_at)
                       VALUES(?,?,?,?,1,?)""",
                    (username, salt, password_hash, int(bool(is_admin)), time.time()),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise AccountError("用户名已存在") from exc
        return {"id": user_id, "username": username,
                "is_admin": bool(is_admin), "is_active": True}

    def authenticate(self, username, password):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                ((username or "").strip(),),
            ).fetchone()
        if not row or not row["is_active"] or not isinstance(password, str):
            return None
        actual = self._password_hash(password, row["password_salt"])
        if not secrets.compare_digest(actual, row["password_hash"]):
            return None
        return self._user_dict(row)

    def create_session(self, user_id, ttl_seconds):
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock, self._db:
            self._db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
            self._db.execute(
                "INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (token_hash, user_id, now, now + ttl_seconds),
            )
        return token

    def user_from_session(self, token):
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute("""
                SELECT u.id, u.username, u.is_admin, u.is_active, u.created_at
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
            """, (token_hash, now)).fetchone()
            if row is None:
                self._db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))
        return self._user_dict(row)

    def revoke_session(self, token):
        if not token:
            return
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self._db:
            self._db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))

    def ensure_admin(self, username, password):
        """Create or promote a bootstrap admin without weakening normal registration."""
        username = (username or "").strip()
        if not username or not password:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
            ).fetchone()
        if row is None:
            return self.register_user(username, password, is_admin=True)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE users SET is_admin=1,is_active=1 WHERE id=?", (row["id"],))
            row = self._db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        return self._user_dict(row)

    def is_admin(self, user_id):
        with self._lock:
            row = self._db.execute(
                "SELECT is_admin,is_active FROM users WHERE id=?", (int(user_id),)
            ).fetchone()
        return bool(row and row["is_admin"] and row["is_active"])

    def user_can_access_device(self, user_id, device_id):
        return self.is_admin(user_id) or self.user_owns_device(user_id, device_id)

    def list_users_for_admin(self):
        with self._lock:
            rows = self._db.execute("""
                SELECT u.id,u.username,u.is_admin,u.is_active,u.created_at,
                       COUNT(DISTINCT d.device_id) AS device_count,
                       COUNT(DISTINCT s.token_hash) AS active_session_count
                FROM users u
                LEFT JOIN devices d ON d.owner_user_id=u.id
                LEFT JOIN user_sessions s ON s.user_id=u.id AND s.expires_at>?
                GROUP BY u.id ORDER BY u.created_at,u.id
            """, (time.time(),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_admin"] = bool(item["is_admin"])
            item["is_active"] = bool(item["is_active"])
            result.append(item)
        return result

    def update_user_for_admin(self, admin_user_id, user_id, *, is_admin=None,
                              is_active=None, password=None):
        admin_user_id = int(admin_user_id)
        user_id = int(user_id)
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("用户不存在")
            next_admin = bool(row["is_admin"] if is_admin is None else is_admin)
            next_active = bool(row["is_active"] if is_active is None else is_active)
            if user_id == admin_user_id and (not next_admin or not next_active):
                raise AccountError("不能取消或禁用当前管理员账号")
            if row["is_admin"] and row["is_active"] and (not next_admin or not next_active):
                remaining = self._db.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin=1 AND is_active=1 AND id<>?",
                    (user_id,)).fetchone()[0]
                if remaining == 0:
                    raise AccountError("系统必须保留至少一个可用管理员")
            self._db.execute(
                "UPDATE users SET is_admin=?,is_active=? WHERE id=?",
                (int(next_admin), int(next_active), user_id))
            if password is not None:
                if not isinstance(password, str) or not 8 <= len(password) <= 128:
                    raise AccountError("密码长度需为 8～128 个字符")
                salt = secrets.token_hex(16)
                self._db.execute(
                    "UPDATE users SET password_salt=?,password_hash=? WHERE id=?",
                    (salt, self._password_hash(password, salt), user_id))
            if not next_active or password is not None:
                self._db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
            updated = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._user_dict(updated)

    def delete_user_for_admin(self, admin_user_id, user_id):
        admin_user_id = int(admin_user_id)
        user_id = int(user_id)
        if admin_user_id == user_id:
            raise AccountError("不能删除当前管理员账号")
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("用户不存在")
            if row["is_admin"] and row["is_active"]:
                remaining = self._db.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin=1 AND is_active=1 AND id<>?",
                    (user_id,)).fetchone()[0]
                if remaining == 0:
                    raise AccountError("系统必须保留至少一个可用管理员")
            device_rows = self._db.execute(
                "SELECT device_id FROM devices WHERE owner_user_id=?", (user_id,)
            ).fetchall()
            for device in device_rows:
                code = self._new_binding_code_locked()
                self._db.execute("""
                    UPDATE devices SET owner_user_id=NULL,identifier=NULL,display_name=NULL,
                        binding_code=?,binding_code_expires_at=?,model_settings='{}',
                        vad_settings='{}',memory_enabled=1,interruption_settings='{}',bound_at=NULL
                    WHERE device_id=?
                """, (code, time.time() + BINDING_CODE_TTL_SECONDS,
                      device["device_id"]))
            self._db.execute("DELETE FROM users WHERE id=?", (user_id,))

    def audit_admin_action(self, admin_user_id, action, target_type, target_id,
                           details=None):
        with self._lock, self._db:
            self._db.execute("""
                INSERT INTO admin_audit_log(
                    admin_user_id,action,target_type,target_id,details,created_at)
                VALUES(?,?,?,?,?,?)
            """, (int(admin_user_id), str(action), str(target_type), str(target_id),
                  json.dumps(details or {}, ensure_ascii=False), time.time()))

    def list_admin_audit(self, limit=100):
        limit = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._db.execute("""
                SELECT a.id,a.admin_user_id,u.username AS admin_username,a.action,
                       a.target_type,a.target_id,a.details,a.created_at
                FROM admin_audit_log a LEFT JOIN users u ON u.id=a.admin_user_id
                ORDER BY a.id DESC LIMIT ?
            """, (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = self._decode_json(item["details"])
            result.append(item)
        return result

    @staticmethod
    def normalize_device_id(device_id):
        return (device_id or "").strip().lower()

    def touch_device(self, device_id, client_id=""):
        device_id = self.normalize_device_id(device_id)
        if not DEVICE_ID_RE.fullmatch(device_id):
            raise AccountError("设备 ID 无效")
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                code = self._new_binding_code_locked()
                self._db.execute("""
                    INSERT INTO devices(
                        device_id,client_id,binding_code,binding_code_expires_at,
                        created_at,last_seen)
                    VALUES(?,?,?,?,?,?)
                """, (device_id, client_id or "", code,
                      now + BINDING_CODE_TTL_SECONDS, now, now))
            else:
                if (row["owner_user_id"] is None and
                        (not BINDING_CODE_RE.fullmatch(row["binding_code"] or "") or
                         (row["binding_code_expires_at"] or 0) <= now)):
                    code = self._new_binding_code_locked()
                    self._db.execute("""
                        UPDATE devices SET client_id=?, last_seen=?, binding_code=?,
                            binding_code_expires_at=? WHERE device_id=?
                    """, (client_id or row["client_id"], now, code,
                          now + BINDING_CODE_TTL_SECONDS, device_id))
                else:
                    self._db.execute(
                        "UPDATE devices SET client_id = ?, last_seen = ? WHERE device_id = ?",
                        (client_id or row["client_id"], now, device_id),
                    )
            row = self._db.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._device_dict(row)

    def get_device(self, device_id):
        device_id = self.normalize_device_id(device_id)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._device_dict(row)

    def device_owner_id(self, device_id):
        device = self.get_device(device_id)
        return device["owner_user_id"] if device else None

    def user_owns_device(self, user_id, device_id):
        return self.device_owner_id(device_id) == int(user_id)

    def bind_device(self, user_id, device_id, binding_code, identifier, name):
        device_id = self.normalize_device_id(device_id)
        identifier = (identifier or "").strip()
        name = (name or "").strip()
        if not DEVICE_IDENTIFIER_RE.fullmatch(identifier):
            raise AccountError("设备识别码需为 1～40 位字母、数字、点、横线或下划线")
        if not 1 <= len(name) <= 64:
            raise AccountError("设备名称需为 1～64 个字符")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise AccountError("设备尚未连接服务器")
            if row["owner_user_id"] is not None and row["owner_user_id"] != user_id:
                raise PermissionError("设备已绑定其他用户")
            if row["owner_user_id"] is None and not secrets.compare_digest(
                    str(binding_code or ""), str(row["binding_code"] or "")):
                raise AccountError("设备绑定码不正确")
            try:
                self._db.execute("""
                    UPDATE devices SET owner_user_id=?, identifier=?, display_name=?,
                        binding_code=NULL, binding_code_expires_at=NULL,
                        bound_at=? WHERE device_id=?
                """, (user_id, identifier, name, time.time(), device_id))
            except sqlite3.IntegrityError as exc:
                raise AccountError("该设备识别码已被你的其他设备使用") from exc
            row = self._db.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._device_dict(row)

    def bind_device_by_code(self, user_id, binding_code):
        """Atomically claim the unbound device identified by a short-lived code."""
        code = str(binding_code or "").strip()
        if not BINDING_CODE_RE.fullmatch(code):
            raise AccountError("绑定码无效或已过期")
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute("""
                SELECT * FROM devices
                WHERE binding_code=? AND owner_user_id IS NULL
                    AND binding_code_expires_at > ?
            """, (code, now)).fetchone()
            if row is None:
                raise AccountError("绑定码无效或已过期")

            compact_id = re.sub(r"[^a-z0-9]", "", row["device_id"].lower())[-6:]
            base_identifier = "device-{}".format(compact_id or "new")
            identifier = base_identifier
            suffix = 2
            while self._db.execute("""
                    SELECT 1 FROM devices WHERE owner_user_id=? AND identifier=? COLLATE NOCASE
                """, (user_id, identifier)).fetchone():
                identifier = "{}-{}".format(base_identifier, suffix)
                suffix += 1
            device_count = self._db.execute(
                "SELECT COUNT(*) FROM devices WHERE owner_user_id=?", (user_id,)
            ).fetchone()[0]
            name = "我的设备 {}".format(device_count + 1)
            cursor = self._db.execute("""
                UPDATE devices SET owner_user_id=?, identifier=?, display_name=?,
                    binding_code=NULL, binding_code_expires_at=NULL, bound_at=?
                WHERE device_id=? AND owner_user_id IS NULL AND binding_code=?
            """, (user_id, identifier, name, now, row["device_id"], code))
            if cursor.rowcount != 1:
                raise AccountError("绑定码无效或已过期")
            row = self._db.execute(
                "SELECT * FROM devices WHERE device_id=?", (row["device_id"],)
            ).fetchone()
        return self._device_dict(row)

    def list_user_devices(self, user_id):
        with self._lock:
            if self.is_admin(user_id):
                rows = self._db.execute("""
                    SELECT d.*,u.username AS owner_username FROM devices d
                    LEFT JOIN users u ON u.id=d.owner_user_id
                    ORDER BY COALESCE(d.display_name,d.identifier,d.device_id) COLLATE NOCASE
                """).fetchall()
            else:
                rows = self._db.execute("""
                    SELECT d.*,u.username AS owner_username FROM devices d
                    LEFT JOIN users u ON u.id=d.owner_user_id
                    WHERE d.owner_user_id = ?
                    ORDER BY COALESCE(d.display_name,d.identifier,d.device_id) COLLATE NOCASE
                """, (user_id,)).fetchall()
        return [self._device_dict(row) for row in rows]

    def list_devices_owned_by(self, user_id):
        """Return exactly one user's devices, even when that user is an admin."""
        with self._lock:
            rows = self._db.execute("""
                SELECT d.*,u.username AS owner_username FROM devices d
                LEFT JOIN users u ON u.id=d.owner_user_id
                WHERE d.owner_user_id=?
                ORDER BY COALESCE(d.display_name,d.identifier,d.device_id) COLLATE NOCASE
            """, (int(user_id),)).fetchall()
        return [self._device_dict(row) for row in rows]

    def assign_device_for_admin(self, device_id, owner_user_id, identifier=None, name=None):
        device_id = self.normalize_device_id(device_id)
        owner_user_id = int(owner_user_id) if owner_user_id not in (None, "") else None
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if row is None:
                raise AccountError("设备不存在")
            if owner_user_id is None:
                code = self._new_binding_code_locked()
                self._db.execute("""
                    UPDATE devices SET owner_user_id=NULL,identifier=NULL,display_name=NULL,
                        binding_code=?,binding_code_expires_at=?,model_settings='{}',
                        vad_settings='{}',memory_enabled=1,interruption_settings='{}',bound_at=NULL
                    WHERE device_id=?
                """, (code, time.time() + BINDING_CODE_TTL_SECONDS, device_id))
            else:
                user = self._db.execute(
                    "SELECT id,is_active FROM users WHERE id=?", (owner_user_id,)).fetchone()
                if user is None or not user["is_active"]:
                    raise AccountError("目标用户不存在或已禁用")
                identifier = (identifier or row["identifier"] or
                              "device-{}".format(re.sub(r"[^a-z0-9]", "", device_id)[-6:]))
                name = (name or row["display_name"] or device_id).strip()
                if not DEVICE_IDENTIFIER_RE.fullmatch(identifier):
                    raise AccountError("设备识别码格式无效")
                if not 1 <= len(name) <= 64:
                    raise AccountError("设备名称需为 1～64 个字符")
                try:
                    self._db.execute("""
                        UPDATE devices SET owner_user_id=?,identifier=?,display_name=?,
                            binding_code=NULL,binding_code_expires_at=NULL,
                            model_settings='{}',vad_settings='{}',memory_enabled=1,
                            interruption_settings='{}',bound_at=?
                        WHERE device_id=?
                    """, (owner_user_id, identifier, name, time.time(), device_id))
                except sqlite3.IntegrityError as exc:
                    raise AccountError("该用户已有相同识别码的设备") from exc
        return self.get_device(device_id)

    def set_device_active_for_admin(self, device_id, is_active):
        device_id = self.normalize_device_id(device_id)
        if not isinstance(is_active, bool):
            raise AccountError("is_active 必须是布尔值")
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE devices SET is_active=? WHERE device_id=?",
                (int(is_active), device_id))
            if cursor.rowcount != 1:
                raise AccountError("设备不存在")
        return self.get_device(device_id)

    def delete_device_for_admin(self, device_id):
        device_id = self.normalize_device_id(device_id)
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
            if cursor.rowcount != 1:
                raise AccountError("设备不存在")

    def update_device(self, user_id, device_id, identifier, name):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        identifier = (identifier or "").strip()
        name = (name or "").strip()
        if not DEVICE_IDENTIFIER_RE.fullmatch(identifier):
            raise AccountError("设备识别码格式无效")
        if not 1 <= len(name) <= 64:
            raise AccountError("设备名称需为 1～64 个字符")
        try:
            with self._lock, self._db:
                self._db.execute(
                    "UPDATE devices SET identifier=?, display_name=? WHERE device_id=?",
                    (identifier, name, device_id),
                )
        except sqlite3.IntegrityError as exc:
            raise AccountError("该设备识别码已被你的其他设备使用") from exc
        return self.get_device(device_id)

    def unbind_device(self, user_id, device_id):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        with self._lock, self._db:
            code = self._new_binding_code_locked()
            self._db.execute("""
                UPDATE devices SET owner_user_id=NULL, identifier=NULL, display_name=NULL,
                    binding_code=?, binding_code_expires_at=?, model_settings='{}',
                    vad_settings='{}', memory_enabled=1, interruption_settings='{}', bound_at=NULL
                WHERE device_id=?
            """, (code, time.time() + BINDING_CODE_TTL_SECONDS, device_id))

    def get_model_settings(self, device_id):
        device_id = self.normalize_device_id(device_id)
        with self._lock:
            row = self._db.execute(
                "SELECT model_settings FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["model_settings"] or "{}")
        except (TypeError, ValueError):
            return {}
        return {key: value[key] for key in MODEL_SETTING_KEYS if key in value}

    def set_model_settings(self, user_id, device_id, settings):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        clean = {key: settings[key] for key in MODEL_SETTING_KEYS if key in settings}
        with self._lock, self._db:
            self._db.execute(
                "UPDATE devices SET model_settings=? WHERE device_id=?",
                (json.dumps(clean, ensure_ascii=False), device_id),
            )
        return clean

    def get_vad_settings(self, device_id):
        device_id = self.normalize_device_id(device_id)
        with self._lock:
            row = self._db.execute(
                "SELECT vad_settings FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["vad_settings"] or "{}")
        except (TypeError, ValueError):
            return {}
        return {key: value[key] for key in VAD_SETTING_KEYS if key in value}

    def set_vad_settings(self, user_id, device_id, settings):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        clean = {key: settings[key] for key in VAD_SETTING_KEYS if key in settings}
        with self._lock, self._db:
            self._db.execute(
                "UPDATE devices SET vad_settings=? WHERE device_id=?",
                (json.dumps(clean, ensure_ascii=False), device_id),
            )
        return clean

    def get_device_features(self, device_id):
        device_id = self.normalize_device_id(device_id)
        defaults = {
            "memory_enabled": True,
            "automatic_interrupt": True,
            "button_interrupt": True,
            "double_click_end": True,
        }
        with self._lock:
            row = self._db.execute("""
                SELECT memory_enabled, interruption_settings FROM devices WHERE device_id=?
            """, (device_id,)).fetchone()
        if not row:
            return defaults
        defaults["memory_enabled"] = bool(row["memory_enabled"])
        values = self._decode_json(row["interruption_settings"])
        for key in INTERRUPTION_SETTING_KEYS:
            if key in values:
                defaults[key] = bool(values[key])
        return defaults

    def set_device_features(self, user_id, device_id, settings):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        current = self.get_device_features(device_id)
        for key in current:
            if key in settings:
                current[key] = bool(settings[key])
        interruption = {key: current[key] for key in INTERRUPTION_SETTING_KEYS}
        with self._lock, self._db:
            self._db.execute("""
                UPDATE devices SET memory_enabled=?, interruption_settings=? WHERE device_id=?
            """, (int(current["memory_enabled"]),
                  json.dumps(interruption), device_id))
        return current

    def record_turn(self, device_id, user_text, assistant_text,
                    timeout_minutes=10, usage=None):
        device_id = self.normalize_device_id(device_id)
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text:
            return None
        owner_id = self.device_owner_id(device_id)
        if owner_id is None:
            return None
        now = time.time()
        usage = usage if isinstance(usage, dict) else {}
        def tokens(name):
            try:
                return max(0, int(usage.get(name, 0) or 0))
            except (TypeError, ValueError):
                return 0
        input_tokens = tokens("input_tokens")
        output_tokens = tokens("output_tokens")
        total_tokens = tokens("total_tokens") or input_tokens + output_tokens
        # Kept in the signature for compatibility with older callers. A chat
        # is now bounded only by standby -> active conversation -> standby,
        # never by elapsed time between two turns.
        _ = timeout_minutes
        with self._lock, self._db:
            self._db.execute("""
                INSERT INTO conversation_turns(
                    user_id,device_id,user_text,assistant_text,input_tokens,output_tokens,total_tokens,created_at)
                VALUES(?,?,?,?,?,?,?,?)
            """, (owner_id, device_id, user_text, assistant_text, input_tokens,
                  output_tokens, total_tokens, now))
            row = self._db.execute("""
                SELECT id,last_message_at FROM chat_sessions
                WHERE user_id=? AND device_id=? AND ended_at IS NULL
                ORDER BY id DESC LIMIT 1
            """, (owner_id, device_id)).fetchone()
            if row is None:
                cursor = self._db.execute("""
                    INSERT INTO chat_sessions(user_id,device_id,title,started_at,last_message_at)
                    VALUES(?,?,?,?,?)
                """, (owner_id, device_id, user_text[:40], now, now))
                conversation_id = cursor.lastrowid
            else:
                conversation_id = row["id"]
            self._db.executemany("""
                INSERT INTO chat_messages(conversation_id,role,content,created_at)
                VALUES(?,?,?,?)
            """, ((conversation_id, "user", user_text, now),
                  (conversation_id, "assistant", assistant_text, now)))
            self._db.execute(
                "UPDATE chat_sessions SET last_message_at=? WHERE id=?",
                (now, conversation_id))
        return {"conversation_id": conversation_id,
                "ended_conversation_id": None}

    @staticmethod
    def _usage_period_starts(now=None):
        now = float(now or time.time())
        local = time.localtime(now)
        day = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0,
                           local.tm_wday, local.tm_yday, local.tm_isdst))
        week = day - local.tm_wday * 86400
        month = time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0,
                             local.tm_wday, local.tm_yday, local.tm_isdst))
        return {"today": day, "week": week, "month": month, "all": 0}

    def usage_summary(self, user_id=None, device_id=None, now=None):
        """Return token-backed dialogue-turn totals for day/week/month/all."""
        starts = self._usage_period_starts(now)
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(int(user_id))
        if device_id is not None:
            clauses.append("device_id=?")
            params.append(self.normalize_device_id(device_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        result = {}
        with self._lock:
            for period, start in starts.items():
                period_where = where + (" AND " if where else " WHERE ") + "created_at>=?" if start else where
                row = self._db.execute("""
                    SELECT COUNT(*) AS turns, COALESCE(SUM(input_tokens),0) AS input_tokens,
                           COALESCE(SUM(output_tokens),0) AS output_tokens,
                           COALESCE(SUM(total_tokens),0) AS total_tokens
                    FROM conversation_turns{}""".format(period_where),
                    tuple(params + ([start] if start else []))).fetchone()
                result[period] = dict(row)
        return result

    def usage_by_user_device(self):
        starts = self._usage_period_starts()
        with self._lock:
            rows = self._db.execute("""
                SELECT t.user_id,u.username,t.device_id,
                       COUNT(*) AS turns, COALESCE(SUM(t.input_tokens),0) AS input_tokens,
                       COALESCE(SUM(t.output_tokens),0) AS output_tokens,
                       COALESCE(SUM(t.total_tokens),0) AS total_tokens,
                       SUM(CASE WHEN t.created_at>=? THEN 1 ELSE 0 END) AS today_turns,
                       COALESCE(SUM(CASE WHEN t.created_at>=? THEN t.total_tokens ELSE 0 END),0) AS today_tokens,
                       SUM(CASE WHEN t.created_at>=? THEN 1 ELSE 0 END) AS week_turns,
                       COALESCE(SUM(CASE WHEN t.created_at>=? THEN t.total_tokens ELSE 0 END),0) AS week_tokens,
                       SUM(CASE WHEN t.created_at>=? THEN 1 ELSE 0 END) AS month_turns,
                       COALESCE(SUM(CASE WHEN t.created_at>=? THEN t.total_tokens ELSE 0 END),0) AS month_tokens
                FROM conversation_turns t JOIN users u ON u.id=t.user_id
                GROUP BY t.user_id,t.device_id ORDER BY u.username,t.device_id
            """, (starts["today"], starts["today"], starts["week"], starts["week"],
                  starts["month"], starts["month"])).fetchall()
        return [dict(row) for row in rows]

    def end_conversation(self, device_id):
        device_id = self.normalize_device_id(device_id)
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute("""
                SELECT id FROM chat_sessions WHERE device_id=? AND ended_at IS NULL
                ORDER BY id DESC LIMIT 1
            """, (device_id,)).fetchone()
            if not row:
                return None
            self._db.execute(
                "UPDATE chat_sessions SET ended_at=? WHERE id=?", (now, row["id"]))
            return row["id"]

    def active_conversation_id(self, device_id):
        device_id = self.normalize_device_id(device_id)
        with self._lock:
            row = self._db.execute("""
                SELECT id FROM chat_sessions
                WHERE device_id=? AND ended_at IS NULL AND deleted_at IS NULL
                ORDER BY id DESC LIMIT 1
            """, (device_id,)).fetchone()
        return row["id"] if row else None

    def list_chat_sessions(self, user_id, device_id, limit=100):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        target_user_id = self.device_owner_id(device_id)
        if target_user_id is None:
            return []
        limit = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._db.execute("""
                SELECT s.id,s.device_id,s.title,s.started_at,s.last_message_at,s.ended_at,
                       COUNT(m.id) AS message_count
                FROM chat_sessions s LEFT JOIN chat_messages m ON m.conversation_id=s.id
                WHERE s.user_id=? AND s.device_id=? AND s.deleted_at IS NULL
                GROUP BY s.id
                ORDER BY s.last_message_at DESC LIMIT ?
            """, (target_user_id, device_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def get_chat_messages(self, user_id, conversation_id):
        with self._lock:
            if self.is_admin(user_id):
                session = self._db.execute(
                    "SELECT * FROM chat_sessions WHERE id=? AND deleted_at IS NULL",
                    (int(conversation_id),)).fetchone()
            else:
                session = self._db.execute(
                    """SELECT * FROM chat_sessions
                       WHERE id=? AND user_id=? AND deleted_at IS NULL""",
                    (int(conversation_id), user_id)).fetchone()
            if not session:
                raise PermissionError("无权访问该会话")
            rows = self._db.execute("""
                SELECT id,role,content,created_at FROM chat_messages
                WHERE conversation_id=? ORDER BY id
            """, (int(conversation_id),)).fetchall()
        return {"conversation": dict(session),
                "messages": [dict(row) for row in rows]}

    def conversation_for_memory(self, conversation_id):
        with self._lock:
            session = self._db.execute(
                "SELECT * FROM chat_sessions WHERE id=? AND deleted_at IS NULL",
                (int(conversation_id),)
            ).fetchone()
            if not session:
                return None
            messages = self._db.execute("""
                SELECT role,content,created_at FROM chat_messages
                WHERE conversation_id=? ORDER BY id
            """, (int(conversation_id),)).fetchall()
        return {"conversation": dict(session),
                "messages": [dict(row) for row in messages]}

    def mark_memory_updated(self, conversation_id):
        with self._lock, self._db:
            self._db.execute(
                """UPDATE chat_sessions SET memory_updated_at=?
                   WHERE id=? AND deleted_at IS NULL""",
                (time.time(), int(conversation_id)))

    def soft_delete_conversation(self, user_id, conversation_id):
        """Hide an ended chat from its owner while retaining an audit copy."""
        conversation_id = int(conversation_id)
        now = time.time()
        with self._lock, self._db:
            if self.is_admin(user_id):
                row = self._db.execute("""
                    SELECT id,device_id,ended_at FROM chat_sessions
                    WHERE id=? AND deleted_at IS NULL
                """, (conversation_id,)).fetchone()
            else:
                row = self._db.execute("""
                    SELECT id,device_id,ended_at FROM chat_sessions
                    WHERE id=? AND user_id=? AND deleted_at IS NULL
                """, (conversation_id, user_id)).fetchone()
            if not row:
                raise PermissionError("无权访问该会话")
            if row["ended_at"] is None:
                raise AccountError("请先结束当前会话，再删除这条记录")
            self._db.execute("""
                UPDATE chat_sessions SET deleted_at=?,deleted_by_user_id=? WHERE id=?
            """, (now, user_id, conversation_id))
        return {"id": conversation_id, "device_id": row["device_id"],
                "deleted_at": now}

    def list_deleted_chat_sessions(self, limit=100):
        """Internal administrative/audit view; never exposed to user APIs."""
        limit = max(1, min(1000, int(limit)))
        with self._lock:
            rows = self._db.execute("""
                SELECT id,user_id,device_id,title,started_at,last_message_at,
                       ended_at,memory_updated_at,deleted_at,deleted_by_user_id
                FROM chat_sessions WHERE deleted_at IS NOT NULL
                ORDER BY deleted_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_chat_messages_for_admin(self, conversation_id):
        """Internal audit access, including user-deleted conversations."""
        with self._lock:
            session = self._db.execute(
                "SELECT * FROM chat_sessions WHERE id=?", (int(conversation_id),)
            ).fetchone()
            if not session:
                return None
            messages = self._db.execute("""
                SELECT id,role,content,created_at FROM chat_messages
                WHERE conversation_id=? ORDER BY id
            """, (int(conversation_id),)).fetchall()
        return {"conversation": dict(session),
                "messages": [dict(row) for row in messages]}

    def list_memories(self, user_id, enabled_only=False):
        clause = " AND um.enabled=1" if enabled_only else ""
        with self._lock:
            rows = self._db.execute("""
                SELECT um.id,um.category,um.label,um.value,um.enabled,
                       um.source_conversation_id,um.created_at,um.updated_at
                FROM user_memories um
                LEFT JOIN chat_sessions source ON source.id=um.source_conversation_id
                WHERE um.user_id=?
                  AND (um.source_conversation_id IS NULL OR source.deleted_at IS NULL)
                  {} ORDER BY um.category,um.label
            """.format(clause), (user_id,)).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["enabled"] = bool(item["enabled"])
        return result

    def memory_prompt(self, user_id):
        rows = self.list_memories(user_id, enabled_only=True)
        return "\n".join("- {}：{}".format(row["label"], row["value"])
                         for row in rows)

    def upsert_memory(self, user_id, category, label, value, enabled=True,
                      source_conversation_id=None):
        category = (category or "其他").strip()[:40]
        label = (label or "信息").strip()[:80]
        value = (value or "").strip()[:1000]
        if not value:
            raise AccountError("记忆内容不能为空")
        now = time.time()
        with self._lock, self._db:
            self._db.execute("""
                INSERT INTO user_memories(user_id,category,label,value,enabled,
                                          source_conversation_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,category,label) DO UPDATE SET
                    value=excluded.value, enabled=excluded.enabled,
                    source_conversation_id=COALESCE(excluded.source_conversation_id,
                                                    user_memories.source_conversation_id),
                    updated_at=excluded.updated_at
            """, (user_id, category, label, value, int(bool(enabled)),
                  source_conversation_id, now, now))
            row = self._db.execute("""
                SELECT id,category,label,value,enabled,source_conversation_id,
                       created_at,updated_at FROM user_memories
                WHERE user_id=? AND category=? AND label=?
            """, (user_id, category, label)).fetchone()
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def update_memory(self, user_id, memory_id, **changes):
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM user_memories WHERE id=? AND user_id=?",
                (int(memory_id), user_id)).fetchone()
            if not row:
                raise PermissionError("无权访问该记忆")
            category = str(changes.get("category", row["category"])).strip()[:40]
            label = str(changes.get("label", row["label"])).strip()[:80]
            value = str(changes.get("value", row["value"])).strip()[:1000]
            enabled = int(bool(changes.get("enabled", row["enabled"])))
            if not value:
                raise AccountError("记忆内容不能为空")
            self._db.execute("""
                UPDATE user_memories SET category=?,label=?,value=?,enabled=?,updated_at=?
                WHERE id=? AND user_id=?
            """, (category, label, value, enabled, time.time(), int(memory_id), user_id))
        return self.list_memories(user_id)

    def delete_memory(self, user_id, memory_id):
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM user_memories WHERE id=? AND user_id=?",
                (int(memory_id), user_id))
            if cursor.rowcount != 1:
                raise PermissionError("无权访问该记忆")

    def list_conversations(self, user_id, device_id, limit=100, ascending=False):
        device_id = self.normalize_device_id(device_id)
        if not self.user_can_access_device(user_id, device_id):
            raise PermissionError("无权访问该设备")
        target_user_id = self.device_owner_id(device_id)
        if target_user_id is None:
            return []
        limit = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._db.execute("""
                WITH ordered AS (
                    SELECT m.id,s.device_id,m.role,m.content,m.created_at,
                           LEAD(m.role) OVER (
                               PARTITION BY m.conversation_id ORDER BY m.id
                           ) AS next_role,
                           LEAD(m.content) OVER (
                               PARTITION BY m.conversation_id ORDER BY m.id
                           ) AS next_content
                    FROM chat_sessions s
                    JOIN chat_messages m ON m.conversation_id=s.id
                    WHERE s.user_id=? AND s.device_id=? AND s.deleted_at IS NULL
                )
                SELECT id,device_id,content AS user_text,
                       next_content AS assistant_text,created_at
                FROM ordered WHERE role='user' AND next_role='assistant'
                ORDER BY id DESC LIMIT ?
            """, (target_user_id, device_id, limit)).fetchall()
        result = [dict(row) for row in rows]
        return list(reversed(result)) if ascending else result
