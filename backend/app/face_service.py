"""HTTP client for the server-side face recognition service."""

import json
import logging
import asyncio

import aiohttp

log = logging.getLogger("face-service")


class FaceService:
    def __init__(self, config):
        settings = config.get("face_service", {})
        self.base_url = (settings.get(
            "base_url", "http://127.0.0.1:8090").rstrip("/"))
        self.username = settings.get("username", "")
        self.password = settings.get("password", "")
        self._login_lock = asyncio.Lock()
        self._authenticated = False

    async def _login(self, http_session):
        async with self._login_lock:
            if self._authenticated:
                return True
            async with http_session.post(
                    self.base_url + "/login",
                    data={"username": self.username, "password": self.password},
                    allow_redirects=True, timeout=15) as response:
                self._authenticated = response.status < 400
                if not self._authenticated:
                    log.error("face service login failed: HTTP %s", response.status)
                return self._authenticated

    async def request(self, http_session, method, path, *, image=None,
                      fields=None, json_body=None):
        url = self.base_url + path
        for attempt in range(2):
            data = None
            if image is not None or fields:
                data = aiohttp.FormData()
                for key, value in (fields or {}).items():
                    if value is not None:
                        data.add_field(key, str(value))
                if image is not None:
                    data.add_field("image", image, filename="camera.jpg",
                                   content_type="image/jpeg")
            async with http_session.request(method, url, data=data,
                                            json=json_body, timeout=30) as response:
                raw = await response.text()
                try:
                    result = json.loads(raw)
                except (TypeError, ValueError):
                    result = {"raw": raw}
                if response.status != 401 or attempt:
                    if response.status >= 400:
                        return {"error": "face service HTTP {}".format(response.status),
                                "detail": result}
                    return result
            self._authenticated = False
            if not await self._login(http_session):
                return {"error": "face service authentication failed"}
