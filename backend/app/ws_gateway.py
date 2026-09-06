"""WS 网关：处理设备 WebSocket 连接。

握手鉴权（Authorization: Bearer <token>） -> Session 创建 -> 消息分发。
"""

import asyncio
import copy
import json
import logging
import time
from typing import Optional

from aiohttp import web, WSMsgType

from .session import Session
from .mcp_bridge import McpBridge

log = logging.getLogger("ws")


class WsGateway:
    def __init__(self, config: dict, omni, sessions: dict, account_store=None,
                 memory_service=None):
        self.config = config
        self.omni = omni
        self.sessions: dict = sessions  # device_id -> Session
        self.device_history: dict = {}   # device_id -> {last_seen, connected_at, client_id}
        self.device_conversations: dict = {}  # device_id -> persistent text memory
        self.account_store = account_store
        self.memory_service = memory_service

    def _device_config(self, device_id: str) -> dict:
        config = copy.deepcopy(self.config)
        if self.account_store:
            config.setdefault("dashscope", {}).update(
                self.account_store.get_model_settings(device_id))
            config.setdefault("dashscope", {})["tool_instructions"] = (
                self.account_store.get_global_setting("tool_instructions"))
            config.setdefault("vad", {}).update(
                self.account_store.get_vad_settings(device_id))
            features = self.account_store.get_device_features(device_id)
            config["features"] = features
            owner_id = self.account_store.device_owner_id(device_id)
            if owner_id is not None and features.get("memory_enabled"):
                config.setdefault("dashscope", {})["user_memory_prompt"] = (
                    self.account_store.memory_prompt(owner_id))
            else:
                config.setdefault("dashscope", {}).pop("user_memory_prompt", None)
        return config

    def _conversation_memory(self, device_id: str) -> dict:
        owner_id = (self.account_store.device_owner_id(device_id)
                    if self.account_store else None)
        # A device WebSocket opens while the device is in standby. Previous
        # chat sessions are deliberately not restored: only permanent memory
        # crosses a standby boundary.
        memory = {
            "turns": [],
            "last_activity": 0.0,
            "owner_user_id": owner_id,
        }
        self.device_conversations[device_id] = memory
        return memory

    def _end_disconnected_conversation(self, device_id):
        if not self.account_store:
            return None
        conversation_id = self.account_store.end_conversation(device_id)
        if conversation_id and self.memory_service:
            asyncio.create_task(self.summarize_conversation(conversation_id))
        return conversation_id

    async def summarize_conversation(self, conversation_id):
        if not self.memory_service:
            return []
        saved = await self.memory_service.summarize_conversation(conversation_id)
        data = (self.account_store.conversation_for_memory(conversation_id)
                if self.account_store else None)
        if data:
            device_id = data["conversation"]["device_id"]
            session = self.sessions.get(device_id)
            if session:
                updated = self._device_config(device_id)
                session.config["dashscope"] = updated["dashscope"]
                session.config["features"] = updated.get("features", {})
        return saved

    def _check_auth(self, headers) -> bool:
        if not self.config["devices"]["enabled"]:
            return True
        token = headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        tokens = self.config["devices"]["tokens"]
        return token in tokens.values()

    async def handle(self, request: web.Request):
        device_id = request.headers.get("Device-Id", "").strip()
        if not device_id:
            return web.Response(status=400, text="Device-Id header is required")
        client_id = request.headers.get("Client-Id", "")
        device_record = None
        if self.account_store:
            try:
                device_record = self.account_store.touch_device(device_id, client_id)
            except ValueError as exc:
                return web.Response(status=400, text=str(exc))
            device_id = device_record["device_id"]
            if not device_record.get("is_active", True):
                log.warning("disabled device connection rejected: %s", device_id)
                return web.Response(status=403, text="device disabled")
        if not self._check_auth(request.headers):
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)

        # 防止同一设备重复连接：先关闭旧会话
        old = self.sessions.pop(device_id, None)
        if old:
            try:
                await old.close()
            except Exception:
                pass
            # Reconnection puts the firmware into standby, so it is also an
            # explicit boundary for the persisted chat session.
            self._end_disconnected_conversation(device_id)

        binding_code = (
            device_record.get("binding_code")
            if device_record and device_record.get("owner_user_id") is None else None
        )
        device_config = self._device_config(device_id)
        turn_recorder = None
        if self.account_store:
            turn_recorder = lambda did, user_text, assistant_text, usage=None: (
                self.account_store.record_turn(
                    did, user_text, assistant_text,
                    device_config.get("dashscope", {}).get(
                        "conversation_timeout_minutes", 10), usage))
        session = Session(
            ws, device_config, self.omni, device_id,
            conversation_memory=self._conversation_memory(device_id),
            binding_code=binding_code,
            turn_recorder=turn_recorder,
            conversation_ender=(self.account_store.end_conversation
                                if self.account_store else None),
            conversation_ended_callback=(
                self.summarize_conversation
                if self.memory_service else None),
        )
        mcp = McpBridge(session.send_json)
        session.set_mcp(mcp)
        self.sessions[device_id] = session
        # 记录设备历史（用于面板显示最近在线，即使当前断开）
        self.device_history[device_id] = {
            "last_seen": time.time(),
            "client_id": client_id,
            "owner_user_id": device_record.get("owner_user_id") if device_record else None,
        }
        log.info("device %s connected (client=%s)", device_id, client_id)

        # 握手：等设备 hello，回 hello ack
        # 设备连上后会立即发 hello；这里在 on_text 里自动回 ack

        # 初始化 MCP：拿设备工具表
        if not binding_code:
            await session.send_json(mcp.make_initialize(session.camera_capabilities()))
            await session.send_json(mcp.make_tools_list())
        else:
            log.info("device %s is waiting for user binding", device_id)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await session.on_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await session.on_binary(msg.data)
                elif msg.type == WSMsgType.CLOSE:
                    break
                elif msg.type == WSMsgType.ERROR:
                    log.warning("ws error: %s", ws.exception())
                    break
        except asyncio.CancelledError:
            pass
        finally:
            # A reconnect may already have installed a newer Session for this
            # device. Do not let the old connection remove the replacement.
            was_current = self.sessions.get(device_id) is session
            if was_current:
                self.sessions.pop(device_id, None)
            await session.close()
            if was_current:
                self._end_disconnected_conversation(device_id)
            if device_id in self.device_history:
                self.device_history[device_id]["last_seen"] = time.time()
            log.info("device %s disconnected", device_id)
        return ws
