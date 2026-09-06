"""HTTP API：设备激活 / OTA 配置下发。

设备启动 -> GET /ota 拿 activation + websocket 配置
       -> POST /activate 激活
参考源码 ota.cc::CheckVersion()。
"""

import hashlib
import json
import logging
import time
import uuid

from aiohttp import web

log = logging.getLogger("http")


class HttpApi:
    def __init__(self, config: dict, account_store=None):
        self.config = config
        self.account_store = account_store
        # 设备 token 表: mac -> token
        self.device_tokens: dict = {}

    def _ws_config(self, device_id: str) -> dict:
        return {
            "url": self.config["server"]["public_ws_url"],
            "token": self._issue_token(device_id),
            "version": 3,
        }

    def _issue_token(self, device_id: str) -> str:
        # 稳定 token：同一设备（Device-Id/MAC）返回相同 token，
        # 避免设备每次重启拿到不同 token 导致鉴权/追踪不一致。
        # 若 device_id 未知，用随机 token 兜底。
        if device_id and len(device_id) > 3:
            if device_id not in self.device_tokens:
                self.device_tokens[device_id] = uuid.uuid4().hex
            return self.device_tokens[device_id]
        return uuid.uuid4().hex

    # ---- handlers ----
    async def ota(self, request: web.Request):
        # 设备请求 OTA 配置（首次启动/周期检查）
        # 设备会在 headers 里带 Device-Id（MAC）/ Client-Id（UUID）
        device_id = request.headers.get("Device-Id", request.headers.get("Client-Id", ""))
        client_id = request.headers.get("Client-Id", "")
        device = None
        if self.account_store and device_id:
            try:
                device = self.account_store.touch_device(device_id, client_id)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
        binding_code = device.get("binding_code") if device and not device.get("owner_user_id") else None
        challenge = None
        if binding_code:
            challenge = hashlib.sha256(
                (device["device_id"] + ":tongtong-bind").encode("utf-8")
            ).hexdigest()
        resp = {
            "firmware": {
                "version": "0.0.1",
                "url": "",  # 不强制升级
                "force": 0,
            },
            "activation": {
                "message": "请登录管理页面并输入此绑定码" if binding_code else "tongtong-omni-backend ready",
                "code": binding_code,
                "challenge": challenge,
                "timeout_ms": 60000,
            },
            "websocket": self._ws_config(device_id),
            "server_time": {
                "timestamp": int(time.time()),
                "timezone_offset": 480,
            },
        }
        return web.json_response(resp)

    async def activate(self, request: web.Request):
        device_id = request.headers.get("Device-Id", "")
        try:
            data = await request.json()
        except Exception:
            data = {}
        log.info("activate: device=%s payload_keys=%s", device_id, sorted(data))
        if self.account_store and not self.account_store.device_owner_id(device_id):
            return web.json_response({"message": "waiting for device binding"}, status=202)
        return web.json_response({"message": "ok"})

    # ---- 路由（挂到主 app，不创建 subapp）----
    def add_routes(self, app: web.Application):
        app.router.add_route("GET", "/ota", self.ota)
        app.router.add_route("POST", "/ota", self.ota)
        app.router.add_post("/activate", self.activate)
        app.router.add_get("/health", self.health)

    async def health(self, request):
        return web.json_response({"status": "ok", "time": time.time()})
