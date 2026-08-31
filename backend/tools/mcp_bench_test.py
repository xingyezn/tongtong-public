"""Authenticated, deterministic MCP bench-test client.

This talks to the dashboard test API, not to the speech model.  The target
device must already be online (for example, hold its touch button to enter
listening mode) and its wheels must be off the ground for motor tests.
"""
import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def request_json(opener, url, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with opener.open(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Run a supervised Tongtong MCP bench test")
    parser.add_argument("--base-url", required=True, help="Dashboard base URL, e.g. https://server.example")
    parser.add_argument("--device-id", required=True, help="Online device ID shown in the dashboard")
    parser.add_argument("--tool", help="MCP tool name, e.g. self.chassis.go_forward")
    parser.add_argument("--arguments", default="{}", help="JSON object passed to the MCP tool")
    parser.add_argument("--timeout-ms", type=int, default=8000)
    parser.add_argument("--password", help="Dashboard password; omit to enter it without echo")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    password = args.password or getpass.getpass("Dashboard password: ")
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    login_data = urllib.parse.urlencode({"password": password}).encode("utf-8")
    try:
        opener.open(urllib.request.Request(base_url + "/login", data=login_data), timeout=20).read()
        if not args.tool:
            print(json.dumps(request_json(
                opener, base_url + "/api/test/tools?" + urllib.parse.urlencode({"device_id": args.device_id})),
                ensure_ascii=False, indent=2))
            return
        tool_args = json.loads(args.arguments)
        if not isinstance(tool_args, dict):
            raise ValueError("--arguments must be a JSON object")
        result = request_json(opener, base_url + "/api/test/mcp", {
            "device_id": args.device_id,
            "name": args.tool,
            "arguments": tool_args,
            "timeout_ms": args.timeout_ms,
        })
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        print("bench test failed: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
