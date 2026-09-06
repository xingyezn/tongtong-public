"""Regression test for speaker-tail false turns after interruption."""

import asyncio
import json
import struct
import sys
import types

sys.path.insert(0, ".")

try:
    import opuslib  # noqa: F401
except Exception:
    opuslib_stub = types.ModuleType("opuslib")
    opuslib_stub.APPLICATION_AUDIO = 0
    opuslib_stub.Encoder = object
    opuslib_stub.Decoder = object
    sys.modules["opuslib"] = opuslib_stub

from app.session import (BIN_V1, DEVICE_SAMPLE_RATE, Session,
                         VAD_POST_PLAYBACK_DISCARD_FRAMES)


class FakeWs:
    closed = False

    async def send_str(self, _message):
        pass

    async def send_bytes(self, _message):
        pass

    async def close(self):
        self.closed = True


class FakeOmni:
    api_key = ""

    def new_for_session(self, *_args):
        return self

    async def close(self):
        pass


class PassthroughDecoder:
    def decode(self, payload, _frame_ms):
        return payload


async def main():
    config = {
        "dashscope": {"output_sample_rate": 24000},
        "vad": {"silence_duration_ms": 600, "energy_threshold": 600},
    }
    session = Session(FakeWs(), config, FakeOmni(), "vad-device")
    session.bin_version = BIN_V1
    session.device_decoder = PassthroughDecoder()
    starts = []
    session._maybe_start_omni = lambda: starts.append(len(session.up_pcm))

    session._next_listen_discard_frames = VAD_POST_PLAYBACK_DISCARD_FRAMES
    await session.on_text(json.dumps({"type": "listen", "state": "start"}))
    samples = DEVICE_SAMPLE_RATE * 60 // 1000
    loud = struct.pack("<h", 2000) * samples
    quiet = b"\0\0" * samples

    # Playback tail is discarded, and two remaining loud glitches are not
    # enough to establish speech.
    for _ in range(VAD_POST_PLAYBACK_DISCARD_FRAMES):
        await session.on_binary(loud)
    for _ in range(2):
        await session.on_binary(loud)
    for _ in range(12):
        await session.on_binary(quiet)
    assert starts == [] and session.listening

    # Three sustained speech frames followed by normal silence still trigger
    # immediately at the configured 600 ms endpoint.
    for _ in range(3):
        await session.on_binary(loud)
    for _ in range(10):
        await session.on_binary(quiet)
    assert len(starts) == 1 and not session.listening
    await session.close()
    print("ALL VAD INTERRUPTION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
