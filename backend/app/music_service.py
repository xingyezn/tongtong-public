"""Local, openly licensed music catalog used by the backend MCP tools.

The service deliberately keeps the audio files outside the source tree.  A
catalog entry contains the original source and license so that the dashboard
or a future device UI can show attribution.  The ESP32 player can later use
the returned preview URL without knowing which provider supplied the track.
"""

import json
import logging
import mimetypes
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from .music_transcoder import MusicTranscoder

log = logging.getLogger("music")

MUSIC_TOOL_PREFIXES = ("server.music.",)

MUSIC_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "server.music.search",
        "description": (
            "在后端备用音乐库中搜索歌曲。只返回已部署且带有授权信息的音乐。"
            "如果用户要播放歌曲，应先搜索，再调用 server.music.play。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌曲名、艺人名或关键词"},
                "limit": {"type": "integer", "description": "最多返回数量，默认 5，最大 10"},
            },
            "required": ["query"],
        },
    },
}

MUSIC_PLAY_TOOL = {
    "type": "function",
    "function": {
        "name": "server.music.play",
        "description": (
            "准备播放备用音乐库中的一首歌曲，返回歌曲信息和最多 30 秒的预览地址。"
            "当前后端只负责解析和提供音频地址，设备端播放器接入后才能实际播放。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string", "description": "搜索结果中的歌曲 ID"},
                "start_seconds": {"type": "number", "description": "预览起始秒数，默认 0"},
                "duration_seconds": {"type": "number", "description": "预览时长，最多 30 秒"},
            },
            "required": ["track_id"],
        },
    },
}

MUSIC_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "server.music.list",
        "description": "列出后端备用音乐库中的歌曲。",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回数量，默认 10，最大 20"},
            },
        },
    },
}

MUSIC_TOOLS = [MUSIC_SEARCH_TOOL, MUSIC_PLAY_TOOL, MUSIC_LIST_TOOL]


class MusicService:
    """Read-only catalog and local audio delivery service."""

    def __init__(self, config=None):
        config = config or {}
        settings = config.get("music", {})
        self.catalog_path = self._resolve_path(
            settings.get("catalog_path", "data/music/catalog.json"))
        self.audio_dir = self._resolve_path(
            settings.get("audio_directory", "data/music/audio"))
        self.opus_dir = self._resolve_path(
            settings.get("opus_directory", "data/music/opus"))
        self.max_preview_seconds = max(
            1, min(30, int(settings.get("max_preview_seconds", 30))))
        self.public_base = self._public_base(config)
        self.transcoder = MusicTranscoder(
            self.audio_dir, self.opus_dir, self.max_preview_seconds)
        self._catalog_mtime = None
        self._tracks = []

    @staticmethod
    def _resolve_path(value):
        path = Path(str(value))
        return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path

    @staticmethod
    def _public_base(config):
        value = str(config.get("server", {}).get("public_ws_url", "")).strip()
        if value.startswith("wss://"):
            value = "https://" + value[6:]
        elif value.startswith("ws://"):
            value = "http://" + value[5:]
        if value.endswith("/ws"):
            value = value[:-3]
        return value.rstrip("/")

    def _load(self):
        try:
            stat = self.catalog_path.stat()
        except FileNotFoundError:
            self._tracks = []
            self._catalog_mtime = None
            return
        if self._catalog_mtime == stat.st_mtime_ns:
            return
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            tracks = payload.get("tracks", []) if isinstance(payload, dict) else payload
            self._tracks = [item for item in tracks if self._valid_track(item)]
            self._catalog_mtime = stat.st_mtime_ns
        except (OSError, ValueError, TypeError) as exc:
            log.warning("unable to load music catalog %s: %s", self.catalog_path, exc)
            self._tracks = []

    @staticmethod
    def _valid_track(track):
        return (isinstance(track, dict) and isinstance(track.get("id"), str)
                and isinstance(track.get("title"), str)
                and isinstance(track.get("filename"), str))

    def _track(self, track_id):
        self._load()
        return next((item for item in self._tracks if item["id"] == str(track_id)), None)

    @staticmethod
    def _public_track(track):
        return {key: value for key, value in track.items() if key != "filename"}

    def _url(self, track, start_seconds=0, duration_seconds=None):
        params = ["start_seconds={}".format(max(0, float(start_seconds)))]
        if duration_seconds is not None:
            params.append("duration_seconds={}".format(
                max(1, min(self.max_preview_seconds, float(duration_seconds)))))
        return "{}/api/music/{}/preview?{}".format(
            self.public_base, quote(track["id"], safe=""), "&".join(params))

    async def call(self, name, arguments):
        arguments = arguments or {}
        self._load()
        if name == "server.music.list":
            try:
                limit = max(1, min(20, int(arguments.get("limit", 10))))
            except (TypeError, ValueError):
                limit = 10
            return {"tracks": [self._public_track(item) for item in self._tracks[:limit]],
                    "count": len(self._tracks)}
        if name == "server.music.search":
            query = str(arguments.get("query") or "").strip().lower()
            if not query or len(query) > 100:
                return {"error": "query is required and must be at most 100 characters"}
            try:
                limit = max(1, min(10, int(arguments.get("limit", 5))))
            except (TypeError, ValueError):
                limit = 5
            matches = []
            for track in self._tracks:
                haystack = " ".join(str(track.get(key, "")) for key in
                                     ("title", "artist", "album", "tags")).lower()
                if query in haystack:
                    matches.append(self._public_track(track))
            return {"query": query, "tracks": matches[:limit], "count": len(matches)}
        if name == "server.music.play":
            track = self._track(arguments.get("track_id"))
            if track is None:
                return {"error": "track not found", "track_id": arguments.get("track_id")}
            try:
                start = max(0, float(arguments.get("start_seconds", 0)))
                duration = max(1, min(self.max_preview_seconds,
                                      float(arguments.get("duration_seconds", self.max_preview_seconds))))
            except (TypeError, ValueError):
                return {"error": "start_seconds and duration_seconds must be numbers"}
            result = self._public_track(track)
            result.update({"preview_url": self._url(track, start, duration),
                           "opus_url": self._opus_url(track),
                           "audio_format": "opus_ogg",
                           "sample_rate": 24000,
                           "channels": 1,
                           "start_seconds": start,
                           "duration_seconds": duration,
                           "max_preview_seconds": self.max_preview_seconds,
                           "backend_only": True})
            return result
        return {"error": "unknown server tool {}".format(name)}

    def add_routes(self, app):
        app.router.add_get("/api/music/catalog", self.http_catalog)
        app.router.add_get("/api/music/search", self.http_search)
        app.router.add_get("/api/music/{track_id}/preview", self.http_preview)
        app.router.add_get("/api/music/{track_id}/opus", self.http_opus)

    async def http_catalog(self, request):
        self._load()
        return web.json_response({"tracks": [self._public_track(item) for item in self._tracks],
                                  "count": len(self._tracks)})

    async def http_search(self, request):
        result = await self.call("server.music.search", {
            "query": request.query.get("query", ""),
            "limit": request.query.get("limit", 10),
        })
        status = 400 if "error" in result else 200
        return web.json_response(result, status=status)

    async def http_preview(self, request):
        track = self._track(request.match_info["track_id"])
        if track is None:
            raise web.HTTPNotFound(text="music track not found")
        path = (self.audio_dir / Path(track["filename"]).name).resolve()
        if self.audio_dir.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound(text="music file not found")
        response = web.FileResponse(path, headers={
            "Cache-Control": "public, max-age=3600",
            "X-Music-Preview-Max-Seconds": str(self.max_preview_seconds),
            "X-Music-Source": str(track.get("source_url", "")),
            "X-Music-License": str(track.get("license", "")),
        })
        content_type = mimetypes.guess_type(path.name)[0]
        if content_type:
            response.content_type = content_type
        return response

    def _opus_url(self, track):
        return "{}/api/music/{}/opus".format(
            self.public_base, quote(track["id"], safe=""))

    async def http_opus(self, request):
        track = self._track(request.match_info["track_id"])
        if track is None:
            raise web.HTTPNotFound(text="music track not found")
        source = (self.audio_dir / Path(track["filename"]).name).resolve()
        output = self.transcoder.output_path(track["filename"]).resolve()
        if self.audio_dir.resolve() not in source.parents:
            raise web.HTTPNotFound(text="music file not found")
        if self.opus_dir.resolve() not in output.parents:
            raise web.HTTPNotFound(text="music output path invalid")
        try:
            await self.transcoder.ensure(source, output)
        except FileNotFoundError:
            raise web.HTTPNotFound(text="music source file not found")
        except Exception as exc:
            log.warning("music opus transcode failed for %s: %s", track["id"], exc)
            raise web.HTTPBadGateway(text="music opus transcode unavailable")
        return web.FileResponse(output, headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Type": "audio/ogg",
            "X-Music-Codec": "opus",
            "X-Music-Sample-Rate": "24000",
            "X-Music-Channels": "1",
            "X-Music-Source": str(track.get("source_url", "")),
            "X-Music-License": str(track.get("license", "")),
        })
