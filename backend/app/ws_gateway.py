"""WS 网关：处理设备 WebSocket 连接。

握手鉴权（Authorization: Bearer <token>） -> Session 创建 -> 消息分发。
"""

import asyncio
import json
import logging
import time
from typing import Optional

from aiohttp import web, WSMsgType

from .session import Session
from .mcp_bridge import McpBridge

log = logging.getLogger("ws")


class WsGateway:
    def __init__(self, config: dict, omni, sessions: dict):
        self.config = config
        self.omni = omni
        self.sessions: dict = sessions  # device_id -> Session
        self.device_history: dict = {}   # device_id -> {last_seen, connected_at, client_id}

    def _check_auth(self, headers) -> bool:
        if not self.config["devices"]["enabled"]:
            return True
        token = headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        tokens = self.config["devices"]["tokens"]
        return token in tokens.values()

    async def handle(self, request: web.Request):
        device_id = request.headers.get("Device-Id", "unknown")
        client_id = request.headers.get("Client-Id", "")
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

        session = Session(ws, self.config, self.omni, device_id)
        mcp = McpBridge(session.send_json)
        session.set_mcp(mcp)
        self.sessions[device_id] = session
        # 记录设备历史（用于面板显示最近在线，即使当前断开）
        self.device_history[device_id] = {
            "last_seen": time.time(),
            "client_id": client_id,
        }
        log.info("device %s connected (client=%s)", device_id, client_id)

        # 握手：等设备 hello，回 hello ack
        # 设备连上后会立即发 hello；这里在 on_text 里自动回 ack

        # 初始化 MCP：拿设备工具表
        await session.send_json(mcp.make_initialize())
        await session.send_json(mcp.make_tools_list())

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
            self.sessions.pop(device_id, None)
            await session.close()
            if device_id in self.device_history:
                self.device_history[device_id]["last_seen"] = time.time()
            log.info("device %s disconnected", device_id)
        return ws
