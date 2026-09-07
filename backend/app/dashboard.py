"""监控面板：设备状态 / 配置查看 / 实时日志 / 硬件测试。

路由：
  GET  /            单页监控面板（内嵌 HTML）
  GET  /api/status  设备在线状态 + 后端配置摘要
  GET  /api/logs    实时日志流（SSE）
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

log = logging.getLogger("dash")

# 登录 cookie 名
AUTH_COOKIE = "tongtong_auth"
MAX_CAMERA_PHOTO_BYTES = 2 * 1024 * 1024
MODEL_LANGUAGE_CODES = {
    "auto", "zh", "en", "fr", "de", "ru", "it", "es", "pt", "ja", "ko",
    "th", "id", "ar", "vi", "tr", "fi", "pl", "hi", "nl", "cs", "ur",
    "fil", "sv", "da", "he", "is", "ms", "no", "fa",
}
BINDING_ATTEMPT_WINDOW_SECONDS = 10 * 60
BINDING_ATTEMPT_LIMIT = 5


# ---------------------------------------------------------------------------
# 广播日志 Handler：把 root logger 的日志实时推给 SSE 订阅者
# ---------------------------------------------------------------------------
class BroadcastLogHandler(logging.Handler):
    def __init__(self, maxlen=500):
        super().__init__()
        # Keep dashboard polling and ordinary HTTP access separate.  Otherwise
        # a browser refreshing /api/status every few seconds can evict the
        # device/MCP diagnostics that are needed for troubleshooting.
        self.buf = deque(maxlen=maxlen)
        self.access_buf = deque(maxlen=max(50, maxlen // 4))
        self.periodic_buf = deque(maxlen=max(30, maxlen // 8))
        self._sequence = 0
        self._loop = None
        self._evt = None
        self._lock = threading.Lock()

    def attach(self, loop):
        self._loop = loop
        self._evt = asyncio.Event()

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        category = self._classify(record)
        with self._lock:
            self._sequence += 1
            entry = {"id": self._sequence, "category": category, "text": msg}
            if category == "periodic":
                self.periodic_buf.append(entry)
            elif category == "access":
                self.access_buf.append(entry)
            else:
                self.buf.append(entry)
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._wake)

    @staticmethod
    def _classify(record):
        """Return a stable, intentionally small category for dashboard logs."""
        name = record.name.lower()
        message = record.getMessage()
        if name == "aiohttp.access":
            # Dashboard auto-refresh is expected noise, not a diagnostic event.
            if " /api/status " in message:
                return "periodic"
            return "access"
        if name in ("ws", "session"):
            return "device"
        if name == "mcp" or name.startswith("mcp."):
            return "mcp"
        if "omni" in name or "dashscope" in name:
            return "model"
        return "system"

    def _wake(self):
        if self._evt is not None:
            self._evt.set()

    def snapshot(self):
        with self._lock:
            return sorted(
                list(self.buf) + list(self.access_buf) + list(self.periodic_buf),
                key=lambda entry: entry["id"],
            )

    def events_after(self, sequence):
        with self._lock:
            entries = list(self.buf) + list(self.access_buf) + list(self.periodic_buf)
        return sorted(
            (entry for entry in entries if entry["id"] > sequence),
            key=lambda entry: entry["id"],
        )


# ---------------------------------------------------------------------------
# Dashboard 页面
# ---------------------------------------------------------------------------
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · Tongtong Monitor</title>
<style>
  :root { --ink:#18334b; --muted:#6d8395; --blue:#2d8cf0; --blue-deep:#1773d6;
          --line:#dceaf2; --card:rgba(255,255,255,.9); }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         color:var(--ink); font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;
         background:radial-gradient(circle at 14% 16%, #d6f7ef 0, transparent 28rem),
                    radial-gradient(circle at 88% 84%, #dceeff 0, transparent 32rem), #f6fbfd; }
  .box { width:min(360px, calc(100vw - 36px)); padding:34px 36px 30px; border:1px solid rgba(255,255,255,.9);
         border-radius:22px; background:var(--card); box-shadow:0 20px 55px rgba(47,105,137,.16);
         backdrop-filter:blur(10px); }
  .eyebrow { margin:0 0 6px; color:#47a89a; font-size:12px; font-weight:700; letter-spacing:.12em; text-align:center; }
  h1 { margin:0 0 7px; color:#17364d; font-size:24px; letter-spacing:-.02em; text-align:center; }
  .sub { margin:0 0 23px; color:var(--muted); font-size:13px; text-align:center; }
  input { width:100%; padding:11px 13px; margin-bottom:14px; border:1px solid var(--line); border-radius:10px;
          background:#fbfeff; color:var(--ink); font-size:14px; transition:border-color .2s, box-shadow .2s; }
  input:focus { outline:none; border-color:#76b8f7; box-shadow:0 0 0 4px rgba(45,140,240,.12); }
  button { width:100%; padding:11px; border:0; border-radius:10px; color:#fff; font-size:14px; font-weight:700;
           cursor:pointer; background:linear-gradient(135deg, #39b8a6, #2d8cf0); box-shadow:0 8px 16px rgba(45,140,240,.22);
           transition:transform .18s, box-shadow .18s; }
  button:hover { transform:translateY(-1px); box-shadow:0 11px 20px rgba(45,140,240,.28); }
  .error { margin-bottom:12px; color:#d9435b; font-size:13px; text-align:center; }
  .env-warning { margin:0 auto 16px; padding:12px 14px; border:2px solid #c93636; border-radius:10px;
                 color:#8b1717; background:#fff0f0; font-size:13px; font-weight:800; line-height:1.45;
                 text-align:center; box-shadow:0 4px 12px rgba(201,54,54,.14); position:fixed; top:16px;
                 left:50%; z-index:20; width:min(420px, calc(100vw - 32px)); transform:translateX(-50%); }
  .switch { margin:16px 0 0; color:var(--muted); font-size:13px; text-align:center; }
  .switch a { color:var(--blue-deep); text-decoration:none; font-weight:700; }
</style>
</head>
<body>
<!--ENV_WARNING-->
<div class="box">
  <p class="eyebrow">TONGTONG · CONTROL CENTER</p>
  <h1>童童监控中心</h1>
  <p class="sub">欢迎回来，连接你的语音助手</p>
  <!--ERROR-->
  <form method="post" action="/login">
    <input type="text" name="username" placeholder="用户名" autocomplete="username" autofocus required>
    <input type="password" name="password" placeholder="密码" autocomplete="current-password" required>
    <button type="submit">登 录</button>
  </form>
  <p class="switch">还没有账号？<a href="/register">立即注册</a></p>
</div>
</body>
</html>
"""

REGISTER_HTML = (LOGIN_HTML
    .replace("登录 · Tongtong Monitor", "注册 · Tongtong Monitor")
    .replace("欢迎回来，连接你的语音助手", "创建账号，管理属于你的设备")
    .replace('action="/login"', 'action="/register"')
    .replace('autocomplete="current-password"', 'autocomplete="new-password"')
    .replace("登 录", "注 册")
    .replace('还没有账号？<a href="/register">立即注册</a>',
             '已有账号？<a href="/login">返回登录</a>'))


# ---------------------------------------------------------------------------
# Dashboard 页面
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tongtong Backend Monitor</title>
<style>
  :root { --bg:#f4fafc; --card:rgba(255,255,255,.92); --line:#dceaf2; --fg:#18334b;
          --muted:#708596; --ok:#27a97e; --warn:#dc9a2e; --bad:#dc5b6c; --acc:#2d8cf0;
          --mint:#39b8a6; --soft-blue:#eaf5ff; --soft-mint:#e6f8f2; }
  * { box-sizing:border-box; }
  body { min-height:100vh; margin:0; color:var(--fg); font:14px/1.55 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
         background:radial-gradient(circle at 5% 0, #daf7ef 0, transparent 24rem),
                    radial-gradient(circle at 96% 8%, #ddecff 0, transparent 30rem), var(--bg); }
  header { min-height:82px; padding:14px clamp(18px, 4vw, 48px); display:flex; align-items:center; gap:14px; flex-wrap:wrap;
           border-bottom:1px solid rgba(220,234,242,.9); background:rgba(255,255,255,.76); backdrop-filter:blur(12px);
           position:sticky; top:0; z-index:10; }
  .brand { display:flex; align-items:center; gap:11px; }
  .brand-mark { display:grid; place-items:center; width:38px; height:38px; border-radius:13px; color:#fff; font-size:12px; font-weight:800;
                letter-spacing:.04em; background:linear-gradient(135deg, var(--mint), var(--acc)); box-shadow:0 7px 14px rgba(45,140,240,.22); }
  .brand-kicker { display:block; margin-bottom:1px; color:#51a99c; font-size:10px; font-weight:800; letter-spacing:.12em; }
  header h1 { margin:0; font-size:18px; letter-spacing:-.01em; }
  .header-spacer { flex:1; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  .dot.ok { background:var(--ok); box-shadow:0 0 0 4px rgba(39,169,126,.12); }
  .dot.bad { background:var(--bad); box-shadow:0 0 0 4px rgba(220,91,108,.12); }
  main { max-width:1500px; margin:0 auto; padding:26px clamp(18px, 4vw, 48px) 44px; display:grid; gap:18px; grid-template-columns:repeat(12, 1fr); }
  .card { grid-column:span 6; padding:19px 20px; border:1px solid rgba(220,234,242,.95); border-radius:18px; background:var(--card);
          box-shadow:0 9px 24px rgba(46,103,137,.07); transition:transform .2s, box-shadow .2s; }
  .card:hover { transform:translateY(-2px); box-shadow:0 13px 29px rgba(46,103,137,.11); }
  .card.full { grid-column:span 12; }
  .card h2 { margin:0 0 14px; color:#3c596d; font-size:14px; font-weight:800; letter-spacing:.025em; }
  table { width:100%; border-collapse:collapse; }
  th,td { padding:9px 10px; border-bottom:1px solid #edf4f7; font-size:13px; text-align:left; }
  tr:last-child td { border-bottom:0; } th { color:var(--muted); font-weight:700; }
  td.mono { font-family:Consolas, monospace; font-size:12px; }
  .tag { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:700; }
  .tag.online { background:var(--soft-mint); color:#178164; } .tag.offline { background:#fff0f2; color:#c04d61; }
  .tag.listen { background:var(--soft-blue); color:#2879c9; }
  .tag.debug { background:#fff5dc; color:#a36600; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:13px; }
  .kv dt { color:var(--muted); } .kv dd { margin:0; word-break:break-all; }
  #logs { height:320px; overflow:auto; padding:11px 13px; border:1px solid #dceaf2; border-radius:12px; background:#f7fbfd;
          color:#3b5265; font-family:Consolas, monospace;
          font-size:12px; line-height:1.5; white-space:pre-wrap; }
  .log-info { color:#3475a7; } .log-warning { color:#ae771b; }
  .log-error, .log-critical { color:var(--bad); }
  .log-debug { color:#91a2ae; }
  .log-category { display:inline-block; min-width:42px; margin-right:7px; padding:1px 5px; border-radius:5px;
                  color:#6a8494; background:#eaf2f6; font-family:inherit; font-size:10px; font-weight:700; text-align:center; }
  .log-filters { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .log-filters label { color:var(--muted); font-size:12px; white-space:nowrap; }
  .btn { padding:8px 15px; border:0; border-radius:9px; color:#fff; cursor:pointer; font-size:13px; font-weight:700;
         background:linear-gradient(135deg, var(--mint), var(--acc)); box-shadow:0 5px 12px rgba(45,140,240,.18); transition:transform .18s, box-shadow .18s; }
  .btn:hover { transform:translateY(-1px); box-shadow:0 8px 16px rgba(45,140,240,.24); }
  .btn:disabled { opacity:.45; cursor:not-allowed; }
  .btn.warn { background:linear-gradient(135deg, #f6bd58, #df9630); color:#fff; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .test-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-top:12px; }
  .test-group { padding:12px; border:1px solid #dceaf2; border-radius:12px; background:#f8fcfe; }
  .test-group h3 { margin:0 0 9px; font-size:14px; color:var(--fg); }
  .test-actions { display:flex; flex-wrap:wrap; gap:7px; }
  .test-actions .btn { padding:7px 10px; font-size:12px; }
  .test-input { width:72px; padding:6px 7px !important; }
  .rgb-picker { width:42px !important; height:32px; margin:0; padding:2px !important; cursor:pointer; vertical-align:middle; }
  .rgb-swatch { display:inline-block; width:28px; height:28px; margin-left:4px; border:1px solid #bdd4e0; border-radius:7px; vertical-align:middle; background:#00a0ff; }
  .rgb-hex-input { width:86px !important; margin:0; padding:6px 7px !important; font-family:Consolas,monospace; text-transform:uppercase; }
  #hardware-test-result { min-height:42px; overflow:visible; margin-top:12px; padding:9px 11px; border:1px solid #dceaf2; border-radius:9px; background:#f7fbfd; color:#3b5265; font:12px/1.5 Consolas,monospace; white-space:pre-wrap; }
  #camera-preview { display:none; margin-top:12px; padding:10px; border:1px solid #dceaf2; border-radius:9px; background:#f8fcfe; }
  #camera-preview img { display:block; width:min(100%, 640px); height:auto; object-fit:contain; border-radius:6px; background:#edf4f8; }
  .face-result { position:relative; width:min(100%, 640px); margin-top:8px; background:#edf4f8; }
  .face-result img { display:block; width:100%; height:auto; }
  .face-result svg { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }
  .face-box { fill:rgba(40,210,130,.12); stroke:#19b879; stroke-width:2; }
  .face-label { fill:#19b879; font:700 13px "Segoe UI","Microsoft YaHei",sans-serif; }
  .result-json { margin:10px 0 0; padding:9px; border:1px solid #dceaf2; border-radius:8px; background:#fff; overflow:visible; white-space:pre-wrap; word-break:break-word; }
  .muted { color:var(--muted); font-size:12px; }
  .empty { color:var(--muted); font-size:13px; padding:8px 0; }
  .hint { font-size:12px; color:var(--muted); margin-top:8px; }
  .badge { padding:3px 9px; border-radius:20px; color:#527087; font-size:11px; background:#e8f3f8; }
  .env-warning { margin:0 clamp(18px, 4vw, 48px); padding:13px 18px; border:2px solid #c93636; border-radius:12px;
                 color:#8b1717; background:#fff0f0; font-size:14px; font-weight:800; line-height:1.45;
                 text-align:center; box-shadow:0 5px 14px rgba(201,54,54,.14); position:sticky; top:82px; z-index:9; }
  .chat-layout { display:grid; grid-template-columns:minmax(180px,26%) 1fr; gap:12px; min-height:360px; }
  .conversation-list { max-height:520px; overflow:auto; display:flex; flex-direction:column; gap:7px; }
  .conversation-item { border:1px solid #dceaf2; border-radius:10px; padding:9px 11px; background:#f8fcfe; cursor:pointer; }
  .conversation-item.active { border-color:var(--acc); background:#edf7ff; }
  .conversation-delete { float:right; padding:4px 8px; margin-left:8px; font-size:11px; }
  .chat-messages { max-height:520px; overflow:auto; padding:12px; border:1px solid #dceaf2; border-radius:12px; background:#f8fbfd; display:flex; flex-direction:column; gap:10px; }
  .chat-message { max-width:78%; padding:9px 12px; border-radius:14px; white-space:pre-wrap; overflow-wrap:anywhere; }
  .chat-message.user { align-self:flex-end; background:#dff1ff; border-bottom-right-radius:4px; }
  .chat-message.assistant { align-self:flex-start; background:#fff; border:1px solid #dceaf2; border-bottom-left-radius:4px; }
  .chat-message .speaker { font-size:10px; color:var(--muted); font-weight:800; margin-bottom:3px; }
  .memory-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:9px; margin-top:10px; }
  .memory-item { border:1px solid #dceaf2; border-radius:10px; padding:10px; background:#f8fcfe; }
  .usage-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:9px; }
  .usage-item { padding:11px; border:1px solid #dceaf2; border-radius:10px; background:#f8fcfe; }
  .usage-item b { display:block; font-size:19px; color:#276c9f; }
  .modal-backdrop { position:fixed; inset:0; z-index:1000; display:none; align-items:center; justify-content:center; padding:18px; background:rgba(19,47,67,.42); }
  .modal-backdrop.show { display:flex; }
  .modal { width:min(520px,100%); padding:22px; border:1px solid #dceaf2; border-radius:16px; background:#fff; box-shadow:0 22px 55px rgba(20,55,79,.25); }
  .modal h3 { margin:0 0 15px; font-size:17px; } .modal label { display:block; margin:10px 0 5px; color:var(--muted); font-size:12px; font-weight:700; }
  .modal input,.modal textarea { width:100%; margin:0; } .modal textarea { min-height:110px; resize:vertical; padding:9px; }
  input:not([type="checkbox"]), select, textarea { border:1px solid #d5e6ef !important; border-radius:9px !important; background:#fbfeff !important;
          color:var(--fg) !important; box-shadow:none; transition:border-color .18s, box-shadow .18s; }
  input:not([type="checkbox"]):focus, select:focus, textarea:focus { outline:none; border-color:#72b7f4 !important; box-shadow:0 0 0 3px rgba(45,140,240,.11) !important; }
  #toast { position:fixed; bottom:28px; left:50%; transform:translateX(-50%);
           display:flex; align-items:center; gap:10px; min-width:250px; max-width:min(420px, 90vw); padding:13px 17px;
           border:1px solid var(--line); border-radius:14px; color:var(--fg); background:#fff; font-size:14px; font-weight:700;
           z-index:999; box-shadow:0 14px 36px rgba(32,83,112,.22); opacity:0; pointer-events:none;
           transition:opacity .25s, transform .25s; text-align:left; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(-8px); }
  #toast.ok { border-color:#95dfcb; color:#167e63; background:#f1fcf8; }
  #toast.err { border-color:#f0a8b2; color:#c4475a; background:#fff6f7; }
  .toast-icon { display:grid; place-items:center; flex:0 0 26px; width:26px; height:26px; border-radius:50%; color:#fff; font-size:15px; }
  #toast.ok .toast-icon { background:var(--ok); } #toast.err .toast-icon { background:var(--bad); }
  @media (max-width:760px) { header { align-items:flex-start; } .header-spacer { display:none; } main { grid-template-columns:1fr; padding-top:18px; } .chat-layout { grid-template-columns:1fr; }
    .card, .card.full { grid-column:1; } .card:hover { transform:none; } #card-devices { overflow-x:auto; } }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-mark">TT</span>
    <div><span class="brand-kicker">TONGTONG · CONTROL CENTER</span><h1>童童监控中心</h1></div>
  </div>
  <span id="health"><span class="dot bad"></span>检查中…</span>
  <span class="badge" id="uptime">—</span>
  <span class="header-spacer"></span>
  <span class="badge" id="current-user">—</span>
  <a class="btn" id="admin-link" href="/admin" style="display:none">管理员端</a>
  <label class="muted"><input type="checkbox" id="autorefresh" checked> 自动刷新</label>
  <button class="btn" onclick="refresh()">刷新</button>
  <a class="btn" href="/logout">退出</a>
</header>
<!--ENV_WARNING-->

<main>
  <div class="card full">
    <h2>设备绑定与管理</h2>
    <div class="row" style="margin-bottom:10px">
      <label class="muted">当前设备</label>
      <select id="active-device" onchange="onDeviceChanged()" style="min-width:220px"></select>
      <input id="device-edit-identifier" maxlength="40" placeholder="设备识别码，如 living-room" style="padding:7px 9px">
      <input id="device-edit-name" maxlength="64" placeholder="自定义名称，如 客厅童童" style="padding:7px 9px">
      <button class="btn" onclick="updateDevice()">保存名称</button>
      <button class="btn warn" onclick="unbindDevice()">解绑</button>
    </div>
    <div class="row">
      <input id="bind-code" maxlength="8" inputmode="numeric" autocomplete="one-time-code"
             placeholder="输入设备屏幕上的 8 位绑定码" style="flex:1;max-width:300px;padding:7px 9px">
      <button class="btn" onclick="bindDevice()">绑定设备</button>
    </div>
    <div class="hint">无需输入设备 ID。绑定码 10 分钟内有效且只能使用一次；连续输错会触发安全限速。绑定成功后可在上方修改自动生成的设备名称和识别码。</div>
  </div>

  <div class="card full" id="card-devices">
    <h2>设备状态</h2>
    <table>
      <thead><tr><th>名称</th><th>识别码</th><th>硬件 ID（诊断）</th><th>版本</th><th>状态</th><th>在线/最后活跃</th><th>Session</th></tr></thead>
      <tbody id="device-body"><tr><td colspan="7" class="empty">加载中…</td></tr></tbody>
    </table>
    <div class="hint" id="ota-hint"></div>
    <div class="hint">说明：设备待机时按省电设计断开连接（显示"待机中"），唤醒对话时自动上线。</div>
  </div>

  <div class="card">
    <h2>后端配置</h2>
    <dl class="kv" id="cfg-kv"></dl>
  </div>

  <div class="card">
    <h2>我的用量</h2>
    <div class="usage-grid" id="usage-summary"><div class="empty">加载中…</div></div>
    <div class="hint" id="usage-device-hint"></div>
  </div>

  <div class="card full">
    <h2>硬件手动测试</h2>
    <div class="row">
      <select id="hardware-device" onchange="updateHardwareTestPanel()" aria-label="选择设备"></select>
      <button class="btn" onclick="loadHardwareTests(true)">刷新测试项</button>
    </div>
    <div class="hint" id="hardware-test-status">等待设备上线…</div>
    <div id="hardware-test-groups" class="test-grid"></div>
    <div id="hardware-test-result">尚未执行测试。</div>
    <div id="camera-preview"><div class="muted" style="margin-bottom:7px">最近拍摄的照片（仅保存在后端内存，重启后自动清除）</div><img id="camera-preview-image" alt="设备最近拍摄的照片"></div>
    <div class="hint">所有按钮通过设备 MCP 通道执行；运动测试必须先让车轮悬空。设备未声明的摄像头、舵机或屏幕工具会显示为不可用，不会伪造测试结果。</div>
  </div>

  <div class="card">
    <h2>当前设备 · 语音检测 (VAD)</h2>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">静音结束时长(ms)</label>
      <input type="number" id="vad-silence" min="200" max="6000" step="100" value="400" style="flex:1;max-width:160px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">语音能量阈值</label>
      <input type="number" id="vad-threshold" min="1" max="30000" step="10" value="100" style="flex:1;max-width:160px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row">
      <button class="btn" onclick="saveVad()">保存 VAD 配置</button>
      <span class="muted" id="vad-status"></span>
    </div>
    <div class="hint">
      <b>静音结束时长</b>：说话停止后等这么久才触发对话（越小反应越快，可能误触发）。<br>
      <b>能量阈值</b>：高于此算"说话"，低于算"静音"（环境吵则调高）。<br>
      修改即时生效，无需重启。
    </div>
  </div>

  <div class="card">
    <h2>当前设备 · 模型 / 语言 / 音色 / 人物设定</h2>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">模型</label>
      <input type="text" id="cfg-model" placeholder="qwen3.5-omni-flash-realtime" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">对话语言</label>
      <select id="cfg-language" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
        <option value="auto">自动检测（Auto Detect）</option>
        <option value="zh">中文（普通话）</option>
        <option value="en">英语（English）</option>
        <option value="fr">法语（Français）</option>
        <option value="de">德语（Deutsch）</option>
        <option value="ru">俄语（Русский）</option>
        <option value="it">意大利语（Italiano）</option>
        <option value="es">西班牙语（Español）</option>
        <option value="pt">葡萄牙语（Português）</option>
        <option value="ja">日语（日本語）</option>
        <option value="ko">韩语（한국어）</option>
        <option value="th">泰语（ไทย）</option>
        <option value="id">印度尼西亚语（Bahasa Indonesia）</option>
        <option value="ar">阿拉伯语（العربية）</option>
        <option value="vi">越南语（Tiếng Việt）</option>
        <option value="tr">土耳其语（Türkçe）</option>
        <option value="fi">芬兰语（Suomi）</option>
        <option value="pl">波兰语（Polski）</option>
        <option value="hi">印地语（हिन्दी）</option>
        <option value="nl">荷兰语（Nederlands）</option>
        <option value="cs">捷克语（Čeština）</option>
        <option value="ur">乌尔都语（اردو）</option>
        <option value="fil">他加禄语（Tagalog）</option>
        <option value="sv">瑞典语（Svenska）</option>
        <option value="da">丹麦语（Dansk）</option>
        <option value="he">希伯来语（עברית）</option>
        <option value="is">冰岛语（Íslenska）</option>
        <option value="ms">马来语（Bahasa Melayu）</option>
        <option value="no">挪威语（Norsk）</option>
        <option value="fa">波斯语（فارسی）</option>
      </select>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">音色</label>
      <select id="cfg-voice" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
        <option value="Tina">Tina · 甜甜 (女·普通话)</option>
        <option value="Cindy">Cindy · 林欣宜 (女·台湾)</option>
        <option value="Liora Mira">Liora Mira · 清欢 (女·普通话)</option>
        <option value="Sunnybobi">Sunnybobi · 知芝 (女·普通话)</option>
        <option value="Raymond">Raymond · 林川野 (男·普通话)</option>
        <option value="Ethan">Ethan · 晨煦 (男·普通话)</option>
        <option value="Theo Calm">Theo Calm · 予安 (男·普通话)</option>
        <option value="Serena">Serena · 苏瑶 (女·普通话)</option>
        <option value="Harvey">Harvey · 厚 (男·普通话)</option>
        <option value="Maia">Maia · 四月 (女·普通话)</option>
        <option value="Evan">Evan · 江晨 (男·普通话)</option>
        <option value="Qiao">Qiao · 小乔妹 (女·台湾)</option>
        <option value="Momo">Momo · 茉兔 (女·普通话)</option>
        <option value="Wil">Wil · 伟伦 (男·普通话)</option>
        <option value="Angel">Angel · 安琪 (女·普通话)</option>
        <option value="Li Cassian">Li Cassian · 李公公 (男·普通话)</option>
        <option value="Mia">Mia · 舒然 (女·普通话)</option>
        <option value="Joyner">Joyner · 阿逗 (男·普通话)</option>
        <option value="Gold">Gold · 金爷 (男·普通话)</option>
        <option value="Katerina">Katerina · 卡捷琳娜 (女·普通话)</option>
        <option value="Ryan">Ryan · 甜茶 (男·普通话)</option>
        <option value="Jennifer">Jennifer · 詹妮弗 (女·普通话)</option>
        <option value="Aiden">Aiden · 艾登 (男·普通话)</option>
        <option value="Mione">Mione · 敏儿 (女·普通话)</option>
        <option value="Sunny">Sunny · 晴儿 (女·四川)</option>
        <option value="Dylan">Dylan · 晓东 (男·北京)</option>
        <option value="Eric">Eric · 程川 (男·四川)</option>
        <option value="Peter">Peter · 李彼得 (男·天津)</option>
        <option value="Joseph Chen">Joseph Chen · 阿樸伯 (男·闽南)</option>
        <option value="Marcus">Marcus · 秦川 (男·陕西)</option>
        <option value="Li">Li · 老李 (男·南京)</option>
        <option value="Kiki">Kiki · 阿清 (女·粤语)</option>
        <option value="Rocky">Rocky · 阿强 (男·粤语)</option>
        <option value="Sohee">Sohee · 素熙 (女·韩)</option>
        <option value="Lenn">Lenn · 莱恩 (男·德)</option>
        <option value="Ono Anna">Ono Anna · 小野杏 (女·日)</option>
        <option value="Sonrisa">Sonrisa · 索尼莎 (女·西)</option>
        <option value="Bodega">Bodega · 博德加 (男·西)</option>
        <option value="Emilien">Emilien · 埃米尔安 (男·法)</option>
        <option value="Andre">Andre · 安德雷 (男·普通话)</option>
        <option value="Radio Gol">Radio Gol · 拉迪奥·戈尔 (男·葡)</option>
        <option value="Alek">Alek · 阿列克 (男·俄)</option>
        <option value="Rizky">Rizky · 阿力 (男·印尼)</option>
        <option value="Roya">Roya · 萝雅 (女·波斯)</option>
        <option value="Arda">Arda · 阿尔达 (男·土耳其)</option>
        <option value="Hana">Hana · 阿幸 (女·越南)</option>
        <option value="Dolce">Dolce · 多尔切 (男·意)</option>
        <option value="Jakub">Jakub · 雅克 (男·波兰)</option>
        <option value="Griet">Griet · 海娜 (女·荷兰)</option>
        <option value="Eliška">Eliška · 艾莉卡 (女·捷克)</option>
        <option value="Marina">Marina · 玛丽娜 (女·多语)</option>
        <option value="Siiri">Siiri · 西芮 (女·芬兰)</option>
        <option value="Ingrid">Ingrid · 林恩 (女·挪威)</option>
        <option value="Sigga">Sigga · 海娜 (女·冰岛)</option>
        <option value="Bea">Bea · 雅娜 (女·菲律宾)</option>
        <option value="Chloe">Chloe · 思怡 (女·马来)</option>
      </select>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px;align-self:flex-start">人物设定</label>
      <textarea id="cfg-instructions" rows="4" placeholder="你是童童，一个友好、热情的语音助手……" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4;resize:vertical;font-family:Consolas,monospace;font-size:12px"></textarea>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">模型连接复用（分钟）</label>
      <input type="number" id="cfg-conversation-timeout" min="1" max="120" step="1" value="10" style="flex:1;max-width:140px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
      <span class="muted">超时后重连模型，但保留本次会话上下文</span>
    </div>
    <div class="row">
      <button class="btn" id="save-model-btn" onclick="saveModel()">保存模型 / 语言 / 音色 / 设定</button>
      <span class="muted" id="model-status"></span>
    </div>
    <div class="hint">
      修改后<b>下一轮对话生效</b>，并持久化保存（重启仍生效）。语言会同时约束语音识别和模型回复。
      一次会话严格从离开待命开始，到再次进入待命结束；模型连接复用时长可设置为 1～120 分钟。
      模型需为百炼 Realtime 系列（如 qwen3.5-omni-flash-realtime / qwen3.5-omni-plus-realtime）。
    </div>
  </div>

  <div class="card">
    <h2>当前设备 · 记忆与打断</h2>
    <label class="row"><input type="checkbox" id="feature-memory"> 允许此设备使用个人长期记忆</label>
    <label class="row"><input type="checkbox" id="feature-auto-interrupt"> 自动打断（当前硬件使用唤醒词检测）</label>
    <label class="row"><input type="checkbox" id="feature-button-interrupt"> 单击复位待命键：打断并继续聆听</label>
    <label class="row"><input type="checkbox" id="feature-double-end"> 双击复位待命键：结束本次会话</label>
    <div class="row" style="margin-top:10px"><button class="btn" onclick="saveFeatures()">保存功能设置</button><span class="muted" id="feature-status"></span></div>
    <div class="hint">无回声消除的设备无法可靠地在扬声器播放时检测任意语音，因此自动模式使用唤醒词打断；按键打断会立即停止音频播放，但仍会保留模型生成的完整文本。</div>
  </div>

  <div class="card">
    <h2>我的个人信息（永久记忆）</h2>
    <div class="row"><input id="memory-category" placeholder="分类，如 偏好" style="padding:7px;width:100px"><input id="memory-label" placeholder="字段，如 喜欢的颜色" style="padding:7px;flex:1"><input id="memory-value" placeholder="内容" style="padding:7px;flex:2"><button class="btn" onclick="addMemory()">添加</button></div>
    <div id="memory-list" class="memory-grid"><div class="empty">加载中…</div></div>
    <div class="hint">你可以启用、停用、编辑或删除。只有已启用的信息才可能注入模型，并且还要由上方的设备开关允许。</div>
  </div>

  <div class="card full">
    <h2>当前设备 · 对话记录</h2>
    <div class="row" style="margin-bottom:10px">
      <button class="btn" onclick="loadConversations()">刷新记录</button>
      <button class="btn warn" onclick="endConversation()">结束当前会话并整理记忆</button>
      <span class="muted" id="conversation-status"></span>
    </div>
    <div class="chat-layout"><div id="conversation-list" class="conversation-list"><div class="empty">请先绑定并选择设备。</div></div><div id="chat-messages" class="chat-messages"><div class="empty">选择一段会话查看消息。</div></div></div>
  </div>

  <div class="card full">
    <h2>实时日志 <span class="muted" style="text-transform:none">(<span id="log-count">0</span> 条显示 / <span id="log-total">0</span> 条已接收)</span></h2>
    <div class="row" style="margin-bottom:8px">
      <button class="btn warn" id="log-toggle" onclick="toggleLogs()">暂停滚动</button>
      <button class="btn" onclick="clearLogs()">清空显示</button>
    </div>
    <div class="log-filters" style="margin-bottom:8px">
      <span class="muted">类别：</span>
      <label><input type="checkbox" data-log-category="device" checked onchange="renderLogs()"> 设备</label>
      <label><input type="checkbox" data-log-category="mcp" checked onchange="renderLogs()"> MCP</label>
      <label><input type="checkbox" data-log-category="model" checked onchange="renderLogs()"> 模型</label>
      <label><input type="checkbox" data-log-category="system" checked onchange="renderLogs()"> 系统</label>
      <label><input type="checkbox" data-log-category="access" checked onchange="renderLogs()"> 普通请求</label>
      <label><input type="checkbox" data-log-category="periodic" onchange="renderLogs()"> 周期状态请求</label>
    </div>
    <div class="hint">“周期状态请求”是面板自动刷新产生的 `/api/status` 日志，默认隐藏且在服务器上独立限额保存，不会挤掉调试信息。</div>
    <div id="logs"></div>
  </div>

</main>
<div class="modal-backdrop" id="memory-modal" role="dialog" aria-modal="true" aria-labelledby="memory-modal-title">
  <form class="modal" onsubmit="saveMemoryEdit(event)"><h3 id="memory-modal-title">编辑个人记忆</h3>
    <input id="memory-edit-id" type="hidden"><label>分类</label><input id="memory-edit-category" maxlength="40" required>
    <label>标题</label><input id="memory-edit-label" maxlength="80" required><label>内容</label><textarea id="memory-edit-value" maxlength="1000" required></textarea>
    <label><input id="memory-edit-enabled" type="checkbox"> 在对话中使用</label>
    <div class="row" style="justify-content:flex-end;margin:16px 0 0"><button type="button" class="btn warn" onclick="closeMemoryEditor()">取消</button><button type="submit" class="btn">保存修改</button></div>
  </form>
</div>
<div id="toast" role="status" aria-live="polite"></div>

<script>
const $ = id => document.getElementById(id);
let autoRefresh = true;
let dashboardDevices = [];
let ownedDevices = [];
let activeDeviceId = "";
let activeConversationId = "";

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600),
        m = Math.floor(sec%3600/60), s = sec%60;
  if (d) return d + "天" + h + "时";
  if (h) return h + "时" + m + "分";
  if (m) return m + "分" + s + "秒";
  return s + "秒";
}

let toastTimer = null;
function showToast(message, kind) {
  const toast = $("toast");
  const isError = kind === "err";
  toast.className = (isError ? "err" : "ok") + " show";
  toast.innerHTML = '<span class="toast-icon">' + (isError ? "!" : "✓") +
    '</span><span>' + message + '</span>';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.classList.remove("show"); }, 4200);
}

function esc(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

async function apiJson(url, options) {
  const r = await fetch(url, options);
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(data.error || ("请求失败 (" + r.status + ")"));
  return data;
}

function selectedOwnedDevice() {
  return ownedDevices.find(dev => dev.device_id === activeDeviceId);
}

function fillDeviceEditor() {
  const dev = selectedOwnedDevice();
  $("device-edit-identifier").value = dev ? dev.identifier : "";
  $("device-edit-name").value = dev ? dev.name : "";
}

function renderOwnedDeviceControls(devices) {
  ownedDevices = devices;
  const select = $("active-device");
  const previous = activeDeviceId;
  if (!devices.some(dev => dev.device_id === activeDeviceId)) {
    activeDeviceId = devices.length ? devices[0].device_id : "";
  }
  select.innerHTML = devices.length ? devices.map(dev =>
    `<option value="${esc(dev.device_id)}">${esc(dev.name)} · ${esc(dev.identifier)}</option>`
  ).join("") : '<option value="">尚未绑定设备</option>';
  select.value = activeDeviceId;
  select.disabled = devices.length === 0;
  if (previous !== activeDeviceId) {
    fillDeviceEditor();
    loadVad();
    loadModel();
    loadFeatures();
    loadConversations();
  }
}

async function onDeviceChanged() {
  activeDeviceId = $("active-device").value;
  fillDeviceEditor();
  await Promise.all([loadVad(), loadModel(), loadFeatures(), loadConversations()]);
}

async function bindDevice() {
  try {
    const code = $("bind-code").value.trim();
    if (!/^\d{8}$/.test(code)) throw new Error("请输入设备屏幕上的 8 位数字绑定码");
    const body = {
      binding_code: code,
    };
    const dev = await apiJson("/api/devices/bind", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    activeDeviceId = dev.device_id;
    $("bind-code").value = "";
    showToast("设备已绑定，正在重新连接", "ok");
    await refresh();
    await onDeviceChanged();
  } catch (e) { showToast("绑定失败：" + e.message, "err"); }
}

async function updateDevice() {
  if (!activeDeviceId) return showToast("请先选择设备", "err");
  try {
    await apiJson("/api/devices/update", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_id: activeDeviceId,
        identifier: $("device-edit-identifier").value.trim(),
        name: $("device-edit-name").value.trim(),
      })
    });
    showToast("设备信息已保存", "ok");
    await refresh();
  } catch (e) { showToast("保存失败：" + e.message, "err"); }
}

async function unbindDevice() {
  const dev = selectedOwnedDevice();
  if (!dev || !confirm("确定解绑“" + dev.name + "”？设备将重新显示绑定码。")) return;
  try {
    await apiJson("/api/devices/unbind", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: dev.device_id })
    });
    activeDeviceId = "";
    showToast("设备已解绑", "ok");
    await refresh();
  } catch (e) { showToast("解绑失败：" + e.message, "err"); }
}

async function refresh() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    render(d);
  } catch (e) {
    $("health").innerHTML = '<span class="dot bad"></span>无法连接';
  }
}

function render(d) {
  // health
  const ok = d.health && d.health.status === "ok";
  $("health").innerHTML = ok
    ? '<span class="dot ok"></span>服务在线'
    : '<span class="dot bad"></span>服务异常';
  $("uptime").textContent = "运行 " + fmtDur(d.server.uptime);
  $("current-user").textContent = "用户：" + (d.user ? d.user.username : "—");
  $("admin-link").style.display = d.user && d.user.is_admin ? "inline-block" : "none";
  renderOwnedDeviceControls(d.devices || []);

  // devices
  const tbody = $("device-body");
  if (!d.devices.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">尚未绑定设备</td></tr>';
  } else {
    tbody.innerHTML = d.devices.map(dev => {
      let st;
      if (dev.online) {
        st = '<span class="tag online">在线</span> ' +
          (dev.listening ? '<span class="tag listen">聆听中</span>' : "") +
          (dev.speaking ? '<span class="tag listen">播放中</span>' : "");
      } else if (dev.idle) {
        st = '<span class="tag offline">待机中</span>';
      } else {
        st = '<span class="tag offline">未连接</span>';
      }
      const timeStr = dev.online ? fmtDur(dev.connected_for)
        : (dev.idle ? "最后活跃 " + fmtDur(dev.connected_for) + " 前" : "—");
      return `<tr>
        <td>${esc(dev.name)}</td>
        <td class="mono">${esc(dev.identifier)}</td>
        <td class="mono">${esc(dev.device_id)}</td>
        <td>v${esc(dev.firmware_version || "?")}</td>
        <td>${st}</td>
        <td>${timeStr}</td>
        <td class="mono">${esc(dev.session_id || "—")}</td>
      </tr>`;
    }).join("");
  }
  const otaReqs = d.ota_requests || [];
  $("ota-hint").textContent = otaReqs.length
    ? "曾请求过 OTA 的设备: " + otaReqs.join("、") : "暂无设备请求过 OTA";

  // config
  const c = d.config;
  $("cfg-kv").innerHTML = [
    ["OTA URL", c.ota_url],
    ["WebSocket URL", c.ws_url],
    ["模型服务", c.model_base],
    ["API Key", c.api_key_configured ? '<span style="color:var(--ok)">已配置</span>'
                                     : '<span style="color:var(--warn)">未配置 (回环模式)</span>'],
    ["设备鉴权", c.devices_enabled ? "开启" : "关闭"],
    ["输出采样率", c.output_sample_rate + " Hz"],
  ].map(([k,v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  renderHardwareTestControls(d.devices || []);
  loadUsageStats();
}

function renderHardwareTestControls(devices) {
  const select = $("hardware-device");
  const previous = select.value;
  dashboardDevices = devices.filter(dev => dev.online);
  select.innerHTML = dashboardDevices.map(dev =>
    `<option value="${esc(dev.device_id)}">${esc(dev.name)} · ${esc(dev.identifier)} (v${esc(dev.firmware_version || "?")})</option>`
  ).join("");
  select.disabled = dashboardDevices.length === 0;
  if (dashboardDevices.some(dev => dev.device_id === previous)) select.value = previous;
  updateHardwareTestPanel();
}

function updateHardwareTestPanel() {
  const select = $("hardware-device");
  const device = dashboardDevices.find(dev => dev.device_id === select.value);
  const status = $("hardware-test-status");
  if (!device) {
    status.textContent = "没有在线设备。";
    hardwareToolsDevice = "";
    hardwareTools = {};
    renderHardwareTests();
    return;
  }
  status.textContent = "设备在线，可直接执行手动硬件测试。";
  loadHardwareTests(false);
}

// ---- 实时日志 (SSE) ----
let logAuto = true, logEvents = [];
function toggleLogs() {
  logAuto = !logAuto;
  $("log-toggle").textContent = logAuto ? "暂停滚动" : "恢复滚动";
}
function clearLogs() {
  logEvents = [];
  renderLogs();
}
function selectedLogCategories() {
  return new Set(Array.from(document.querySelectorAll("[data-log-category]:checked"))
    .map(input => input.dataset.logCategory));
}
function logLine(event) {
  if (typeof event === "string") event = { category: "system", text: event };
  logEvents.push(event);
  while (logEvents.length > 750) logEvents.shift();
  renderLogs();
}
function renderLogs() {
  const selected = selectedLogCategories();
  const visible = logEvents.filter(event => selected.has(event.category || "system"));
  const box = $("logs");
  box.innerHTML = "";
  for (const event of visible.slice(-500)) {
    const text = event.text || "";
    const div = document.createElement("div");
  const m = text.match(/^([\d\-]+ [\d:,]+) (\w+) (\w+): (.*)$/);
  let cls = "log-info";
  if (m) {
    const lvl = m[2].toLowerCase();
    if (lvl === "warning" || lvl === "warn") cls = "log-warning";
    else if (lvl === "error") cls = "log-error";
    else if (lvl === "debug") cls = "log-debug";
    div.textContent = m[1] + " [" + m[2] + "] " + m[3] + ": " + m[4];
  } else {
    div.textContent = text;
  }
  div.className = cls;
    const category = document.createElement("span");
    category.className = "log-category";
    category.textContent = ({ device:"设备", mcp:"MCP", model:"模型", system:"系统", access:"请求", periodic:"周期" })[event.category] || "系统";
    div.prepend(category);
  box.appendChild(div);
  }
  $("log-count").textContent = visible.length;
  $("log-total").textContent = logEvents.length;
  if (logAuto) box.scrollTop = box.scrollHeight;
}
const es = new EventSource("/api/logs");
es.onmessage = e => {
  try { logLine(JSON.parse(e.data)); }
  catch (_) { logLine(e.data); }
};
es.onerror = () => {};

// ---- VAD 控制 ----
async function loadVad() {
  if (!activeDeviceId) {
    $("vad-status").textContent = "请先绑定并选择设备";
    return;
  }
  try {
    const r = await fetch("/api/vad?device_id=" + encodeURIComponent(activeDeviceId));
    if (!r.ok) return;
    const d = await r.json();
    $("vad-silence").value = d.silence_duration_ms;
    $("vad-threshold").value = d.energy_threshold;
  } catch (e) {}
}
async function saveVad() {
  if (!activeDeviceId) return showToast("请先绑定并选择设备", "err");
  const silence = parseInt($("vad-silence").value, 10);
  const threshold = parseFloat($("vad-threshold").value);
  if (isNaN(silence) || isNaN(threshold)) {
    $("vad-status").textContent = "请输入数字";
    $("vad-status").style.color = "var(--bad)";
    return;
  }
  if (silence < 200 || silence > 6000) {
    $("vad-status").textContent = "静音时长需在 200~6000ms";
    $("vad-status").style.color = "var(--bad)";
    return;
  }
  if (threshold < 1 || threshold > 30000) {
    $("vad-status").textContent = "能量阈值需在 1~30000";
    $("vad-status").style.color = "var(--bad)";
    return;
  }
  const body = { device_id: activeDeviceId, silence_duration_ms: silence, energy_threshold: threshold };
  try {
    const r = await fetch("/api/vad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    $("vad-status").textContent = "已保存: 静音 " + d.silence_duration_ms + "ms, 阈值 " + d.energy_threshold;
    $("vad-status").style.color = "var(--ok)";
  } catch (e) {
    $("vad-status").textContent = "保存失败";
    $("vad-status").style.color = "var(--bad)";
  }
}

// ---- 模型/音色/人物设定 ----
let hardwareTools = {};
let hardwareToolsDevice = "";

const HARDWARE_TEST_GROUPS = [
  { title: "电机驱动", items: [
    ["self.chassis.go_forward", "前进"], ["self.chassis.go_back", "后退"],
    ["self.chassis.turn_left", "左转"], ["self.chassis.turn_right", "右转"],
    ["self.chassis.spin", "原地转圈"], ["self.chassis.test_direct_drive", "高低电平前进测试"],
    ["self.chassis.stop", "停止"]
  ]},
  { title: "摄像头", items: [["self.camera.take_photo", "拍照测试"], ["self.camera.face_detect_local", "本地 ESP-DL 人脸检测"]] },
  { title: "舵机 / 云台", items: [
    ["self.gimbal.center", "云台回中"], ["self.gimbal.pan", "水平舵机"],
    ["self.gimbal.tilt", "俯仰舵机"], ["self.face_tracking.get_state", "跟随状态"]
  ]},
  { title: "屏幕", items: [
    ["self.screen.get_info", "读取屏幕信息"], ["self.screen.set_brightness", "亮度测试"],
    ["self.screen.set_theme", "主题测试"]
  ]},
  { title: "RGB 指示灯", items: [
    ["self.led.get_state", "读取灯状态"], ["self.led.turn_on", "开灯"],
    ["self.led.turn_off", "关灯"], ["self.led.set_color", "设置颜色"]
  ]}
];

function selectedHardwareDevice() {
  return dashboardDevices.find(dev => dev.device_id === $("hardware-device").value);
}

function showCameraPreview(deviceId) {
  const box = $("camera-preview");
  const image = $("camera-preview-image");
  image.onload = () => { box.style.display = "block"; };
  image.onerror = () => { box.style.display = "none"; image.removeAttribute("src"); };
  image.src = "/api/camera/latest?device_id=" + encodeURIComponent(deviceId) + "&v=" + Date.now();
}

function hardwareArgs(name) {
  if (name === "self.led.set_color") return ledRgbValues();
  if (name === "self.chassis.test_direct_drive") {
    return { left_direction: 1, right_direction: 1,
             duration_ms: parseInt($("test-duration").value, 10) || 500 };
  }
  if (name.indexOf("self.chassis.") === 0 && name !== "self.chassis.stop") {
    return { speed: parseInt($("test-speed").value, 10) || 30,
             duration_ms: parseInt($("test-duration").value, 10) || 500 };
  }
  if (name === "self.camera.take_photo") {
    return { question: "检查摄像头是否能正常拍照" };
  }
  if (name === "self.screen.set_brightness") {
    return { brightness: parseInt($("test-brightness").value, 10) || 50 };
  }
  if (name === "self.screen.set_theme") {
    return { theme: $("test-theme").value };
  }
  return {};
}

function clampRgb(value) {
  const number = parseInt(value, 10);
  return Math.max(0, Math.min(255, Number.isFinite(number) ? number : 0));
}

function ledRgbValues() {
  return {
    red: clampRgb($("test-led-red").value),
    green: clampRgb($("test-led-green").value),
    blue: clampRgb($("test-led-blue").value),
  };
}

function ledRgbHex(rgb) {
  return "#" + [rgb.red, rgb.green, rgb.blue]
    .map(channel => channel.toString(16).padStart(2, "0")).join("");
}

function updateLedColorPresentation(rgb) {
  const hex = ledRgbHex(rgb);
  const picker = $("test-led-color");
  const swatch = $("test-led-swatch");
  const hexInput = $("test-led-hex");
  if (picker) picker.value = hex;
  if (swatch) swatch.style.background = hex;
  if (hexInput) {
    hexInput.value = hex.toUpperCase();
    hexInput.setCustomValidity("");
  }
}

function syncLedColorFromRgb() {
  const rgb = ledRgbValues();
  $("test-led-red").value = rgb.red;
  $("test-led-green").value = rgb.green;
  $("test-led-blue").value = rgb.blue;
  updateLedColorPresentation(rgb);
}

function syncLedRgbFromColor() {
  const value = $("test-led-color").value || "#000000";
  const rgb = {
    red: parseInt(value.slice(1, 3), 16),
    green: parseInt(value.slice(3, 5), 16),
    blue: parseInt(value.slice(5, 7), 16),
  };
  $("test-led-red").value = rgb.red;
  $("test-led-green").value = rgb.green;
  $("test-led-blue").value = rgb.blue;
  updateLedColorPresentation(rgb);
}

function syncLedRgbFromHex() {
  const hexInput = $("test-led-hex");
  const match = /^#?([0-9a-fA-F]{6})$/.exec(hexInput.value.trim());
  // Keep incomplete text intact while the user is typing.  Values only update
  // after a complete #RRGGBB (or RRGGBB) color is available.
  if (!match) {
    hexInput.setCustomValidity("请输入 #RRGGBB 格式的颜色代码");
    return;
  }
  const value = match[1];
  const rgb = {
    red: parseInt(value.slice(0, 2), 16),
    green: parseInt(value.slice(2, 4), 16),
    blue: parseInt(value.slice(4, 6), 16),
  };
  $("test-led-red").value = rgb.red;
  $("test-led-green").value = rgb.green;
  $("test-led-blue").value = rgb.blue;
  updateLedColorPresentation(rgb);
}

function renderHardwareTests() {
  const box = $("hardware-test-groups");
  const device = selectedHardwareDevice();
  const canRun = !!device;
  const previousValues = {};
  ["test-speed", "test-duration", "test-brightness", "test-theme", "test-camera-question", "test-led-color", "test-led-hex", "test-led-red", "test-led-green", "test-led-blue"].forEach(id => {
    const input = $(id);
    if (input) previousValues[id] = input.value;
  });
  if (!device) {
    box.innerHTML = '<div class="empty">没有在线设备。</div>';
    return;
  }
  let html = '';
  html += HARDWARE_TEST_GROUPS.map(group => {
    const actions = group.items.map(([name, label]) => {
      const available = !!hardwareTools[name];
      const disabled = !canRun || !available;
      const reason = !available ? " title=\"设备未声明此工具\"" : "";
      return '<button class="btn"' + reason + (disabled ? " disabled" : "") +
        ' onclick=\'runHardwareTest("' + name + '", this)\'>' + label +
        (available ? "" : "（不可用）") + '</button>';
    }).join("");
    const faceActions = group.items.some(item => item[0] === "self.camera.take_photo") ? '<div class="hint" style="margin:8px 0">服务器端人脸测试（识别、录入或替换照片都会重新拍摄当前画面）</div><div class="test-actions"><button class="btn" onclick="runFaceTest(\'recognize_current\', this)">识别当前画面</button><button class="btn" onclick="runFaceTest(\'list\', this)">查询已录入人脸</button><button class="btn" onclick="runFaceTest(\'register_current\', this)">录入当前画面</button><button class="btn" onclick="runFaceTest(\'delete\', this)">删除人脸</button><button class="btn" onclick="runFaceTest(\'update\', this)">修改人脸</button></div>' : '';
    const groupInputs = group.items.some(item => item[0].indexOf("self.chassis.") === 0)
      ? '<div class="row" style="margin-bottom:8px"><label class="muted">速度 <input class="test-input" id="test-speed" type="number" min="0" max="100" value="30"></label>' +
        '<label class="muted">持续时间(ms) <input class="test-input" id="test-duration" type="number" min="1" max="10000" value="500"></label></div>'
      : group.items.some(item => item[0].indexOf("self.screen.") === 0)
        ? '<div class="row" style="margin-bottom:8px"><label class="muted">亮度 <input class="test-input" id="test-brightness" type="number" min="0" max="100" value="50"></label>' +
          '<label class="muted">主题 <select id="test-theme"><option value="light">浅色</option><option value="dark">深色</option></select></label></div>'
        : '';
    const colorInputs = group.title === "RGB 指示灯"
      ? '<div class="row" style="margin-bottom:8px"><label class="muted">色盘 <input class="rgb-picker" id="test-led-color" type="color" value="#0000ff" oninput="syncLedRgbFromColor()"><span class="rgb-swatch" id="test-led-swatch"></span></label>' +
        '<label class="muted">HEX <input class="rgb-hex-input" id="test-led-hex" type="text" value="#0000FF" maxlength="7" spellcheck="false" oninput="syncLedRgbFromHex()" onchange="syncLedRgbFromHex()"></label>' +
        '<label class="muted">R <input class="test-input" id="test-led-red" type="number" min="0" max="255" value="0" oninput="syncLedColorFromRgb()"></label>' +
        '<label class="muted">G <input class="test-input" id="test-led-green" type="number" min="0" max="255" value="0" oninput="syncLedColorFromRgb()"></label>' +
        '<label class="muted">B <input class="test-input" id="test-led-blue" type="number" min="0" max="255" value="255" oninput="syncLedColorFromRgb()"></label></div>' +
        '<div class="hint" style="margin:0 0 8px">色盘与 RGB 数值会同步；当前固件仅提供颜色、开关控制。亮度设置目前仅适用于“屏幕亮度”，RGB 指示灯没有独立亮度接口。</div>' : '';
    return '<div class="test-group"><h3>' + group.title + '</h3>' + groupInputs + colorInputs + '<div class="test-actions">' + actions + '</div>' + faceActions + '</div>';
  }).join("");
  box.innerHTML = html;
  Object.entries(previousValues).forEach(([id, value]) => {
    const input = $(id);
    if (input) input.value = value;
  });
  if ($("test-led-color")) syncLedColorFromRgb();
}

async function loadHardwareTests(force) {
  const device = selectedHardwareDevice();
  if (!device) { hardwareTools = {}; renderHardwareTests(); return; }
  // Status polling runs every three seconds.  Do not replace the controls for
  // the same device during that poll: replacing an <input type=color> closes
  // the browser's native color palette while the user is choosing a color.
  if (!force && hardwareToolsDevice === device.device_id) return;
  hardwareToolsDevice = device.device_id;
  const status = $("hardware-test-status");
  status.textContent = "正在读取设备测试工具…";
  try {
    const r = await fetch("/api/test/tools?device_id=" + encodeURIComponent(device.device_id));
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "读取失败");
    hardwareTools = Object.fromEntries((data.tools || []).map(tool => [tool.name, tool]));
    status.textContent = "已加载 " + Object.keys(hardwareTools).length + " 个设备工具";
  } catch (e) {
    hardwareTools = {};
    status.textContent = "读取测试工具失败：" + e.message;
  }
  renderHardwareTests();
}

async function runHardwareTest(name, button) {
  const device = selectedHardwareDevice();
  if (!device) return;
  button.disabled = true;
  $("hardware-test-result").textContent = "执行中：" + name;
  try {
    const r = await fetch("/api/test/mcp", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: device.device_id, name: name, arguments: hardwareArgs(name), timeout_ms: 15000 }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "调用失败");
    if (name === "self.camera.face_detect_local") {
      renderLocalFaceResult(data.result);
    } else {
      $("hardware-test-result").textContent = JSON.stringify(data.result, null, 2);
    }
    if (name === "self.camera.take_photo") showCameraPreview(device.device_id);
    showToast(name + " 测试完成", "ok");
  } catch (e) {
    $("hardware-test-result").textContent = "失败：" + e.message;
    showToast("硬件测试失败：" + e.message, "err");
  } finally {
    renderHardwareTests();
  }
}

async function runFaceTest(operation, button) {
  const device = selectedHardwareDevice(); if (!device) return;
  let args = {};
  if (operation === "register_current") { const name = prompt("请输入要录入的姓名"); if (!name) return; args.name = name; }
  if (operation === "delete" || operation === "update") { const id = parseInt(prompt("请输入人脸 ID"), 10); if (!Number.isInteger(id)) return; args.face_id = id; if (operation === "delete" && !confirm("确认删除人脸 ID " + id + "？")) return; if (operation === "update") { const name = prompt("请输入新姓名（留空表示不修改）", ""); if (name) args.name = name; } }
  button.disabled = true; $("hardware-test-result").textContent = "执行服务器端人脸测试中：" + operation;
  try { const r = await fetch("/api/test/face", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({device_id:device.device_id, operation:operation, arguments:args})}); const data = await r.json(); if (!r.ok) throw new Error(data.error || "调用失败"); $("hardware-test-result").textContent = JSON.stringify(data.result, null, 2); if (operation !== "list" && operation !== "delete") showCameraPreview(device.device_id); showToast("服务器端人脸测试完成", "ok"); } catch(e) { $("hardware-test-result").textContent = "失败：" + e.message; showToast("服务器端人脸测试失败：" + e.message, "err"); } finally { button.disabled = false; }
}

function renderLocalFaceResult(result) {
  const text = result && result.content && result.content[0] ? result.content[0].text : "";
  let item;
  try { item = JSON.parse(text); } catch (_) { $("hardware-test-result").textContent = text || "检测结果为空"; return; }
  const width = Number(item.width) || 1, height = Number(item.height) || 1;
  const faces = Array.isArray(item.faces) ? item.faces : [];
  const image = typeof item.image_jpeg_base64 === "string" ? item.image_jpeg_base64 : "";
  if (!image) { $("hardware-test-result").textContent = JSON.stringify(item, null, 2); return; }
  const boxes = faces.map((face, index) => {
    const b = Array.isArray(face.box) ? face.box.map(Number) : [];
    if (b.length < 4 || b.some(Number.isNaN)) return "";
    const score = Number(face.confidence);
    const label = "#" + (index + 1) + " " + (Number.isFinite(score) ? score.toFixed(3) : "?");
    const labelY = Math.max(16, b[1] - 4);
    return '<rect class="face-box" x="' + b[0] + '" y="' + b[1] + '" width="' + Math.max(0, b[2]-b[0]) + '" height="' + Math.max(0, b[3]-b[1]) + '"></rect>' +
      '<text class="face-label" x="' + b[0] + '" y="' + labelY + '">' + label + '</text>';
  }).join("");
  const jsonItem = Object.assign({}, item);
  delete jsonItem.image_jpeg_base64;
  $("hardware-test-result").innerHTML = '<div>检测到 <b>' + faces.length + '</b> 张人脸，耗时 ' + (Number(item.elapsed_ms) || 0) + ' ms</div>' +
    '<div class="face-result"><img src="data:image/jpeg;base64,' + image + '" alt="本地人脸检测画面"><svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">' + boxes + '</svg></div>' +
    '<pre class="result-json">' + esc(JSON.stringify(jsonItem, null, 2)) + '</pre>';
}

async function loadModel() {
  if (!activeDeviceId) {
    $("model-status").textContent = "请先绑定并选择设备";
    return;
  }
  try {
    const r = await fetch("/api/model?device_id=" + encodeURIComponent(activeDeviceId));
    if (!r.ok) return;
    const d = await r.json();
    $("cfg-model").value = d.model || "";
    $("cfg-language").value = d.language || "zh";
    $("cfg-instructions").value = d.instructions || "";
    $("cfg-conversation-timeout").value = d.conversation_timeout_minutes || 10;
    const voice = d.voice || "";
    if (Array.from($("cfg-voice").options).some(option => option.value === voice)) {
      $("cfg-voice").value = voice;
    }
  } catch (e) {}
}
async function saveModel() {
  if (!activeDeviceId) return showToast("请先绑定并选择设备", "err");
  const voice = $("cfg-voice").value;
  const body = {
    device_id: activeDeviceId,
    model: $("cfg-model").value.trim(),
    language: $("cfg-language").value,
    voice: voice,
    instructions: $("cfg-instructions").value.trim(),
    conversation_timeout_minutes: parseFloat($("cfg-conversation-timeout").value),
  };
  if (!body.model) {
    $("model-status").textContent = "模型不能为空";
    $("model-status").style.color = "var(--bad)";
    return;
  }
  if (!Number.isFinite(body.conversation_timeout_minutes) || body.conversation_timeout_minutes < 1 || body.conversation_timeout_minutes > 120) {
    $("model-status").textContent = "对话连续时长需为 1～120 分钟";
    $("model-status").style.color = "var(--bad)";
    return;
  }
  const saveButton = $("save-model-btn");
  const normalText = saveButton.textContent;
  saveButton.disabled = true;
  saveButton.textContent = "保存中…";
  try {
    const r = await fetch("/api/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error("save failed");
    const d = await r.json();
    $("model-status").textContent = "已保存: " + d.model + " / " + d.language + " / " + d.voice;
    $("model-status").style.color = "var(--ok)";
    showToast("模型设置已保存 · " + d.model + " / " + d.language + " / " + d.voice, "ok");
  } catch (e) {
    $("model-status").textContent = "保存失败";
    $("model-status").style.color = "var(--bad)";
    showToast("保存失败，请检查服务连接后重试", "err");
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = normalText;
  }
}

async function loadConversations() {
  const box = $("conversation-list");
  const messages = $("chat-messages");
  if (!activeDeviceId) {
    box.innerHTML = '<div class="empty">请先绑定并选择设备。</div>';
    messages.innerHTML = '<div class="empty">选择一段会话查看消息。</div>';
    $("conversation-status").textContent = "";
    return;
  }
  try {
    const data = await apiJson("/api/conversations?device_id=" +
      encodeURIComponent(activeDeviceId) + "&limit=100");
    box.innerHTML = "";
    if (!data.conversations.length) {
      box.innerHTML = '<div class="empty">这台设备还没有对话记录。</div>';
      messages.innerHTML = '<div class="empty">还没有消息。</div>';
    } else {
      if (!data.conversations.some(item => String(item.id) === String(activeConversationId))) {
        activeConversationId = data.conversations[0].id;
      }
      data.conversations.forEach(conversation => {
        const item = document.createElement("div");
        item.className = "conversation-item" + (String(conversation.id) === String(activeConversationId) ? " active" : "");
        item.innerHTML = '<div>' + esc(conversation.title || "新会话") + '</div><div class="muted">' +
          new Date(conversation.last_message_at * 1000).toLocaleString() + ' · ' + conversation.message_count + ' 条消息' +
          (conversation.ended_at ? '' : ' · 进行中') + '</div>';
        item.onclick = () => { activeConversationId = conversation.id; loadConversationMessages(); loadConversations(); };
        if (conversation.ended_at) {
          const remove = document.createElement("button");
          remove.className = "btn warn conversation-delete";
          remove.textContent = "删除";
          remove.onclick = event => {
            event.stopPropagation();
            deleteConversation(conversation.id);
          };
          item.prepend(remove);
        }
        box.appendChild(item);
      });
      await loadConversationMessages();
    }
    $("conversation-status").textContent = data.conversations.length + " 次会话";
  } catch (e) {
    box.innerHTML = '<div class="empty">加载失败。</div>';
    $("conversation-status").textContent = e.message;
  }
}

async function loadConversationMessages() {
  const box = $("chat-messages");
  if (!activeConversationId) return;
  try {
    const data = await apiJson("/api/conversations?conversation_id=" + encodeURIComponent(activeConversationId));
    box.innerHTML = data.messages.map(message =>
      '<div class="chat-message ' + message.role + '"><div class="speaker">' +
      (message.role === "user" ? "你" : "AI") + '</div>' + esc(message.content) + '</div>'
    ).join("") || '<div class="empty">还没有消息。</div>';
    box.scrollTop = box.scrollHeight;
  } catch (e) { box.innerHTML = '<div class="empty">加载消息失败。</div>'; }
}

async function endConversation() {
  if (!activeDeviceId) return showToast("请先选择设备", "err");
  try {
    const data = await apiJson("/api/conversations/end", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({device_id:activeDeviceId})});
    activeConversationId = data.conversation_id || activeConversationId;
    showToast(data.pending ? "将在当前文本保存后结束会话" :
      (data.conversation_id ? "会话已结束，正在整理长期记忆" : "当前没有进行中的会话"), "ok");
    await loadConversations();
    setTimeout(loadMemories, 2500);
  } catch (e) { showToast("结束会话失败：" + e.message, "err"); }
}

async function deleteConversation(id) {
  if (!confirm("删除这次会话记录？删除后将不再用于对话上下文或长期记忆。")) return;
  try {
    await apiJson("/api/conversations/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({conversation_id:id})});
    if (String(activeConversationId) === String(id)) activeConversationId = null;
    showToast("会话记录已删除", "ok");
    await loadConversations();
    await loadMemories();
  } catch (e) { showToast("删除失败：" + e.message, "err"); }
}

async function loadFeatures() {
  if (!activeDeviceId) return;
  try {
    const d = await apiJson("/api/features?device_id=" + encodeURIComponent(activeDeviceId));
    $("feature-memory").checked = d.memory_enabled;
    $("feature-auto-interrupt").checked = d.automatic_interrupt;
    $("feature-button-interrupt").checked = d.button_interrupt;
    $("feature-double-end").checked = d.double_click_end;
  } catch (_) {}
}

async function saveFeatures() {
  if (!activeDeviceId) return showToast("请先选择设备", "err");
  try {
    await apiJson("/api/features", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
      device_id:activeDeviceId, memory_enabled:$("feature-memory").checked,
      automatic_interrupt:$("feature-auto-interrupt").checked,
      button_interrupt:$("feature-button-interrupt").checked,
      double_click_end:$("feature-double-end").checked})});
    $("feature-status").textContent = "已保存并下发";
    showToast("设备功能设置已保存", "ok");
  } catch (e) { showToast("保存失败：" + e.message, "err"); }
}

async function loadMemories() {
  const box = $("memory-list");
  try {
    const data = await apiJson("/api/memories");
    if (!data.memories.length) { box.innerHTML = '<div class="empty">还没有长期记忆。结束一次会话后可自动整理，也可手动添加。</div>'; return; }
    box.innerHTML = data.memories.map(m => '<div class="memory-item"><div class="row"><label><input type="checkbox" ' +
      (m.enabled ? 'checked' : '') + ' onchange="toggleMemory(' + m.id + ',this.checked)"> 使用</label><span class="badge">' + esc(m.category) +
      '</span></div><b>' + esc(m.label) + '</b><div style="margin:5px 0">' + esc(m.value) +
      '</div><div class="row"><button class="btn" onclick="editMemory(' + m.id + ')">编辑</button><button class="btn warn" onclick="deleteMemory(' + m.id + ')">删除</button></div></div>').join('');
    window.currentMemories = data.memories;
  } catch (e) { box.innerHTML = '<div class="empty">加载失败。</div>'; }
}

async function addMemory() {
  try {
    await apiJson("/api/memories", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({category:$("memory-category").value || "其他", label:$("memory-label").value, value:$("memory-value").value})});
    $("memory-label").value = ""; $("memory-value").value = ""; await loadMemories(); showToast("个人信息已添加", "ok");
  } catch(e) { showToast("添加失败：" + e.message, "err"); }
}
async function toggleMemory(id, enabled) { await apiJson("/api/memories/update", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,enabled})}); await loadMemories(); }
async function editMemory(id) {
  const m = (window.currentMemories || []).find(x => x.id === id); if (!m) return;
  $("memory-edit-id").value = m.id; $("memory-edit-category").value = m.category;
  $("memory-edit-label").value = m.label; $("memory-edit-value").value = m.value;
  $("memory-edit-enabled").checked = !!m.enabled; $("memory-modal").classList.add("show");
}
function closeMemoryEditor(){ $("memory-modal").classList.remove("show"); }
async function saveMemoryEdit(event) { event.preventDefault(); try {
  await apiJson("/api/memories/update", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
    id:parseInt($("memory-edit-id").value,10), category:$("memory-edit-category").value,
    label:$("memory-edit-label").value, value:$("memory-edit-value").value,
    enabled:$("memory-edit-enabled").checked})}); closeMemoryEditor(); await loadMemories(); showToast("个人记忆已保存", "ok");
} catch(e) { showToast("保存失败："+e.message, "err"); } }
async function deleteMemory(id) { if (!confirm("确定删除这条个人信息？")) return; await apiJson("/api/memories/delete", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}); await loadMemories(); }

function fmtTokens(value){ return Number(value||0).toLocaleString(); }
function usageCard(label, value){ return '<div class="usage-item"><span class="muted">'+label+'</span><b>'+fmtTokens(value.total_tokens)+' token</b><span class="muted">'+value.turns+' 轮对话 · 输入 '+fmtTokens(value.input_tokens)+' / 输出 '+fmtTokens(value.output_tokens)+'</span></div>'; }
async function loadUsageStats(){ try { const data=await apiJson("/api/usage"); const usage=data.usage||{};
  $("usage-summary").innerHTML=[['今日',usage.today],['本周',usage.week],['本月',usage.month],['累计',usage.all]].map(([label,value])=>usageCard(label,value||{})).join('');
  $("usage-device-hint").textContent=(data.by_device||[]).map(d=>d.name+'：累计 '+fmtTokens((d.usage.all||{}).total_tokens)+' token / '+((d.usage.all||{}).turns||0)+' 轮').join('；') || '尚无已统计的对话。';
}catch(_){ $("usage-summary").innerHTML='<div class="empty">用量加载失败。</div>'; } }

// ---- auto refresh ----
$("autorefresh").addEventListener("change", e => { autoRefresh = e.target.checked; });
setInterval(() => { if (autoRefresh) refresh(); }, 3000);
refresh();
loadMemories();
</script>
</body>
</html>
"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>管理员端 · Tongtong</title>
<style>
:root{--bg:#f4fafc;--card:#fff;--line:#dceaf2;--fg:#18334b;--muted:#708596;--ok:#198764;--bad:#c4475a;--acc:#2d8cf0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}
header{padding:18px 4vw;background:#fff;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;position:sticky;top:0}h1{font-size:19px;margin:0}.spacer{flex:1}main{max-width:1500px;margin:auto;padding:24px 4vw;display:grid;gap:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;overflow:auto}h2{font-size:15px;margin:0 0 14px}.stats{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:12px}.stat{background:#eef8fc;border-radius:12px;padding:15px}.stat b{display:block;font-size:25px}table{width:100%;border-collapse:collapse;min-width:760px}th,td{padding:9px;border-bottom:1px solid #edf3f6;text-align:left}th{color:var(--muted)}button,.btn,input,select{border:1px solid var(--line);border-radius:8px;padding:7px 10px;background:#fff;color:var(--fg)}button,.btn{cursor:pointer;text-decoration:none}button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}button.danger{color:var(--bad)}.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.tag{border-radius:12px;padding:2px 8px;background:#eaf5ff}.off{background:#fff0f2;color:var(--bad)}#msg{position:fixed;right:24px;bottom:24px;padding:12px 16px;background:#17364d;color:#fff;border-radius:10px;display:none}@media(max-width:700px){.stats{grid-template-columns:1fr 1fr}}
</style></head><body>
<header><h1>童童管理员端</h1><span id="env" class="tag">—</span><span class="spacer"></span><a class="btn" href="/">用户面板</a><a class="btn" href="/logout">退出</a></header>
<main>
<section class="stats"><div class="stat">用户<b id="user-count">0</b></div><div class="stat">管理员<b id="admin-count">0</b></div><div class="stat">设备<b id="device-count">0</b></div><div class="stat">在线设备<b id="online-count">0</b></div></section>
<section class="card"><h2>用户管理</h2><div class="row"><input id="new-user" placeholder="用户名"><input id="new-password" type="password" placeholder="初始密码（至少 8 位）"><label><input id="new-admin" type="checkbox"> 管理员</label><button class="primary" onclick="createUser()">创建用户</button></div>
<table><thead><tr><th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>设备</th><th>会话</th><th>累计 Token</th><th>操作</th></tr></thead><tbody id="users"></tbody></table></section>
<section class="card"><h2>设备管理</h2><div class="row"><label>按用户查询 <select id="device-user-filter" onchange="load()"><option value="">全部用户 / 未绑定设备</option></select></label></div><table><thead><tr><th>设备 ID</th><th>名称/识别码</th><th>所有者</th><th>设备状态</th><th>在线</th><th>累计 / 本月 Token</th><th>最后出现</th><th>操作</th></tr></thead><tbody id="devices"></tbody></table></section>
<section class="card"><h2>服务器端人脸识别服务</h2><p>用于对话中的人脸录入、识别、删除和修改。修改后立即对新会话生效。</p><div class="row"><input id="face-service-url" style="min-width:340px" placeholder="服务地址"><input id="face-service-user" placeholder="登录用户名"><input id="face-service-password" type="password" placeholder="登录密码"></div><div class="row"><button class="primary" onclick="saveFaceServiceSettings()">保存人脸服务配置</button></div></section>
<section class="card"><h2>固件版本库（最多 20 个版本）</h2><p>上传后可选择在线设备下发指定版本。设备接受指令不代表已经完成下载和重启，请结合进度和日志确认结果。</p><div class="row"><input id="firmware-version" placeholder="版本号，例如 1.0.1"><input id="firmware-description" style="min-width:320px" placeholder="版本描述"><input id="firmware-file" type="file" accept=".bin"><button class="primary" onclick="uploadFirmware()">上传固件</button></div><table><thead><tr><th>ID</th><th>版本</th><th>描述</th><th>大小</th><th style="width:150px">SHA-256</th><th>上传时间</th><th>选择设备</th><th>下发</th><th>进度/日志</th><th>操作</th></tr></thead><tbody id="firmware-releases"></tbody></table></section>
<section class="card"><h2>全局工具规则</h2><p>此规则适用于所有用户和所有设备，普通用户不可修改。</p><textarea id="global-tool-instructions" rows="6" style="width:100%;font:13px/1.5 Consolas,monospace;padding:9px"></textarea><div class="row"><button class="primary" onclick="saveGlobalSettings()">保存全局工具规则</button></div></section>
<section class="card"><h2>管理员操作审计</h2><table><thead><tr><th>时间</th><th>管理员</th><th>操作</th><th>对象</th><th>详情</th></tr></thead><tbody id="audit"></tbody></table><div class="row" style="align-items:center;margin:14px 0 0"><button onclick="changeAuditPage(-1)">上一页</button><span id="audit-page-info" class="muted"></span><button onclick="changeAuditPage(1)">下一页</button><label>每页 <select id="audit-page-size" onchange="auditPage=1;load()"><option value="10">10</option><option value="20" selected>20</option><option value="50">50</option></select> 条</label></div></section>
</main><div id="msg"></div>
<script>
let data={users:[],devices:[]},firmware={releases:[]},auditPage=1; const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(url,options={}){const r=await fetch(url,options);let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.error||("HTTP "+r.status));return d}
function toast(s,bad=false){const n=document.getElementById("msg");n.textContent=s;n.style.background=bad?"#a93649":"#17364d";n.style.display="block";setTimeout(()=>n.style.display="none",2800)}
async function load(){try{const selected=document.getElementById("device-user-filter")?.value||"";const params=new URLSearchParams();if(selected)params.set("user_id",selected);params.set("audit_page",auditPage);params.set("audit_page_size",document.getElementById("audit-page-size")?.value||20);data=await api("/api/admin/overview?"+params.toString());firmware=await api("/api/admin/firmware");firmware.deployments=(await api("/api/admin/firmware/deployments")).deployments||[];render();renderFirmware();const settings=await api("/api/admin/settings");document.getElementById("global-tool-instructions").value=settings.tool_instructions||""}catch(e){toast(e.message,true)}}
async function saveGlobalSettings(){try{await api("/api/admin/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tool_instructions:document.getElementById("global-tool-instructions").value})});toast("全局工具规则已保存并应用")}catch(e){toast(e.message,true)}}
function render(){document.getElementById("env").textContent="环境："+data.environment;document.getElementById("user-count").textContent=data.counts.users;document.getElementById("admin-count").textContent=data.counts.admins;document.getElementById("device-count").textContent=data.counts.devices;document.getElementById("online-count").textContent=data.counts.online_devices;
const filter=document.getElementById("device-user-filter"),selected=String(data.selected_user_id??filter.value??"");filter.innerHTML='<option value="">全部用户 / 未绑定设备</option>'+data.users.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('');filter.value=selected;const usage=new Map((data.usage_by_user_device||[]).map(x=>[x.user_id+'|'+x.device_id,x]));const userTokens=id=>(data.usage_by_user_device||[]).filter(x=>x.user_id===id).reduce((s,x)=>s+Number(x.total_tokens||0),0);
document.getElementById("users").innerHTML=data.users.map(u=>`<tr><td>${u.id}</td><td>${esc(u.username)}</td><td>${u.is_admin?'<span class="tag">管理员</span>':'用户'}</td><td>${u.is_active?'启用':'<span class="tag off">禁用</span>'}</td><td>${u.device_count}</td><td>${u.active_session_count}</td><td>${userTokens(u.id).toLocaleString()}</td><td><button onclick="toggleRole(${u.id},${!u.is_admin})">${u.is_admin?'取消管理员':'设为管理员'}</button> <button onclick="toggleActive(${u.id},${!u.is_active})">${u.is_active?'禁用':'启用'}</button> <button onclick="resetPassword(${u.id})">重置密码</button> <button class="danger" onclick="deleteUser(${u.id})">删除</button></td></tr>`).join("");
const opts='<option value="">未绑定</option>'+data.users.filter(u=>u.is_active).map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('');
document.getElementById("devices").innerHTML=data.devices.map(d=>{const u=usage.get((d.owner_user_id||'')+'|'+d.device_id)||{};return `<tr><td><code>${esc(d.device_id)}</code></td><td>${esc(d.name||'—')}<br><small>${esc(d.identifier||'—')}</small></td><td><select id="owner-${esc(d.device_id)}">${opts}</select></td><td>${d.is_active?'<span class="tag">启用</span>':'<span class="tag off">已禁用</span>'}</td><td>${d.online?'<span class="tag">在线</span>':'离线'}</td><td>${Number(u.total_tokens||0).toLocaleString()} / ${Number(u.month_tokens||0).toLocaleString()}<br><small>${u.turns||0} 轮</small></td><td>${new Date(d.last_seen*1000).toLocaleString()}</td><td><button onclick="assignDevice('${esc(d.device_id)}')">保存所有者</button> <button onclick="toggleDevice('${esc(d.device_id)}',${!d.is_active})">${d.is_active?'禁用设备':'启用设备'}</button> <button onclick="switchEnvironment('${esc(d.device_id)}')" ${d.online&&d.is_active?'':'disabled'}>切换环境</button> <button onclick="unbindDevice('${esc(d.device_id)}')">解绑</button> <button class="danger" onclick="deleteDevice('${esc(d.device_id)}')">删除记录</button></td></tr>`}).join('');data.devices.forEach(d=>{const e=document.getElementById('owner-'+d.device_id);if(e)e.value=d.owner_user_id||''});
auditPage=Number(data.audit_page||auditPage);document.getElementById("audit").innerHTML=data.audit.map(a=>`<tr><td>${new Date(a.created_at*1000).toLocaleString()}</td><td>${esc(a.admin_username||a.admin_user_id||'—')}</td><td>${esc(a.action)}</td><td>${esc(a.target_type)} #${esc(a.target_id)}</td><td><code>${esc(JSON.stringify(a.details))}</code></td></tr>`).join('')||'<tr><td colspan="5">暂无审计记录</td></tr>';const total=Number(data.audit_total||0),size=Number(data.audit_page_size||20),pages=Math.max(1,Math.ceil(total/size));document.getElementById('audit-page-info').textContent=`第 ${auditPage} / ${pages} 页，共 ${total} 条`;document.querySelector('#audit-page-info').previousElementSibling.disabled=auditPage<=1;document.querySelector('#audit-page-info').nextElementSibling.disabled=auditPage>=pages}
function renderFirmware(){const devices=data.devices||[];document.getElementById('firmware-releases').innerHTML=(firmware.releases||[]).map(r=>{const opts='<option value="">选择在线设备</option>'+devices.filter(d=>d.online&&d.is_active).map(d=>`<option value="${esc(d.device_id)}">${esc(d.name||d.device_id)}</option>`).join('');const jobs=(firmware.deployments||[]).filter(j=>j.release_id===r.id);const j=jobs[jobs.length-1];const pct=j?Number(j.progress||0):0;const logs=j?(j.logs||[]).slice(-5).map(x=>new Date(x.time*1000).toLocaleTimeString()+' '+x.message).join('\\n'):'';return `<tr><td>${r.id}</td><td><b>${esc(r.version)}</b></td><td>${esc(r.description||'—')}</td><td>${(Number(r.size_bytes)/1024/1024).toFixed(2)} MiB</td><td style="max-width:150px;word-break:break-all;font-size:11px"><code>${esc(r.sha256)}</code></td><td>${new Date(r.created_at*1000).toLocaleString()}</td><td><select id="fw-device-${r.id}">${opts}</select></td><td><button onclick="deployFirmware(${r.id})">下发</button></td><td>${j?`<progress max="100" value="${pct}"></progress> ${pct}%<br><small>${esc(j.message)}</small><pre style="max-width:360px;white-space:pre-wrap;font-size:11px">${esc(logs)}</pre><button onclick="clearFirmwareLog('${j.id}')">清除日志</button>`:'—'}</td><td><a class="btn" href="/api/admin/firmware/${r.id}/download">下载</a> <button class="danger" onclick="deleteFirmware(${r.id})">删除</button></td></tr>`}).join('')||'<tr><td colspan="10">暂无固件版本</td></tr>'}
async function uploadFirmware(){const file=document.getElementById('firmware-file').files[0];if(!file){toast('请选择 .bin 文件',true);return}const form=new FormData();form.append('version',document.getElementById('firmware-version').value);form.append('description',document.getElementById('firmware-description').value);form.append('file',file);try{await api('/api/admin/firmware',{method:'POST',body:form});toast('固件上传成功');document.getElementById('firmware-file').value='';await load()}catch(e){toast(e.message,true)}}
async function deployFirmware(id){const device_id=document.getElementById('fw-device-'+id).value;if(!device_id){toast('请选择在线设备',true);return}if(!confirm('确认向该设备下发指定固件？设备将下载后重启。'))return;try{const d=await api('/api/admin/firmware/'+id+'/deploy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id})});toast(d.message||'升级指令已下发')}catch(e){toast(e.message,true)}}
async function deleteFirmware(id){if(!confirm('确认删除这个固件版本？'))return;try{await api('/api/admin/firmware/'+id,{method:'DELETE'});toast('固件版本已删除');await load()}catch(e){toast(e.message,true)}}
async function clearFirmwareLog(id){try{await api('/api/admin/firmware/deployments/'+encodeURIComponent(id),{method:'DELETE'});toast('日志已清除');await load()}catch(e){toast(e.message,true)}}
function changeAuditPage(delta){const total=Number(data.audit_total||0),size=Number(data.audit_page_size||20),pages=Math.max(1,Math.ceil(total/size));auditPage=Math.max(1,Math.min(pages,auditPage+delta));load()}
async function post(url,body){await api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load();toast('操作成功')}
async function createUser(){try{await post('/api/admin/users/create',{username:document.getElementById('new-user').value,password:document.getElementById('new-password').value,is_admin:document.getElementById('new-admin').checked})}catch(e){toast(e.message,true)}}
async function toggleRole(id,is_admin){try{await post('/api/admin/users/update',{user_id:id,is_admin})}catch(e){toast(e.message,true)}}async function toggleActive(id,is_active){try{await post('/api/admin/users/update',{user_id:id,is_active})}catch(e){toast(e.message,true)}}
async function resetPassword(id){const password=prompt('输入新密码（至少 8 位）');if(!password)return;try{await post('/api/admin/users/update',{user_id:id,password})}catch(e){toast(e.message,true)}}async function deleteUser(id){if(!confirm('永久删除该用户及其对话、记忆？设备会变为未绑定。'))return;try{await post('/api/admin/users/delete',{user_id:id})}catch(e){toast(e.message,true)}}
async function assignDevice(id){const owner=document.getElementById('owner-'+id).value;try{await post('/api/admin/devices/assign',{device_id:id,owner_user_id:owner||null})}catch(e){toast(e.message,true)}}async function unbindDevice(id){if(!confirm('确认解绑设备？'))return;try{await post('/api/admin/devices/assign',{device_id:id,owner_user_id:null})}catch(e){toast(e.message,true)}}async function deleteDevice(id){if(!confirm('永久删除设备登记？设备再次连接时会重新登记。'))return;try{await post('/api/admin/devices/delete',{device_id:id})}catch(e){toast(e.message,true)}}load();setInterval(load,10000);
async function toggleDevice(id,is_active){if(!confirm(is_active?'确认启用该设备？':'确认禁用该设备？禁用后设备会断开且无法连接。'))return;try{await post('/api/admin/devices/update',{device_id:id,is_active})}catch(e){toast(e.message,true)}}
async function switchEnvironment(id){const environment=prompt('目标环境：test 或 production','test');if(!environment)return;const ota_url=prompt('目标环境 OTA 地址，例如 http://192.168.31.237:8082/ota');if(!ota_url)return;if(!confirm('设备将保存新地址并立即重启，确认继续？'))return;try{await post('/api/admin/devices/switch-environment',{device_id:id,environment,ota_url,reboot:true})}catch(e){toast(e.message,true)}}
async function loadFaceServiceSettings(){try{const s=await api("/api/admin/settings");const f=s.face_service||{};document.getElementById("face-service-url").value=f.base_url||"";document.getElementById("face-service-user").value=f.username||"";document.getElementById("face-service-password").value=f.password||""}catch(e){toast(e.message,true)}}
async function saveFaceServiceSettings(){try{await api("/api/admin/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({face_service:{base_url:document.getElementById("face-service-url").value,username:document.getElementById("face-service-user").value,password:document.getElementById("face-service-password").value}})});toast("人脸服务配置已保存并应用")}catch(e){toast(e.message,true)}}
loadFaceServiceSettings();
</script></body></html>"""


# ---------------------------------------------------------------------------
# Dashboard 路由
# ---------------------------------------------------------------------------
class Dashboard:
    def __init__(self, config: dict, sessions: dict, http_api, log_handler: BroadcastLogHandler,
                 device_history: dict = None, save_config: callable = None,
                 account_store=None, gateway=None, memory_service=None):
        self.config = config
        self.sessions = sessions  # device_id -> Session
        self.device_history = device_history or {}  # device_id -> {last_seen, client_id}
        self.http_api = http_api
        self.log_handler = log_handler
        self.save_config = save_config
        self.account_store = account_store
        self.gateway = gateway
        self.memory_service = memory_service
        self.start_time = time.time()
        dash_cfg = config.get("dashboard", {})
        self.session_ttl = int(dash_cfg.get("session_ttl", 86400))
        self.registration_enabled = bool(dash_cfg.get("registration_enabled", True))
        self._binding_failures = {}
        # Latest JPEG per device. Photos are intentionally ephemeral: they
        # are lost on restart and never written to disk.
        shared_photos = (getattr(gateway, "camera_photos", None)
                         if gateway is not None else None)
        self._camera_photos = shared_photos if shared_photos is not None else {}
        storage_cfg = config.get("storage", {})
        firmware_dir = storage_cfg.get("firmware_directory", "data/firmware")
        self._firmware_dir = Path(firmware_dir)
        if not self._firmware_dir.is_absolute():
            self._firmware_dir = Path(__file__).resolve().parents[1] / self._firmware_dir
        self._firmware_dir.mkdir(parents=True, exist_ok=True)
        self._firmware_tokens = {}
        self._firmware_deployments = {}

    def _render_page(self, template):
        """Add a prominent warning to the non-production dashboard only."""
        port = self.config.get("server", {}).get("port")
        warning = ""
        if str(port) == "8081":
            warning = (
                '<div class="env-warning" role="alert">'
                '⚠️ 当前为测试页面（8081），禁止登录主测试/生产后端账号，避免误操作真实设备和数据。'
                '</div>'
            )
        return template.replace("<!--ENV_WARNING-->", warning)

    def add_routes(self, app: web.Application):
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_get("/register", self.register_page)
        app.router.add_post("/register", self.register)
        app.router.add_get("/logout", self.logout)
        app.router.add_get("/admin", self.admin_page)
        app.router.add_get("/api/me", self.api_me)
        app.router.add_get("/api/devices", self.api_devices)
        app.router.add_post("/api/devices/bind", self.api_device_bind)
        app.router.add_post("/api/devices/update", self.api_device_update)
        app.router.add_post("/api/devices/unbind", self.api_device_unbind)
        app.router.add_get("/api/conversations", self.api_conversations)
        app.router.add_post("/api/conversations/end", self.api_conversation_end)
        app.router.add_post("/api/conversations/delete", self.api_conversation_delete)
        app.router.add_get("/api/memories", self.api_memories)
        app.router.add_post("/api/memories", self.api_memory_create)
        app.router.add_post("/api/memories/update", self.api_memory_update)
        app.router.add_post("/api/memories/delete", self.api_memory_delete)
        app.router.add_post("/api/memories/summarize", self.api_memory_summarize)
        app.router.add_get("/api/usage", self.api_usage)
        app.router.add_get("/api/features", self.api_features_get)
        app.router.add_post("/api/features", self.api_features_set)
        app.router.add_get("/api/status", self.api_status)
        app.router.add_get("/api/logs", self.api_logs)
        app.router.add_get("/api/vad", self.api_vad_get)
        app.router.add_post("/api/vad", self.api_vad_set)
        app.router.add_get("/api/model", self.api_model_get)
        app.router.add_post("/api/model", self.api_model_set)
        app.router.add_get("/api/test/tools", self.api_test_tools)
        app.router.add_post("/api/test/mcp", self.api_test_mcp)
        app.router.add_post("/api/test/face", self.api_test_face)
        app.router.add_post("/api/camera/upload", self.api_camera_upload)
        app.router.add_get("/api/camera/latest", self.api_camera_latest)
        app.router.add_get("/api/admin/overview", self.api_admin_overview)
        app.router.add_get("/api/admin/settings", self.api_admin_settings_get)
        app.router.add_post("/api/admin/settings", self.api_admin_settings_set)
        app.router.add_get("/api/admin/firmware", self.api_admin_firmware_list)
        app.router.add_post("/api/admin/firmware", self.api_admin_firmware_upload)
        app.router.add_delete("/api/admin/firmware/{release_id}",
                              self.api_admin_firmware_delete)
        app.router.add_get("/api/admin/firmware/{release_id}/download",
                           self.api_admin_firmware_download)
        app.router.add_post("/api/admin/firmware/{release_id}/deploy",
                            self.api_admin_firmware_deploy)
        app.router.add_delete("/api/admin/firmware/deployments/{deployment_id}",
                              self.api_admin_firmware_deployment_clear)
        app.router.add_get("/api/admin/firmware/deployments",
                           self.api_admin_firmware_deployments)
        app.router.add_get("/ota/firmware/{token}", self.ota_firmware_download)
        app.router.add_post("/api/admin/users/create", self.api_admin_user_create)
        app.router.add_post("/api/admin/users/update", self.api_admin_user_update)
        app.router.add_post("/api/admin/users/delete", self.api_admin_user_delete)
        app.router.add_post("/api/admin/devices/assign", self.api_admin_device_assign)
        app.router.add_post("/api/admin/devices/update", self.api_admin_device_update)
        app.router.add_post("/api/admin/devices/delete", self.api_admin_device_delete)
        app.router.add_post("/api/admin/devices/switch-environment",
                            self.api_admin_device_switch_environment)

    # ------------------------------------------------------------------
    # 鉴权辅助
    # ------------------------------------------------------------------
    def _current_user(self, request):
        if self.account_store is None:
            return None
        return self.account_store.user_from_session(request.cookies.get(AUTH_COOKIE, ""))

    def _check_cookie(self, request) -> bool:
        return self._current_user(request) is not None

    def _redirect_login(self):
        raise web.HTTPFound("/login")

    def _require_user(self, request):
        user = self._current_user(request)
        if user is None:
            raise web.HTTPUnauthorized(text="unauthorized")
        return user

    def _require_owned_device(self, request, device_id):
        user = self._require_user(request)
        if not self.account_store.user_can_access_device(user["id"], device_id):
            raise web.HTTPForbidden(text="device does not belong to this user")
        return user

    def _require_admin(self, request):
        user = self._require_user(request)
        if not user.get("is_admin"):
            raise web.HTTPForbidden(text="administrator access required")
        return user

    @staticmethod
    def _target_user_id(user, requested_user_id):
        if requested_user_id in (None, ""):
            return user["id"]
        try:
            target_user_id = int(requested_user_id)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="invalid user_id") from exc
        if target_user_id != user["id"] and not user.get("is_admin"):
            raise web.HTTPForbidden(text="administrator access required")
        return target_user_id

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def login_page(self, request):
        if self._check_cookie(request):
            raise web.HTTPFound("/")
        return web.Response(text=self._render_page(LOGIN_HTML), content_type="text/html", charset="utf-8")

    async def login(self, request):
        data = await request.post()
        username = data.get("username", "")
        password = data.get("password", "")
        user = self.account_store.authenticate(username, password) if self.account_store else None
        if user is None:
            log.warning("dashboard 登录失败 (username=%s ip=%s)", username, request.remote)
            return web.Response(text=self._render_page(LOGIN_HTML.replace(
                "<!--ERROR-->",
                '<div class="error">用户名或密码错误</div>'
            )), content_type="text/html", charset="utf-8")
        log.info("dashboard 登录成功 (user=%s ip=%s)", user["username"], request.remote)
        token = self.account_store.create_session(user["id"], self.session_ttl)
        resp = web.HTTPFound("/")
        resp.set_cookie(AUTH_COOKIE, token, max_age=self.session_ttl,
                        httponly=True, samesite="Lax", secure=request.secure)
        raise resp

    async def register_page(self, request):
        if self._check_cookie(request):
            raise web.HTTPFound("/")
        if not self.registration_enabled:
            raise web.HTTPForbidden(text="registration disabled")
        return web.Response(text=self._render_page(REGISTER_HTML), content_type="text/html", charset="utf-8")

    async def register(self, request):
        if not self.registration_enabled:
            raise web.HTTPForbidden(text="registration disabled")
        data = await request.post()
        try:
            user = self.account_store.register_user(
                data.get("username", ""), data.get("password", ""))
        except ValueError as exc:
            return web.Response(text=self._render_page(REGISTER_HTML.replace(
                "<!--ERROR-->", '<div class="error">{}</div>'.format(
                    str(exc).replace("<", "&lt;").replace(">", "&gt;"))
            )), content_type="text/html", charset="utf-8", status=400)
        token = self.account_store.create_session(user["id"], self.session_ttl)
        log.info("dashboard 用户注册成功 (user=%s ip=%s)", user["username"], request.remote)
        resp = web.HTTPFound("/")
        resp.set_cookie(AUTH_COOKIE, token, max_age=self.session_ttl,
                        httponly=True, samesite="Lax", secure=request.secure)
        raise resp

    async def logout(self, request):
        if self.account_store:
            self.account_store.revoke_session(request.cookies.get(AUTH_COOKIE, ""))
        resp = web.HTTPFound("/login")
        resp.del_cookie(AUTH_COOKIE)
        raise resp

    def add_routes_public(self, app):  # no-op, 保留接口
        pass

    async def index(self, request):
        if not self._check_cookie(request):
            raise web.HTTPFound("/login")
        return web.Response(text=self._render_page(DASHBOARD_HTML), content_type="text/html", charset="utf-8")

    async def admin_page(self, request):
        self._require_admin(request)
        return web.Response(
            text=ADMIN_HTML, content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    async def api_admin_overview(self, request):
        self._require_admin(request)
        users = self.account_store.list_users_for_admin()
        owner_filter = request.query.get("user_id")
        if owner_filter not in (None, ""):
            owner_filter = int(owner_filter)
            devices = self.account_store.list_devices_owned_by(owner_filter)
        else:
            devices = self.account_store.list_user_devices(self._current_user(request)["id"])
        for device in devices:
            session = self.sessions.get(device["device_id"])
            device["online"] = bool(
                session is not None and
                not getattr(getattr(session, "ws", None), "closed", False))
        audit_page_size = request.query.get("audit_page_size", 20)
        audit_page = request.query.get("audit_page", 1)
        audit, audit_total = self.account_store.list_admin_audit(
            audit_page_size, audit_page)
        return web.json_response({
            "environment": self.config.get("environment", "production"),
            "counts": {
                "users": len(users),
                "admins": sum(1 for item in users if item["is_admin"] and item["is_active"]),
                "devices": len(devices),
                "online_devices": sum(1 for item in devices if item["online"]),
            },
            "capabilities": self.config.get("runtime", {}),
            "users": users,
            "devices": devices,
            "selected_user_id": owner_filter,
            "usage_by_user_device": self.account_store.usage_by_user_device(),
            "audit": audit,
            "audit_total": audit_total,
            "audit_page": max(1, int(audit_page)),
            "audit_page_size": max(1, min(100, int(audit_page_size))),
        })

    async def api_admin_settings_get(self, request):
        self._require_admin(request)
        from .omni_client import DEFAULT_TOOL_INSTRUCTIONS
        face_service = dict(self.config.get("face_service", {}))
        return web.json_response({
            "tool_instructions": (
                self.account_store.get_global_setting("tool_instructions")
                or DEFAULT_TOOL_INSTRUCTIONS),
            "face_service": face_service,
        })

    async def api_admin_settings_set(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            tool_instructions = (data.get("tool_instructions") or "").strip()
            if len(tool_instructions) > 12000:
                raise ValueError("invalid tool instructions")
            self.account_store.set_global_setting(
                "tool_instructions", tool_instructions)
            face_data = data.get("face_service")
            if face_data is not None:
                if not isinstance(face_data, dict):
                    raise ValueError("invalid face service settings")
                base_url = str(face_data.get("base_url", "")).strip().rstrip("/")
                username = str(face_data.get("username", "")).strip()
                password = str(face_data.get("password", ""))
                if (not base_url.startswith(("http://", "https://")) or
                        len(base_url) > 300 or not username or len(username) > 100 or
                        not password or len(password) > 200):
                    raise ValueError("invalid face service settings")
                self.config["face_service"] = {
                    "base_url": base_url,
                    "username": username,
                    "password": password,
                }
                from .face_service import FaceService
                for session in self.sessions.values():
                    session.config["face_service"] = dict(self.config["face_service"])
                    session.face_service = FaceService(session.config)
                if self.save_config:
                    self.save_config(self.config)
                self.account_store.audit_admin_action(
                    admin["id"], "settings.update", "app", "face_service", {
                        "base_url": base_url, "username": username})
            for session in self.sessions.values():
                session.config.setdefault("dashscope", {})[
                    "tool_instructions"] = tool_instructions
            self.account_store.audit_admin_action(
                admin["id"], "settings.update", "app", "tool_instructions", {})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({
            "tool_instructions": tool_instructions,
            "face_service": dict(self.config.get("face_service", {})),
        })

    def _firmware_public_base(self):
        base = self.config.get("server", {}).get("public_ws_url", "")
        base = base.replace("wss://", "https://").replace("ws://", "http://")
        if base.endswith("/ws"):
            base = base[:-3]
        return base.rstrip("/")

    @staticmethod
    def _firmware_json(row):
        item = dict(row)
        item["size_bytes"] = int(item["size_bytes"])
        return item

    async def api_admin_firmware_list(self, request):
        self._require_admin(request)
        return web.json_response({
            "max_versions": 20,
            "releases": [self._firmware_json(row)
                         for row in self.account_store.list_firmware_releases()],
        })

    async def api_admin_firmware_upload(self, request):
        admin = self._require_admin(request)
        temp_path = None
        target_path = None
        try:
            reader = await request.multipart()
            version = ""
            description = ""
            original_name = "firmware.bin"
            file_part = None
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "file":
                    file_part = part
                    original_name = Path(part.filename or "firmware.bin").name
                    break
                value = await part.text()
                if part.name == "version":
                    version = value
                elif part.name == "description":
                    description = value
            if file_part is None:
                raise ValueError("请选择固件 .bin 文件")
            if not version.strip() or len(version.strip()) > 64 or any(
                    char in version for char in "\\/\r\n"):
                raise ValueError("版本号不能为空，且不能包含路径字符")
            if len(description) > 2000:
                raise ValueError("固件描述不能超过 2000 个字符")
            if not original_name.lower().endswith(".bin"):
                raise ValueError("固件文件必须是 .bin")
            temp_path = self._firmware_dir / (".upload-{}".format(secrets.token_hex(12)))
            digest = hashlib.sha256()
            size = 0
            with temp_path.open("wb") as handle:
                while True:
                    chunk = await file_part.read_chunk(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise ValueError("固件文件不能超过 16 MiB")
                    digest.update(chunk)
                    handle.write(chunk)
            stored_name = "{}-{}.bin".format(
                time.strftime("%Y%m%d%H%M%S"), secrets.token_hex(8))
            target_path = self._firmware_dir / stored_name
            os.replace(str(temp_path), str(target_path))
            temp_path = None
            try:
                release = self.account_store.create_firmware_release(
                    version.strip(), description.strip(), stored_name, original_name,
                    digest.hexdigest(), size, admin["id"])
            except Exception:
                try:
                    target_path.unlink()
                except FileNotFoundError:
                    pass
                target_path = None
                raise
            target_path = None
            self.account_store.audit_admin_action(
                admin["id"], "firmware.upload", "firmware", release["id"], {
                    "version": release["version"], "size_bytes": size,
                    "sha256": release["sha256"]})
            return web.json_response(self._firmware_json(release), status=201)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
            if target_path is not None:
                try:
                    target_path.unlink()
                except FileNotFoundError:
                    pass

    async def api_admin_firmware_delete(self, request):
        admin = self._require_admin(request)
        try:
            release = self.account_store.delete_firmware_release(
                int(request.match_info["release_id"]))
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        path = self._firmware_dir / Path(release["stored_name"]).name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.account_store.audit_admin_action(
            admin["id"], "firmware.delete", "firmware", release["id"],
            {"version": release["version"]})
        return web.json_response({"success": True})

    async def api_admin_firmware_download(self, request):
        self._require_admin(request)
        try:
            release = self.account_store.get_firmware_release(
                int(request.match_info["release_id"]))
        except (ValueError, TypeError):
            release = None
        if release is None:
            raise web.HTTPNotFound(text="firmware release not found")
        path = self._firmware_dir / Path(release["stored_name"]).name
        if not path.is_file():
            raise web.HTTPNotFound(text="firmware file not found")
        return web.FileResponse(path, headers={
            "Content-Disposition": "attachment; filename={}".format(
                Path(release["original_name"]).name),
            "X-Firmware-Version": release["version"],
            "X-Firmware-SHA256": release["sha256"],
        })

    def _firmware_deployment_update(self, deployment_id, status, progress, message):
        job = self._firmware_deployments.get(deployment_id)
        if job is None:
            return
        job["status"] = status
        job["progress"] = int(progress)
        job["message"] = message
        job["updated_at"] = time.time()
        logs = job.setdefault("logs", [])
        if not logs or any(logs[-1].get(key) != value for key, value in (
                ("status", status), ("progress", int(progress)),
                ("message", message))):
            logs.append({
                "time": job["updated_at"], "status": status,
                "progress": int(progress), "message": message})

    async def _run_firmware_deployment(self, deployment_id, session):
        job = self._firmware_deployments[deployment_id]
        self._firmware_deployment_update(
            deployment_id, "command_sent", 20, "已向设备发送 OTA 指令，等待设备处理")
        try:
            result = await session.mcp.call_tool(
                "self.upgrade_firmware", {"url": job["url"]}, timeout=8)
            job["tool_result"] = result
            self._firmware_deployment_update(
                deployment_id, "command_accepted", 35, "设备返回 OTA 指令已接受")
        except Exception as exc:
            # The firmware starts OTA asynchronously and normally reboots before
            # its JSON-RPC response can reach this connection.
            job["tool_error"] = str(exc) or type(exc).__name__
            if job["status"] != "downloaded":
                self._firmware_deployment_update(
                    deployment_id, "rebooting", 35,
                    "设备连接已断开，推测正在重启并执行 OTA；继续等待下载请求")
            else:
                self._firmware_deployment_update(
                    deployment_id, "downloaded", 80,
                    "固件已经下载，MCP 等待因设备重启断开；继续等待设备上线")
        for _ in range(180):
            await asyncio.sleep(1)
            if job["status"] in ("download_failed", "version_mismatch"):
                return
            if job["status"] == "downloaded":
                session_now = self.sessions.get(job["device_id"])
                if session_now is not None and not getattr(
                        getattr(session_now, "ws", None), "closed", False):
                    actual_version = getattr(session_now, "firmware_version", "?")
                    if actual_version == job["expected_version"]:
                        self._firmware_deployment_update(
                            deployment_id, "completed", 100,
                            "固件已完整下载，设备已重新上线并上报目标版本 {}".format(
                                actual_version))
                        return
                    self._firmware_deployment_update(
                        deployment_id, "version_mismatch", 100,
                        "设备已重新上线，但版本校验失败：期望 {}，实际 {}".format(
                            job["expected_version"], actual_version))
                    return
                self._firmware_deployment_update(
                    deployment_id, "downloaded", 80,
                    "固件已下载，等待设备重启后重新上线")
            elif job["status"] in ("command_sent", "rebooting", "command_accepted"):
                self._firmware_deployment_update(
                    deployment_id, "waiting_download", 35,
                    "等待设备请求固件文件")
        if job["status"] == "downloaded":
            self._firmware_deployment_update(
                deployment_id, "downloaded", 80,
                "固件已下载，但设备尚未重新上线；请检查设备串口日志")
        elif job["status"] != "completed":
            self._firmware_deployment_update(
                deployment_id, "failed", 100, "超时：未观察到固件下载请求")

    async def api_admin_firmware_deploy(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            device_id = self.account_store.normalize_device_id(data.get("device_id"))
            release = self.account_store.get_firmware_release(
                int(request.match_info["release_id"]))
            if release is None:
                raise ValueError("固件版本不存在")
            path = self._firmware_dir / Path(release["stored_name"]).name
            if not path.is_file():
                raise ValueError("固件文件不存在，请重新上传")
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        session = self.sessions.get(device_id)
        if session is None or getattr(getattr(session, "ws", None), "closed", False):
            return web.json_response({"error": "设备不在线，无法下发 OTA"}, status=409)
        token = secrets.token_urlsafe(32)
        deployment_id = secrets.token_hex(12)
        self._firmware_tokens[token] = {
            "release_id": release["id"], "device_id": device_id,
            "deployment_id": deployment_id, "expires_at": time.time() + 15 * 60}
        url = self._firmware_public_base() + "/ota/firmware/" + token
        if session.mcp is None:
            self._firmware_tokens.pop(token, None)
            return web.json_response({"error": "设备 MCP 尚未就绪"}, status=409)
        self._firmware_deployments[deployment_id] = {
            "id": deployment_id, "release_id": release["id"],
            "version": release["version"], "expected_version": release["version"],
            "device_id": device_id,
            "url": url, "status": "queued", "progress": 5,
            "message": "任务已创建", "created_at": time.time(),
            "updated_at": time.time(), "logs": []}
        asyncio.create_task(self._run_firmware_deployment(deployment_id, session))
        self.account_store.audit_admin_action(
            admin["id"], "firmware.deploy", "device", device_id, {
                "release_id": release["id"], "version": release["version"]})
        return web.json_response({
            "accepted": True, "deployment_id": deployment_id,
            "device_id": device_id, "release": self._firmware_json(release),
            "message": "升级任务已创建，请查看进度和日志"}, status=202)

    async def api_admin_firmware_deployments(self, request):
        self._require_admin(request)
        now = time.time()
        for key in list(self._firmware_deployments):
            if now - self._firmware_deployments[key]["updated_at"] > 3600:
                self._firmware_deployments.pop(key, None)
        return web.json_response({"deployments": list(self._firmware_deployments.values())})

    async def api_admin_firmware_deployment_clear(self, request):
        self._require_admin(request)
        deployment_id = request.match_info["deployment_id"]
        job = self._firmware_deployments.get(deployment_id)
        if job is None:
            return web.json_response({"error": "deployment not found"}, status=404)
        job["logs"] = []
        job["message"] = "日志已清除"
        job["updated_at"] = time.time()
        return web.json_response({"success": True})

    async def ota_firmware_download(self, request):
        token = request.match_info["token"]
        entry = self._firmware_tokens.get(token)
        if entry is None or entry["expires_at"] < time.time():
            self._firmware_tokens.pop(token, None)
            raise web.HTTPNotFound(text="firmware download link expired")
        release = self.account_store.get_firmware_release(entry["release_id"])
        if release is None:
            raise web.HTTPNotFound(text="firmware release not found")
        path = self._firmware_dir / Path(release["stored_name"]).name
        if not path.is_file():
            raise web.HTTPNotFound(text="firmware file not found")
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": "attachment; filename=firmware.bin",
            "Content-Length": str(release["size_bytes"]),
            "X-Firmware-Version": release["version"],
            "X-Firmware-SHA256": release["sha256"],
        })
        await response.prepare(request)
        sent = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    await response.write(chunk)
                    sent += len(chunk)
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            self._firmware_deployment_update(
                entry["deployment_id"], "download_failed", 40,
                "设备中断固件传输：已发送 {}/{} 字节".format(
                    sent, release["size_bytes"]))
            raise
        if sent != int(release["size_bytes"]):
            self._firmware_deployment_update(
                entry["deployment_id"], "download_failed", 40,
                "固件传输大小不一致：已发送 {}/{} 字节".format(
                    sent, release["size_bytes"]))
            return response
        self._firmware_deployment_update(
            entry["deployment_id"], "downloaded", 80,
            "服务器已完整发送固件：{}/{} 字节".format(
                sent, release["size_bytes"]))
        return response

    async def api_admin_user_create(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            is_admin = data.get("is_admin", False)
            if not isinstance(is_admin, bool):
                raise ValueError("is_admin 必须是布尔值")
            user = self.account_store.register_user(
                data.get("username"), data.get("password"), is_admin)
            self.account_store.audit_admin_action(
                admin["id"], "user.create", "user", user["id"],
                {"username": user["username"], "is_admin": user["is_admin"]})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(user, status=201)

    async def api_admin_user_update(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            user_id = int(data.pop("user_id"))
            allowed = {key: data[key] for key in ("is_admin", "is_active", "password")
                       if key in data}
            if not allowed:
                raise ValueError("没有可更新的字段")
            for key in ("is_admin", "is_active"):
                if key in allowed and not isinstance(allowed[key], bool):
                    raise ValueError("{} 必须是布尔值".format(key))
            if "password" in allowed and not isinstance(allowed["password"], str):
                raise ValueError("password 必须是字符串")
            user = self.account_store.update_user_for_admin(
                admin["id"], user_id, **allowed)
            audit_details = {key: value for key, value in allowed.items()
                             if key != "password"}
            if "password" in allowed:
                audit_details["password_reset"] = True
            self.account_store.audit_admin_action(
                admin["id"], "user.update", "user", user_id, audit_details)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(user)

    async def api_admin_user_delete(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            user_id = int(data.get("user_id"))
            affected = [item["device_id"] for item in
                        self.account_store.list_devices_owned_by(user_id)]
            self.account_store.delete_user_for_admin(admin["id"], user_id)
            self.account_store.audit_admin_action(
                admin["id"], "user.delete", "user", user_id,
                {"unbound_devices": affected})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        for device_id in affected:
            session = self.sessions.get(device_id)
            if session is not None:
                await session.close()
        return web.json_response({"success": True})

    async def api_admin_device_assign(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            device_id = self.account_store.normalize_device_id(data.get("device_id"))
            device = self.account_store.assign_device_for_admin(
                device_id, data.get("owner_user_id"), data.get("identifier"), data.get("name"))
            self.account_store.audit_admin_action(
                admin["id"], "device.assign", "device", device_id,
                {"owner_user_id": device.get("owner_user_id")})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        session = self.sessions.get(device_id)
        if session is not None:
            await session.close()
        return web.json_response(device)

    async def api_admin_device_update(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            device_id = self.account_store.normalize_device_id(data.get("device_id"))
            device = self.account_store.set_device_active_for_admin(
                device_id, data.get("is_active"))
            self.account_store.audit_admin_action(
                admin["id"], "device.update", "device", device_id,
                {"is_active": device["is_active"]})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not device["is_active"]:
            session = self.sessions.get(device_id)
            if session is not None:
                await session.close()
        return web.json_response(device)

    async def api_admin_device_delete(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            device_id = self.account_store.normalize_device_id(data.get("device_id"))
            self.account_store.delete_device_for_admin(device_id)
            self.account_store.audit_admin_action(
                admin["id"], "device.delete", "device", device_id)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        session = self.sessions.get(device_id)
        if session is not None:
            await session.close()
        return web.json_response({"success": True})

    @staticmethod
    def _validate_environment_url(environment, ota_url):
        environment = str(environment or "").strip().lower()
        ota_url = str(ota_url or "").strip()
        if environment not in ("test", "production"):
            raise ValueError("环境必须是 test 或 production")
        if len(ota_url) > 512:
            raise ValueError("OTA 地址过长")
        parsed = urlparse(ota_url)
        if not parsed.hostname or parsed.path.rstrip("/") != "/ota":
            raise ValueError("OTA 地址必须是以 /ota 结尾的完整地址")
        if environment == "production" and parsed.scheme != "https":
            raise ValueError("生产环境 OTA 地址必须使用 HTTPS")
        if environment == "test":
            if parsed.scheme not in ("http", "https"):
                raise ValueError("测试环境 OTA 地址必须使用 HTTP 或 HTTPS")
        return environment, ota_url

    async def api_admin_device_switch_environment(self, request):
        admin = self._require_admin(request)
        try:
            data = await request.json()
            device_id = self.account_store.normalize_device_id(data.get("device_id"))
            if self.account_store.get_device(device_id) is None:
                raise ValueError("设备不存在")
            environment, ota_url = self._validate_environment_url(
                data.get("environment"), data.get("ota_url"))
            session = self.sessions.get(device_id)
            if session is None or getattr(getattr(session, "ws", None), "closed", False):
                return web.json_response({"error": "设备不在线，无法切换环境"}, status=409)
            await session.send_json({
                "type": "system", "command": "set_ota_url",
                "environment": environment, "ota_url": ota_url,
                "reboot": bool(data.get("reboot", True)),
            })
            self.account_store.audit_admin_action(
                admin["id"], "device.switch_environment", "device", device_id,
                {"environment": environment, "ota_url": ota_url,
                 "reboot": bool(data.get("reboot", True))})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({
            "accepted": True, "device_id": device_id,
            "environment": environment, "rebooting": bool(data.get("reboot", True)),
        }, status=202)

    def _effective_model_settings(self, device_id):
        settings = {
            key: self.config.get("dashscope", {}).get(key)
            for key in ("model", "language", "voice", "instructions",
                        "conversation_timeout_minutes")
        }
        settings["language"] = settings.get("language") or "zh"
        settings["conversation_timeout_minutes"] = (
            settings.get("conversation_timeout_minutes") or 10)
        settings.update(self.account_store.get_model_settings(device_id))
        settings["api_key_configured"] = bool(
            self.config.get("dashscope", {}).get("api_key"))
        return settings

    def _effective_vad_settings(self, device_id):
        settings = dict(self.config.get("vad", {}))
        settings.update(self.account_store.get_vad_settings(device_id))
        return {
            "silence_duration_ms": settings.get("silence_duration_ms", 900),
            "energy_threshold": settings.get("energy_threshold", 120.0),
        }

    def _refresh_active_device_config(self, device_id):
        session = self.sessions.get(device_id)
        if session is None:
            return
        model = self._effective_model_settings(device_id)
        model.pop("api_key_configured", None)
        session.config.setdefault("dashscope", {}).update(model)
        session.config.setdefault("vad", {}).update(self._effective_vad_settings(device_id))
        features = self.account_store.get_device_features(device_id)
        session.config["features"] = features
        owner_id = self.account_store.device_owner_id(device_id)
        if owner_id is not None and features.get("memory_enabled"):
            session.config.setdefault("dashscope", {})["user_memory_prompt"] = (
                self.account_store.memory_prompt(owner_id))
        else:
            session.config.setdefault("dashscope", {}).pop("user_memory_prompt", None)

    def _binding_attempt_keys(self, request, user):
        return ("user:{}".format(user["id"]),
                "ip:{}".format(request.remote or "unknown"))

    def _binding_retry_after(self, request, user):
        now = time.monotonic()
        retry_after = 0
        for key in self._binding_attempt_keys(request, user):
            attempts = self._binding_failures.setdefault(key, deque())
            while attempts and now - attempts[0] >= BINDING_ATTEMPT_WINDOW_SECONDS:
                attempts.popleft()
            if len(attempts) >= BINDING_ATTEMPT_LIMIT:
                retry_after = max(
                    retry_after,
                    int(BINDING_ATTEMPT_WINDOW_SECONDS - (now - attempts[0])) + 1,
                )
        return retry_after

    def _record_binding_failure(self, request, user):
        now = time.monotonic()
        for key in self._binding_attempt_keys(request, user):
            self._binding_failures.setdefault(key, deque()).append(now)

    def _clear_binding_failures(self, request, user):
        for key in self._binding_attempt_keys(request, user):
            self._binding_failures.pop(key, None)

    async def api_me(self, request):
        user = self._require_user(request)
        return web.json_response({"id": user["id"], "username": user["username"]})

    async def api_devices(self, request):
        user = self._require_user(request)
        return web.json_response({"devices": self.account_store.list_user_devices(user["id"])})

    async def api_device_bind(self, request):
        user = self._require_user(request)
        retry_after = self._binding_retry_after(request, user)
        if retry_after:
            return web.json_response(
                {"error": "尝试次数过多，请稍后再试", "retry_after": retry_after},
                status=429, headers={"Retry-After": str(retry_after)})
        try:
            data = await request.json()
            device = self.account_store.bind_device_by_code(
                user["id"], data.get("binding_code"))
        except (ValueError, TypeError):
            self._record_binding_failure(request, user)
            return web.json_response({"error": "绑定码无效或已过期"}, status=400)
        self._clear_binding_failures(request, user)
        # Reconnect so the restricted binding session is replaced by a normal
        # per-user session with this device's effective settings.
        session = self.sessions.get(device["device_id"])
        if session is not None:
            await session.close()
        log.info("device bound: user=%s device=%s identifier=%s",
                 user["username"], device["device_id"], device["identifier"])
        return web.json_response(device)

    async def api_device_update(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
            device = self.account_store.update_device(
                user["id"], data.get("device_id"),
                data.get("identifier"), data.get("name"))
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(device)

    async def api_device_unbind(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
            device_id = self.account_store.normalize_device_id(data.get("device_id"))
            self.account_store.unbind_device(user["id"], device_id)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        session = self.sessions.get(device_id)
        if session is not None:
            await session.close()
        return web.json_response({"success": True})

    async def api_conversations(self, request):
        user = self._require_user(request)
        device_id = request.query.get("device_id", "")
        try:
            conversation_id = request.query.get("conversation_id")
            if conversation_id:
                return web.json_response(
                    self.account_store.get_chat_messages(user["id"], conversation_id))
            conversations = self.account_store.list_chat_sessions(
                user["id"], device_id, request.query.get("limit", 100))
            # Compatibility for API clients on the previous paired-turn
            # contract; the dashboard renders conversations/messages.
            turns = self.account_store.list_conversations(
                user["id"], device_id, request.query.get("limit", 100))
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid limit"}, status=400)
        return web.json_response({"device_id": device_id,
                                  "conversations": conversations,
                                  "turns": turns})

    async def api_conversation_end(self, request):
        self._require_user(request)
        data = await request.json()
        device_id = data.get("device_id", "")
        self._require_owned_device(request, device_id)
        conversation_id = self.account_store.active_conversation_id(device_id)
        session = self.sessions.get(device_id)
        if session:
            pending = bool(session.omni_busy)
            ended_now = await session.request_end_conversation()
            conversation_id = ended_now or conversation_id
        else:
            pending = False
            conversation_id = self.account_store.end_conversation(device_id)
        if conversation_id and self.gateway and not session:
            asyncio.create_task(self.gateway.summarize_conversation(conversation_id))
        return web.json_response({"conversation_id": conversation_id,
                                  "pending": pending})

    async def api_conversation_delete(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
            deleted = self.account_store.soft_delete_conversation(
                user["id"], data.get("conversation_id"))
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self._refresh_user_sessions(user["id"])
        return web.json_response({"deleted": deleted})

    async def api_memories(self, request):
        user = self._require_user(request)
        target_user_id = self._target_user_id(user, request.query.get("user_id"))
        return web.json_response({
            "user_id": target_user_id,
            "memories": self.account_store.list_memories(target_user_id),
        })

    async def api_usage(self, request):
        user = self._require_user(request)
        device_id = request.query.get("device_id") or None
        if device_id and not self.account_store.user_can_access_device(user["id"], device_id):
            raise web.HTTPForbidden(text="无权访问该设备")
        target_user_id = self.account_store.device_owner_id(device_id) if device_id else user["id"]
        if target_user_id is None:
            target_user_id = user["id"]
        if not user.get("is_admin") and target_user_id != user["id"]:
            raise web.HTTPForbidden(text="无权访问该用量")
        by_device = []
        devices = self.account_store.list_devices_owned_by(target_user_id)
        for device in devices:
            by_device.append({
                "device_id": device["device_id"], "name": device["name"],
                "identifier": device["identifier"],
                "usage": self.account_store.usage_summary(target_user_id, device["device_id"]),
            })
        return web.json_response({
            "user_id": target_user_id,
            "usage": self.account_store.usage_summary(target_user_id, device_id),
            "by_device": by_device,
        })

    async def api_memory_create(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
            target_user_id = self._target_user_id(user, data.pop("user_id", None))
            memory = self.account_store.upsert_memory(
                target_user_id, data.get("category"), data.get("label"),
                data.get("value"), data.get("enabled", True))
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self._refresh_user_sessions(target_user_id)
        return web.json_response(memory)

    async def api_memory_update(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
            target_user_id = self._target_user_id(user, data.pop("user_id", None))
            memories = self.account_store.update_memory(
                target_user_id, data.pop("id"), **data)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self._refresh_user_sessions(target_user_id)
        return web.json_response({"memories": memories})

    async def api_memory_delete(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
            target_user_id = self._target_user_id(user, data.pop("user_id", None))
            self.account_store.delete_memory(target_user_id, data.get("id"))
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self._refresh_user_sessions(target_user_id)
        return web.json_response({"success": True})

    async def api_memory_summarize(self, request):
        user = self._require_user(request)
        if not self.gateway:
            return web.json_response({"error": "memory service unavailable"}, status=503)
        try:
            data = await request.json()
            conversation_id = int(data.get("conversation_id"))
            self.account_store.get_chat_messages(user["id"], conversation_id)
            saved = await self.gateway.summarize_conversation(conversation_id)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"memories": saved})

    async def api_features_get(self, request):
        device_id = request.query.get("device_id", "")
        self._require_owned_device(request, device_id)
        return web.json_response(self.account_store.get_device_features(device_id))

    async def api_features_set(self, request):
        user = self._require_user(request)
        data = await request.json()
        device_id = data.get("device_id", "")
        try:
            features = self.account_store.set_device_features(
                user["id"], device_id, data)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        self._refresh_active_device_config(device_id)
        session = self.sessions.get(device_id)
        if session:
            await session.send_json({"type": "system", "command": "conversation_config",
                "automatic_interrupt": features["automatic_interrupt"],
                "button_interrupt": features["button_interrupt"],
                "double_click_end": features["double_click_end"]})
        return web.json_response(features)

    def _refresh_user_sessions(self, user_id):
        for device in self.account_store.list_devices_owned_by(user_id):
            self._refresh_active_device_config(device["device_id"])

    async def api_status(self, request):
        user = self._require_user(request)
        now = time.time()
        devices = []
        for device in self.account_store.list_user_devices(user["id"]):
            device_id = device["device_id"]
            s = self.sessions.get(device_id)
            online = s is not None and not getattr(getattr(s, "ws", None), "closed", False)
            last_seen = device.get("last_seen") or now
            devices.append({
                "device_id": device_id,
                "identifier": device["identifier"],
                "name": device["name"],
                "session_id": s.session_id if online else "",
                "bin_version": getattr(s, "bin_version", "?") if online else "?",
                "firmware_version": getattr(s, "firmware_version", "?") if online else "?",
                "online": online,
                "idle": not online,
                "listening": getattr(s, "listening", False) if online else False,
                "speaking": getattr(s, "speaking", False) if online else False,
                "omni_busy": getattr(s, "omni_busy", False) if online else False,
                "connected_at": s.connected_at if online else last_seen,
                "connected_for": now - (s.connected_at if online else last_seen),
            })

        cfg = self.config
        # OTA 端点从 public_ws_url 推导: ws://host:port/ws -> http://host:port/ota
        base = cfg["server"].get("public_ws_url", "").replace("ws://", "http://").replace("wss://", "https://")
        if base.endswith("/ws"):
            base = base[:-3]
        return web.json_response({
            "health": {"status": "ok", "time": time.time()},
            "server": {"uptime": now - self.start_time,
                       "environment": cfg.get("environment", "production")},
            "user": user,
            "devices": devices,
            "ota_requests": [device["device_id"] for device in devices],
            "config": {
                "ota_url": base.rstrip("/") + "/ota",
                "ws_url": cfg["server"].get("public_ws_url", ""),
                "model_base": cfg["dashscope"].get("realtime_url", ""),
                "api_key_configured": bool(cfg["dashscope"].get("api_key")),
                "devices_enabled": bool(cfg.get("devices", {}).get("enabled")),
                "output_sample_rate": cfg["dashscope"].get("output_sample_rate", 24000),
            },
        })

    async def api_vad_get(self, request):
        device_id = request.query.get("device_id", "")
        self._require_owned_device(request, device_id)
        return web.json_response(self._effective_vad_settings(device_id))

    async def api_vad_set(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        device_id = data.get("device_id", "")
        self._require_owned_device(request, device_id)
        try:
            vad = {
                "silence_duration_ms": max(200, min(6000, int(data["silence_duration_ms"]))),
                "energy_threshold": max(1, min(30000, float(data["energy_threshold"]))),
            }
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "invalid VAD settings"}, status=400)
        self.account_store.set_vad_settings(user["id"], device_id, vad)
        self._refresh_active_device_config(device_id)
        log.info("VAD 配置更新: user=%s device=%s settings=%s",
                 user["username"], device_id, vad)
        return web.json_response(vad)

    async def api_model_get(self, request):
        device_id = request.query.get("device_id", "")
        self._require_owned_device(request, device_id)
        return web.json_response(self._effective_model_settings(device_id))

    async def api_model_set(self, request):
        user = self._require_user(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        device_id = data.get("device_id", "")
        self._require_owned_device(request, device_id)
        model = (data.get("model") or "").strip()
        language = data.get("language", "")
        voice = (data.get("voice") or "").strip()
        instructions = (data.get("instructions") or "").strip()
        if not model or len(model) > 128:
            return web.json_response({"error": "invalid model"}, status=400)
        if language not in MODEL_LANGUAGE_CODES:
            return web.json_response({"error": "unsupported language"}, status=400)
        if not voice or len(voice) > 128 or len(instructions) > 12000:
            return web.json_response({"error": "invalid voice or instructions"}, status=400)
        try:
            minutes = float(data.get("conversation_timeout_minutes", 10))
        except (TypeError, ValueError):
            return web.json_response({"error": "conversation_timeout_minutes must be a number"}, status=400)
        if not math.isfinite(minutes) or not 1 <= minutes <= 120:
            return web.json_response(
                {"error": "conversation_timeout_minutes must be between 1 and 120"}, status=400)
        settings = {
            "model": model,
            "language": language,
            "voice": voice,
            "instructions": instructions,
            "conversation_timeout_minutes": minutes,
        }
        self.account_store.set_model_settings(user["id"], device_id, settings)
        self._refresh_active_device_config(device_id)
        log.info("设备模型配置更新: user=%s device=%s model=%s language=%s voice=%s",
                 user["username"], device_id, model, language, voice)
        return web.json_response(self._effective_model_settings(device_id))

    @staticmethod
    def _testable_tools(session):
        """Only expose actuator tools intended for supervised bench tests."""
        mcp = getattr(session, "mcp", None)
        tools = getattr(mcp, "tools", []) if mcp else []
        # Keep manual hardware tests limited to bounded, observable tools.
        # Destructive/configuration tools (firmware upgrade, screen snapshot
        # upload, asset download, etc.) are intentionally not exposed here.
        allowed_prefixes = ("self.chassis.", "self.gimbal.",
                            "self.servo.", "self.face_tracking.", "self.led.")
        allowed_names = {
            "self.camera.take_photo",
            "self.camera.face_detect_local",
            "self.screen.get_info",
            "self.screen.set_brightness",
            "self.screen.set_theme",
        }
        result = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            if not isinstance(name, str) or (not name.startswith(allowed_prefixes) and name not in allowed_names):
                continue
            result.append({
                "name": name,
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
            })
        return result

    def _get_test_session(self, device_id):
        if not isinstance(device_id, str) or not device_id:
            return None
        return self.sessions.get(device_id)

    async def api_camera_upload(self, request):
        """Accept one JPEG from the device's current MCP camera session."""
        device_id = request.headers.get("Device-Id", "")
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        session = self._get_test_session(device_id)
        expected = getattr(session, "camera_upload_token", "") if session else ""
        if not token or not expected or not hmac.compare_digest(token, expected):
            raise web.HTTPUnauthorized(text="invalid camera upload token")
        if not request.content_type.startswith("multipart/"):
            log.warning("camera upload rejected: device=%s content_type=%s", device_id, request.content_type)
            return web.json_response({"error": "multipart JPEG upload required"}, status=400)

        image = bytearray()
        try:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name != "file":
                    await part.release()
                    continue
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    if len(image) + len(chunk) > MAX_CAMERA_PHOTO_BYTES:
                        return web.json_response({"error": "photo exceeds 2 MiB limit"}, status=413)
                    image.extend(chunk)
        except Exception:
            log.exception("camera upload parse failed: device=%s", device_id)
            return web.json_response({"error": "invalid camera upload"}, status=400)

        if len(image) < 4 or image[:2] != b"\xff\xd8" or image[-2:] != b"\xff\xd9":
            log.warning("camera upload rejected: device=%s bytes=%d head=%s tail=%s",
                        device_id, len(image), bytes(image[:4]).hex(), bytes(image[-4:]).hex())
            return web.json_response({"error": "camera did not send a JPEG"}, status=400)

        photo = {
            "data": bytes(image),
            "created_at": time.time(),
            "nonce": secrets.token_urlsafe(8),
        }
        self._camera_photos[device_id] = photo
        log.info("camera photo stored in memory: device=%s bytes=%d", device_id, len(image))
        return web.json_response({
            "success": True,
            "result": "Photo captured and available on the dashboard.",
            "image_url": "/api/camera/latest?device_id={}&v={}".format(device_id, photo["nonce"]),
        })

    async def api_camera_latest(self, request):
        """Serve a dashboard-authenticated, in-memory latest camera photo."""
        device_id = request.query.get("device_id", "")
        self._require_owned_device(request, device_id)
        photo = self._camera_photos.get(device_id)
        if photo is None:
            raise web.HTTPNotFound(text="no camera photo for this device")
        return web.Response(
            body=photo["data"],
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    async def api_test_tools(self, request):
        device_id = request.query.get("device_id", "")
        self._require_owned_device(request, device_id)
        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        return web.json_response({
            "device_id": device_id,
            "tools": self._testable_tools(session),
        })

    async def api_test_face(self, request):
        """Run one authenticated conversational face operation manually."""
        self._require_user(request)
        try:
            data = await request.json()
            device_id = data.get("device_id", "")
            operation = data.get("operation", "")
            arguments = data.get("arguments") or {}
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        self._require_owned_device(request, device_id)
        allowed = {"list", "register_current", "recognize_current", "delete", "update"}
        if operation not in allowed or not isinstance(arguments, dict):
            return web.json_response({"error": "invalid face operation"}, status=400)
        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        result = await session._handle_face_tool(
            "server.face." + operation, arguments)
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            pass
        return web.json_response({"device_id": device_id, "result": result})

    async def api_test_mcp(self, request):
        """Send one supervised bench-test MCP command to an online device."""
        self._require_user(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)

        device_id = data.get("device_id", "")
        self._require_owned_device(request, device_id)
        name = data.get("name", "")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            return web.json_response({"error": "arguments must be an object"}, status=400)
        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        testable_names = {tool["name"] for tool in self._testable_tools(session)}
        if name not in testable_names:
            return web.json_response({"error": "tool is not available for supervised testing"}, status=403)

        timeout_ms = data.get("timeout_ms", 8000)
        try:
            timeout_ms = max(500, min(15000, int(timeout_ms)))
        except (TypeError, ValueError):
            return web.json_response({"error": "timeout_ms must be an integer"}, status=400)
        try:
            result = await session.mcp.call_tool(name, arguments, timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            log.warning("MCP bench test timed out: device=%s tool=%s", device_id, name)
            return web.json_response({"error": "device tool call timed out"}, status=504)
        except Exception as exc:
            log.exception("MCP bench test failed: device=%s tool=%s", device_id, name)
            return web.json_response({"error": "device tool call failed", "detail": str(exc)}, status=502)

        log.info("MCP bench test completed: device=%s tool=%s", device_id, name)
        return web.json_response({"device_id": device_id, "name": name, "result": result})

    async def api_logs(self, request):
        user = self._require_user(request)
        owned_ids = {
            device["device_id"].casefold()
            for device in self.account_store.list_user_devices(user["id"])
        }

        def visible(entry):
            text = entry.get("text", "").casefold()
            return any(device_id in text for device_id in owned_ids)

        # 先推快照（最近的日志），再持续推送新增
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)

        all_snapshot = self.log_handler.snapshot()
        snapshot = [entry for entry in all_snapshot if visible(entry)]
        for entry in snapshot:
            try:
                await resp.write(("data: " + json.dumps(entry, ensure_ascii=False) + "\n\n").encode("utf-8"))
            except (ConnectionError, RuntimeError):
                return resp

        evt = self.log_handler._evt
        last_sequence = all_snapshot[-1]["id"] if all_snapshot else 0
        try:
            while True:
                # 检查是否有新日志
                all_new_entries = self.log_handler.events_after(last_sequence)
                new_entries = [entry for entry in all_new_entries if visible(entry)]
                if new_entries:
                    for entry in new_entries:
                        await resp.write(("data: " + json.dumps(entry, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    last_sequence = all_new_entries[-1]["id"]
                elif all_new_entries:
                    last_sequence = all_new_entries[-1]["id"]
                else:
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=15)
                        evt.clear()
                    except asyncio.TimeoutError:
                        await resp.write(b": keepalive\n\n")
                    except (asyncio.CancelledError, ConnectionError, RuntimeError):
                        break
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp
