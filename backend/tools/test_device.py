"""模拟设备端调试工具（不依赖真实硬件）。

用法:
  python tools/test_device.py --url ws://127.0.0.1:8080/ws [--audio sample.wav]

行为:
  1. 连接 WS，发 hello（bin v3）
  2. 处理 initialize / tools/list（回 MCP 响应）
  3. 发 listen start，若给了音频文件则按 60ms/16k 编码发送
  4. 发 listen stop，等待服务器 tts 音频回包并播放/落盘
"""

import argparse
import asyncio
import json
import logging
import struct
import time

import opuslib
import websockets

log = logging.getLogger("test-device")

DEVICE_SAMPLE_RATE = 16000
FRAME_MS = 60
BIN_VERSION = 3


def encode_opus(pcm: bytes) -> bytes:
    enc = opuslib.Encoder(DEVICE_SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
    return enc.encode(pcm, FRAME_MS * DEVICE_SAMPLE_RATE // 1000)


def build_bin_frame(payload: bytes) -> bytes:
    return struct.pack(">BBH", 0, 0, len(payload)) + payload


def read_wav_pcm(path: str):
    import wave
    with wave.open(path, "rb") as w:
        assert w.getframerate() == DEVICE_SAMPLE_RATE, "音频采样率需 16k"
        assert w.getnchannels() == 1, "需单声道"
        return w.readframes(w.getnframes())


async def run(url: str, audio_path: str, duration: float):
    async with websockets.connect(url, max_size=64 * 1024 * 1024) as ws:
        # 0. 先启动 reader（真实设备连上就监听）
        received = {"hello_ack": False, "frames": 0}

        async def reader():
            async for msg in ws:
                if isinstance(msg, str):
                    data = json.loads(msg)
                    mtype = data.get("type")
                    if mtype == "hello":
                        received["hello_ack"] = True
                        log.info("S<- hello ACK: %s",
                                 json.dumps(data, ensure_ascii=False)[:200])
                    else:
                        log.info("S<- %s", json.dumps(data, ensure_ascii=False)[:200])
                    if mtype == "mcp":
                        payload = data["payload"]
                        method = payload.get("method")
                        if method == "initialize":
                            await ws.send(json.dumps({
                                "type": "mcp", "payload": {
                                    "jsonrpc": "2.0", "id": payload.get("id"),
                                    "result": {"protocolVersion": "2024-11-05",
                                               "capabilities": {"tools": {}},
                                               "serverInfo": {"name": "test-device",
                                                              "version": "0.0.1"}}}}))
                        elif method == "tools/list":
                            await ws.send(json.dumps({
                                "type": "mcp", "payload": {
                                    "jsonrpc": "2.0", "id": payload.get("id"),
                                    "result": {"tools": [
                                        {"name": "self.lamp.turn_on",
                                         "description": "Turn on the lamp",
                                         "inputSchema": {"type": "object",
                                                        "properties": {}}},
                                        {"name": "self.lamp.get_state",
                                         "description": "Get lamp state",
                                         "inputSchema": {"type": "object",
                                                        "properties": {}}},
                                    ], "nextCursor": ""}}}))
                        elif method == "tools/call":
                            name = payload["params"]["name"]
                            result_text = "true" if name == "self.lamp.turn_on" \
                                else "{\"power\":false}"
                            await ws.send(json.dumps({
                                "type": "mcp", "payload": {
                                    "jsonrpc": "2.0", "id": payload.get("id"),
                                    "result": {"content": [
                                        {"type": "text", "text": result_text}],
                                        "isError": False}}}))
                else:
                    # 二进制音频帧
                    if len(msg) >= 4:
                        t, resv, size = struct.unpack_from(">BBH", msg, 0)
                        payload = msg[4:4 + size]
                        received["frames"] += 1
                        log.info("S<- audio opus %d bytes (tts downlink)", len(payload))
        reader_task = asyncio.create_task(reader())

        # 1. hello
        await ws.send(json.dumps({
            "type": "hello", "version": BIN_VERSION,
            "features": {"mcp": True}, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": DEVICE_SAMPLE_RATE,
                             "channels": 1, "frame_duration": FRAME_MS},
        }))
        log.info("hello sent")

        # 3. listen start
        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
        log.info("listen start")

        if audio_path:
            pcm = read_wav_pcm(audio_path)
            frame_bytes = DEVICE_SAMPLE_RATE * FRAME_MS // 1000 * 2
            for i in range(0, len(pcm), frame_bytes):
                chunk = pcm[i:i + frame_bytes]
                if len(chunk) < frame_bytes:
                    chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
                opus = encode_opus(chunk)
                await ws.send(build_bin_frame(opus))
            log.info("audio sent: %d frames", len(pcm) // frame_bytes)
        else:
            # 无音频：持续发送静音
            silence = b"\x00\x00" * (DEVICE_SAMPLE_RATE * FRAME_MS // 1000)
            end = time.time() + duration
            while time.time() < end:
                await ws.send(build_bin_frame(encode_opus(silence)))
                await asyncio.sleep(FRAME_MS / 1000)

        # 4. listen stop
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))
        log.info("listen stop")

        # 5. 等待服务端回复
        await asyncio.sleep(15)
        reader_task.cancel()
        log.info("RESULT: hello_ack=%s, downlink_audio_frames=%d",
                 received["hello_ack"], received["frames"])
        assert received["hello_ack"], "未收到服务器 hello ACK！"


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    p.add_argument("--audio", default="", help="16k 单声道 wav 文件（真实语音）")
    p.add_argument("--duration", type=float, default=3.0, help="无音频时的静音时长(秒)")
    args = p.parse_args()
    asyncio.run(run(args.url, args.audio, args.duration))


if __name__ == "__main__":
    main()
