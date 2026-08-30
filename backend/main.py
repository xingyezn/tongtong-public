"""tongtong-omni-backend 入口。

自建童童后端 + Qwen-Omni 端到端语音：
  - HTTP: /ota /activate /health（设备激活）
  - WS:   /ws（语音会话 + MCP）
"""

import asyncio
import json
import logging
import os

import yaml
from aiohttp import web

from app.dashboard import BroadcastLogHandler, Dashboard
from app.http_api import HttpApi
from app.omni_client import OmniClient
from app.opus_codec import OpusCodec
from app.ws_gateway import WsGateway

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str = None) -> dict:
    path = path or os.path.join(BASE_DIR, "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # 允许环境变量覆盖（部署时用，不写死明文）
    env_key = os.environ.get("DASHSCOPE_API_KEY")
    if env_key:
        cfg["dashscope"]["api_key"] = env_key
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


async def main():
    config = load_config()

    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, config["logging"]["level"].upper(), logging.INFO),
        format=log_format,
    )
    log = logging.getLogger("main")

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
    gateway = WsGateway(config, omni, sessions)
    http_api = HttpApi(config)

    app = web.Application()
    app.router.add_route("GET", "/ws", gateway.handle)
    http_api.add_routes(app)  # /ota /activate /health

    dashboard = Dashboard(config, sessions, http_api, log_handler, gateway.device_history,
                          save_config=save_config)
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

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await omni.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
