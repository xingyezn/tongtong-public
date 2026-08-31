"""MCP (Model Context Protocol) 桥接。

后端作为 MCP 客户端，ESP32 设备作为 MCP 服务器。
流程（参考 mcp-protocol.md）：
  1. 设备 WS hello 后，后端主动发 initialize + tools/list 拿设备工具表
  2. Omni 要调工具 -> tools/call 下发设备
  3. 设备执行 -> result 回传 -> 回填 Omni
"""

import json
import logging
from typing import Optional

log = logging.getLogger("mcp")


class McpBridge:
    """管理单个设备会话的 MCP 交互。"""

    def __init__(self, send_json):
        # send_json: 回调函数，把 JSON 发到设备（WS 文本帧）
        self._send_json = send_json
        self._next_id = 1
        self.tools: list = []
        self._pending_tools_call: Optional[dict] = None
        self._pending_calls: dict = {}  # id -> asyncio.Future

    # ---- 工具 ----
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def make_initialize(self) -> dict:
        """initialize 请求（发给设备）"""
        return {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {"capabilities": {}},
                "id": self._new_id(),
            },
        }

    def make_tools_list(self, cursor: str = "") -> dict:
        """tools/list 请求（发给设备）"""
        return {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {"cursor": cursor},
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

    def make_omni_tools(self) -> list:
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
            annotations = tool.get("annotations") or {}
            if annotations.get("audience") == ["user"]:
                continue
            parameters = tool.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            if not isinstance(parameters, dict):
                continue
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
            self.tools = payload["result"]["tools"]
            log.info("MCP tools list updated: %d tools", len(self.tools))

    # 供上层挂 future 的接口
    def register_pending(self, req_id, fut):
        log.info("register_pending id=%s (type=%s)", req_id, type(req_id).__name__)
        self._pending_calls[req_id] = fut
