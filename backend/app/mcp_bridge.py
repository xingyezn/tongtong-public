"""MCP (Model Context Protocol) 桥接。

后端作为 MCP 客户端，ESP32 设备作为 MCP 服务器。
流程（参考 mcp-protocol.md）：
  1. 设备 WS hello 后，后端主动发 initialize + tools/list 拿设备工具表
  2. Omni 要调工具 -> tools/call 下发设备
  3. 设备执行 -> result 回传 -> 回填 Omni
"""

import asyncio
import copy
import inspect
import json
import logging
from typing import Optional

log = logging.getLogger("mcp")

MODEL_MOTOR_ACTIONS = {
    "self.chassis.go_forward",
    "self.chassis.go_back",
    "self.chassis.turn_left",
    "self.chassis.turn_right",
}

# These switches control only which device functions are supplied to the LLM.
# The device still advertises every supported MCP tool, so the supervised
# manual-test panel remains available while a category is hidden.
MODEL_TOOL_CATEGORY_DEFAULTS = {
    "chassis": False,
    "camera": True,
    "gimbal_servo": False,
}


def model_tool_category(name):
    """Return the configurable LLM-visibility category for a device tool."""
    if not isinstance(name, str):
        return None
    lowered = name.lower()
    if lowered.startswith("self.chassis."):
        return "chassis"
    if lowered.startswith("self.camera."):
        return "camera"
    if (lowered.startswith(("self.gimbal.", "self.servo.")) or
            "servo" in lowered or "gimbal" in lowered or
            "pan_tilt" in lowered or "face_tracking" in lowered):
        return "gimbal_servo"
    if (lowered.startswith(("self.led.", "self.led_strip.")) or
            "rgb" in lowered):
        # RGB LED control is intentionally not exposed to the model.  The
        # board LED is reserved for firmware status indications.
        return "__removed__"
    return None


def normalize_model_tool_categories(value):
    """Merge stored settings with safe backward-compatible defaults."""
    normalized = dict(MODEL_TOOL_CATEGORY_DEFAULTS)
    if isinstance(value, dict):
        for key in normalized:
            if key in value:
                normalized[key] = bool(value[key])
    return normalized


def is_model_tool_visible(name, categories=None):
    category = model_tool_category(name)
    if category == "__removed__":
        return False
    if category is None:
        return True
    return normalize_model_tool_categories(categories).get(category, True)


class McpBridge:
    """管理单个设备会话的 MCP 交互。"""

    def __init__(self, send_json):
        # send_json: 回调函数，把 JSON 发到设备（WS 文本帧）
        self._send_json = send_json
        self._next_id = 1
        self.tools: list = []
        self._requested_tool_cursors = set()
        self._pending_tools_call: Optional[dict] = None
        self._pending_calls: dict = {}  # id -> asyncio.Future

    # ---- 工具 ----
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def make_initialize(self, capabilities: Optional[dict] = None) -> dict:
        """initialize 请求（发给设备）"""
        return {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {"capabilities": capabilities or {}},
                "id": self._new_id(),
            },
        }

    def make_tools_list(self, cursor: str = "") -> dict:
        """请求完整设备工具表，模型转换时仍会过滤用户专用工具。"""
        return {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {"cursor": cursor, "withUserTools": True},
                "id": self._new_id(),
            },
        }

    def make_tools_call(self, name: str, arguments: dict, call_id=None) -> dict:
        """tools/call 请求（发给设备），对应 Omni 的一次 function_call"""
        req = {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
                "id": self._new_id(),
            },
        }
        if call_id:
            req["call_id"] = call_id  # 自定义字段，关联 Omni tool_call id
        return req

    def make_omni_tools(self, motor_defaults=None, model_tool_categories=None) -> list:
        """Convert the device MCP tools/list result to Realtime function tools.

        The ESP32 is the source of truth for capabilities.  Keeping this
        conversion here prevents the model schema from drifting from the
        device's JSON Schema as boards add or remove tools.
        """
        result = []
        for tool in self.tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            if not is_model_tool_visible(name, model_tool_categories):
                continue
            annotations = tool.get("annotations") or {}
            if annotations.get("audience") == ["user"]:
                continue
            parameters = copy.deepcopy(tool.get("inputSchema") or {
                "type": "object",
                "properties": {},
            })
            if not isinstance(parameters, dict):
                continue
            if name in MODEL_MOTOR_ACTIONS:
                # 速度和持续时间由后端持久化配置注入，模型只需要选择动作。
                properties = parameters.get("properties")
                if isinstance(properties, dict):
                    properties.pop("speed", None)
                    properties.pop("duration_ms", None)
                required = parameters.get("required")
                if isinstance(required, list):
                    parameters["required"] = [
                        item for item in required
                        if item not in ("speed", "duration_ms")
                    ]
            if (motor_defaults and isinstance(name, str)
                    and name.startswith("self.chassis.")):
                properties = parameters.setdefault("properties", {})
                if isinstance(properties, dict):
                    speed = motor_defaults.get("speed")
                    duration = motor_defaults.get("duration_ms")
                    if "speed" in properties and isinstance(speed, int):
                        properties["speed"]["default"] = speed
                    if "duration_ms" in properties and isinstance(duration, int):
                        properties["duration_ms"]["default"] = duration
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or name,
                    "parameters": parameters,
                },
            })
        return result

    # ---- 处理设备返回的 MCP 消息 ----
    def on_device_mcp(self, payload: dict):
        """设备回传的 mcp payload（jsonrpc 2.0 result/error）"""
        # 找 pending future 并完成
        if "id" in payload:
            pid = payload.get("id")
            fut = self._pending_calls.pop(pid, None)
            if fut and not fut.done():
                if "result" in payload:
                    fut.set_result(payload["result"])
                elif "error" in payload:
                    fut.set_result(payload["error"])
                else:
                    fut.set_result({})
        # 如果是 list 结果，缓存 tools
        if "result" in payload and "tools" in payload["result"]:
            known = {tool.get("name") for tool in self.tools
                     if isinstance(tool, dict)}
            for tool in payload["result"]["tools"]:
                if not isinstance(tool, dict) or tool.get("name") in known:
                    continue
                self.tools.append(tool)
                known.add(tool.get("name"))
            log.info("MCP tools list updated: %d tools", len(self.tools))
            next_cursor = payload["result"].get("nextCursor")
            if isinstance(next_cursor, str) and next_cursor and next_cursor not in self._requested_tool_cursors:
                self._requested_tool_cursors.add(next_cursor)
                asyncio.create_task(self._request_tool_page(next_cursor))

    async def _request_tool_page(self, cursor: str):
        request = self.make_tools_list(cursor)
        sent = self._send_json(request)
        if inspect.isawaitable(sent):
            await sent

    # 供上层挂 future 的接口
    def register_pending(self, req_id, fut):
        log.info("register_pending id=%s (type=%s)", req_id, type(req_id).__name__)
        self._pending_calls[req_id] = fut

    async def call_tool(self, name: str, arguments: dict, timeout: float = 8.0):
        """Call a device MCP tool and wait for its JSON-RPC result.

        This is shared by the Omni bridge and the authenticated developer test
        endpoint.  The latter deliberately bypasses speech recognition and the
        model so a hardware test can be deterministic.
        """
        request = self.make_tools_call(name, arguments)
        request_id = request["payload"]["id"]
        future = asyncio.get_running_loop().create_future()
        self.register_pending(request_id, future)
        try:
            sent = self._send_json(request)
            if inspect.isawaitable(sent):
                await sent
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_calls.pop(request_id, None)
