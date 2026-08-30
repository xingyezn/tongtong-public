"""OPUS 编解码与重采样模块。

设备端: OPUS 16kHz / 60ms 帧 (上行)
服务器: OPUS 24kHz / 60ms 帧 (下行，见 websocket.md audio_params)
百炼:  wav/pcm/opus (audio in/out)

依赖 libopus 系统库 (apt install libopus-dev)。
"""

import array
import struct

import opuslib

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class OpusCodec:
    """OPUS 编码器/解码器封装（单声道）。"""

    def __init__(self, sample_rate: int, channels: int = 1, bitrate: int = None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bitrate = bitrate
        self._encoder = None
        self._decoder = None

    # ---- 编码 ----
    def _ensure_encoder(self):
        if self._encoder is None:
            self._encoder = opuslib.Encoder(
                self.sample_rate, self.channels, opuslib.APPLICATION_AUDIO
            )
            # 指定码率（若给出）。24k 音频推荐 48-64kbps，保证音质
            if self.bitrate:
                self._encoder.bitrate = self.bitrate
        return self._encoder

    def encode(self, pcm: bytes, frame_ms: int = 60) -> bytes:
        """PCM 16bit 小端 -> OPUS 帧"""
        enc = self._ensure_encoder()
        return enc.encode(pcm, frame_ms * self.sample_rate // 1000)

    # ---- 解码 ----
    def _ensure_decoder(self):
        if self._decoder is None:
            self._decoder = opuslib.Decoder(self.sample_rate, self.channels)
        return self._decoder

    def decode(self, opus: bytes, frame_ms: int = 60) -> bytes:
        """OPUS 帧 -> PCM 16bit 小端"""
        dec = self._ensure_decoder()
        return dec.decode(opus, frame_ms * self.sample_rate // 1000)


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性插值重采样（16k<->24k 足够用，避免引入重依赖）。

    返回 PCM 16bit 小端。
    """
    if src_rate == dst_rate:
        return pcm
    if np is None:  # pragma: no cover
        raise RuntimeError("numpy 未安装，无法重采样")
    data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    ratio = dst_rate / src_rate
    n_out = int(len(data) * ratio)
    x_old = np.linspace(0, len(data) - 1, num=n_out)
    x_new = np.arange(len(data))
    out = np.interp(x_old, x_new, data).astype(np.int16)
    return out.tobytes()


# ---------------------------------------------------------------------------
# WAV 打包/解包（百炼 audio 输入输出用）
# ---------------------------------------------------------------------------

def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """PCM 16bit 小端 -> WAV 文件字节"""
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        sample_rate * channels * bits // 8,
        channels * bits // 8, bits,
        b"data", data_size,
    )
    return header + pcm


def wav_to_pcm(wav: bytes):
    """WAV 文件字节 -> (PCM 16bit 小端, sample_rate)。仅支持 PCM 格式。"""
    if len(wav) < 44:
        raise ValueError("WAV too short")
    channels = struct.unpack("<H", wav[22:24])[0]
    sample_rate = struct.unpack("<I", wav[24:28])[0]
    bits = struct.unpack("<H", wav[34:36])[0]
    # 找到 data 块
    offset = 12
    data_size = None
    while offset + 8 <= len(wav):
        chunk_id = wav[offset:offset + 4]
        chunk_size = struct.unpack("<I", wav[offset + 4:offset + 8])[0]
        if chunk_id == b"data":
            data_size = chunk_size
            break
        offset += 8 + chunk_size
    if data_size is None:
        raise ValueError("No data chunk")
    pcm = wav[offset + 8:offset + 8 + data_size]
    if bits == 16:
        pass
    elif bits == 8:
        # unsigned 8bit -> signed 16bit
        arr = array.array("B", pcm)
        out = array.array("h", ((b - 128) << 8 for b in arr))
        pcm = out.tobytes()
    else:
        raise ValueError(f"Unsupported bits: {bits}")
    return pcm, sample_rate
