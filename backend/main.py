"""tongtong-omni-backend 入口。

自建童童后端 + Qwen-Omni 端到端语音：
  - HTTP: /ota /activate /health（设备激活）
  - WS:   /ws（语音会话 + MCP）
"""

import asyncio
import argparse
import json
import logging
import os

import yaml
from aiohttp import web

from app.account_store import AccountStore
from app.dashboard import BroadcastLogHandler, Dashboard
from app.http_api import HttpApi
from app.memory_service import MemoryService
from app.omni_client import OmniClient
from app.opus_codec import OpusCodec, opus_available
from app.ws_gateway import WsGateway

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@web.middleware
async def security_headers_middleware(request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    # The current dashboard embeds inline JS/CSS. Keep this compatible policy
    # now; it still restricts all external resources and frame embedding.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'")
    return response


def load_config(path: str = None) -> dict:
    path = path or os.path.join(BASE_DIR, "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # 允许环境变量覆盖（部署时用，不写死明文）
    env_key = os.environ.get("DASHSCOPE_API_KEY")
    if env_key:
        cfg["dashscope"]["api_key"] = env_key
    if os.environ.get("TONGTONG_SERVER_PORT"):
        cfg.setdefault("server", {})["port"] = int(os.environ["TONGTONG_SERVER_PORT"])
    if os.environ.get("TONGTONG_PUBLIC_WS_URL"):
        cfg.setdefault("server", {})["public_ws_url"] = os.environ["TONGTONG_PUBLIC_WS_URL"]
    if os.environ.get("TONGTONG_DATABASE"):
        cfg.setdefault("storage", {})["database"] = os.environ["TONGTONG_DATABASE"]
    if os.environ.get("TONGTONG_ENVIRONMENT"):
        cfg["environment"] = os.environ["TONGTONG_ENVIRONMENT"]
    return cfg


def save_config(config: dict, path: str = None) -> None:
    """把运行时配置持久化到 config.yaml（面板在线修改后保存，重启仍生效）。
    注意: API Key 仅从环境变量注入，不写回文件（避免明文落盘）。
    """
    path = path or os.path.join(BASE_DIR, "config.yaml")
    saved = None
    # 从环境变量来的 key 不落盘
    if os.environ.get("DASHSCOPE_API_KEY"):
        saved = config["dashscope"].get("api_key")
        config["dashscope"]["api_key"] = ""
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    finally:
        if saved is not None:
            config["dashscope"]["api_key"] = saved


async def main(config_path=None):
    config_path = config_path or os.environ.get("TONGTONG_CONFIG") or os.path.join(
        BASE_DIR, "config.yaml")
    if not os.path.isabs(config_path):
        config_path = os.path.abspath(config_path)
    config = load_config(config_path)

    database_path = config.get("storage", {}).get("database", "data/tongtong.db")
    if not os.path.isabs(database_path):
        database_path = os.path.join(BASE_DIR, database_path)
    account_store = AccountStore(database_path)
    bootstrap_username = os.environ.get("TONGTONG_ADMIN_USERNAME", "")
    bootstrap_password = os.environ.get("TONGTONG_ADMIN_PASSWORD", "")
    if bootstrap_username or bootstrap_password:
        if not bootstrap_username or not bootstrap_password:
            raise ValueError(
                "TONGTONG_ADMIN_USERNAME and TONGTONG_ADMIN_PASSWORD must be set together")
        account_store.ensure_admin(bootstrap_username, bootstrap_password)
    memory_service = MemoryService(config, account_store)

    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, config["logging"]["level"].upper(), logging.INFO),
        format=log_format,
    )
    log = logging.getLogger("main")
    config.setdefault("runtime", {})["opus_available"] = opus_available()
    if not opus_available():
        log.warning("未找到原生 libopus；管理接口可用，但设备语音编解码暂不可用")

    # 广播日志 handler（推给监控面板 SSE）
    log_handler = BroadcastLogHandler()
    log_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(log_handler)

    # 音频编解码（百炼输入/输出用，采样率按配置）
    input_codec = OpusCodec(config["dashscope"]["input_sample_rate"])
    output_codec = OpusCodec(config["dashscope"]["output_sample_rate"])

    # 统一编解码器（OmniClient 只用 pcm_to_omni_audio，不直接编解码）
    audio_codec = input_codec

    omni = OmniClient(config, audio_codec)
    sessions: dict = {}
    gateway = WsGateway(config, omni, sessions, account_store=account_store,
                        memory_service=memory_service)
    http_api = HttpApi(config, account_store=account_store)

    app = web.Application(middlewares=[security_headers_middleware])
    app.router.add_route("GET", "/ws", gateway.handle)
    http_api.add_routes(app)  # /ota /activate /health

    dashboard = Dashboard(config, sessions, http_api, log_handler, gateway.device_history,
                          save_config=lambda value: save_config(value, config_path),
                          account_store=account_store,
                          gateway=gateway, memory_service=memory_service)
    dashboard.add_routes(app)  # /  /api/status /api/logs /ws/echo

    loop = asyncio.get_event_loop()
    log_handler.attach(loop)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config["server"]["host"], config["server"]["port"])
    await site.start()

    log.info("tongtong-omni-backend 启动: http://%s:%d", 
             config["server"]["host"], config["server"]["port"])
    log.info("WS: %s", config["server"]["public_ws_url"])
    log.info("环境: %s, 数据库: %s", config.get("environment", "production"), database_path)

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await dashboard.close()
        await omni.close()
        await memory_service.close()
        await runner.cleanup()
        account_store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Tongtong backend")
    parser.add_argument("--config", help="path to an isolated runtime YAML configuration")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.config))
    except KeyboardInterrupt:
        pass
