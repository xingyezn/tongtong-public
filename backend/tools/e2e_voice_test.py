"""Run one authenticated virtual-microphone E2E voice test.

The WAV is converted to 16 kHz / mono / signed-16 PCM, injected at the
ESP32 virtual microphone boundary, and then follows the normal device audio
uplink, VAD, Omni model, MCP and callback path.
"""
import argparse
import audioop
import base64
import getpass
import http.cookiejar
import json
import urllib.parse
import urllib.request
import wave


def load_pcm(path):
    with wave.open(path, "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        sample_rate = source.getframerate()
        raw = source.readframes(source.getnframes())
    if channels not in (1, 2):
        raise ValueError("WAV must be mono or stereo")
    if width not in (1, 2, 3, 4):
        raise ValueError("unsupported WAV sample width")
    if channels == 2:
        raw = audioop.tomono(raw, width, 0.5, 0.5)
    if width != 2:
        raw = audioop.lin2lin(raw, width, 2)
    if sample_rate != 16000:
        raw, _ = audioop.ratecv(raw, 2, 1, sample_rate, 16000, None)
    if not raw:
        raise ValueError("WAV contains no audio")
    return raw


def request_json(opener, url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Run a Tongtong end-to-end voice/MCP test")
    parser.add_argument("--base-url", required=True, help="Dashboard base URL")
    parser.add_argument("--device-id", required=True, help="Online device ID from Dashboard")
    parser.add_argument("--wav", required=True, help="Speech WAV file")
    parser.add_argument("--expected-tool", required=True, help="Expected model-selected MCP tool")
    parser.add_argument("--allow-motion", action="store_true", help="Allow chassis tools (wheels must be raised)")
    parser.add_argument("--password", help="Dashboard password; omit for hidden input")
    args = parser.parse_args()

    pcm = load_pcm(args.wav)
    password = args.password or getpass.getpass("Dashboard password: ")
    base_url = args.base_url.rstrip("/")
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    login = urllib.request.Request(
        base_url + "/login",
        data=urllib.parse.urlencode({"password": password}).encode("utf-8"),
        method="POST",
    )
    opener.open(login, timeout=15)
    result = request_json(opener, base_url + "/api/test/conversation", {
        "device_id": args.device_id,
        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
        "expected_tool": args.expected_tool,
        "allow_motion": args.allow_motion,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("matched") or result.get("model_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
