import struct
import math
import wave
import os

SR = 16000


def tone(freq, dur):
    n = int(SR * dur)
    return [int(15000 * math.sin(2 * math.pi * freq * i / SR)) for i in range(n)]


def silence(dur):
    return [0] * int(SR * dur)


# 组合：3 段不同频率模拟语音节奏（非真实语音，仅用于链路测试）
pcm = (tone(440, 0.6) + silence(0.2) +
       tone(660, 0.6) + silence(0.2) +
       tone(880, 0.6))

out = os.path.join("tools", "sample_speech.wav")
with wave.open(out, "w") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(b"".join(struct.pack("<h", v) for v in pcm))
print(f"generated {out}: {len(pcm) / SR:.2f} sec")
