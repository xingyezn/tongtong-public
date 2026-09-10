"""Backend-side audio transcoding for the music stream.

The device already has an Opus decoder for TTS.  Keeping source decoding and
Opus encoding here avoids adding MP3/AAC decoder memory and CPU cost to the
ESP32 firmware.
"""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

from .opus_codec import OpusCodec

log = logging.getLogger("music.transcoder")


class MusicTranscoder:
    def __init__(self, audio_dir, opus_dir=None, max_seconds=30):
        self.audio_dir = Path(audio_dir)
        self.opus_dir = Path(opus_dir or self.audio_dir.parent / "opus")
        self.max_seconds = max(1, min(30, int(max_seconds)))
        self.ffmpeg = shutil.which("ffmpeg")

    @property
    def available(self):
        return bool(self.ffmpeg)

    def output_path(self, filename):
        return self.opus_dir / (Path(filename).stem + ".opus")

    def transcode(self, source, output):
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is not installed on the backend")
        source = Path(source)
        output = Path(output)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-t", str(self.max_seconds), "-vn",
            "-ac", "1", "-ar", "24000", "-c:a", "libopus", "-b:a", "64k",
            "-application", "audio", "-f", "ogg", str(temporary),
        ]
        try:
            subprocess.run(command, check=True, timeout=120,
                           capture_output=True, text=True)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return output

    async def ensure(self, source, output):
        output = Path(output)
        if output.is_file() and output.stat().st_size > 0:
            return output
        return await asyncio.to_thread(self.transcode, source, output)

    async def iter_raw_opus(self, source, frame_ms=60, start_seconds=0,
                            duration_seconds=None):
        """Yield raw Opus packets compatible with the existing TTS path.

        The file endpoint is useful for browser/debug playback and returns an
        Ogg Opus file.  This generator is for the future music WebSocket path:
        ffmpeg decodes the source to 24 kHz mono PCM, then the existing native
        Opus encoder creates one packet per device audio frame.
        """
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is not installed on the backend")
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        frame_bytes = 24000 * 2 * frame_ms // 1000
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
        ]
        if start_seconds:
            command.extend(["-ss", str(max(0, float(start_seconds)))])
        command.extend([
            "-i", str(source), "-t", str(max(1, min(
                self.max_seconds,
                float(duration_seconds) if duration_seconds is not None
                else self.max_seconds))), "-vn", "-ac", "1", "-ar", "24000",
            "-f", "s16le", "pipe:1"])
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        codec = OpusCodec(24000, bitrate=64000)
        try:
            while True:
                pcm = await process.stdout.readexactly(frame_bytes)
                yield codec.encode(pcm, frame_ms)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                padded = exc.partial + bytes(frame_bytes - len(exc.partial))
                yield codec.encode(padded, frame_ms)
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()
