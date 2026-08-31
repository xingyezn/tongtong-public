"""监控面板：设备状态 / 配置查看 / 实时日志 / 音频回显测试。

路由：
  GET  /            单页监控面板（内嵌 HTML）
  GET  /api/status  设备在线状态 + 后端配置摘要
  GET  /api/logs    实时日志流（SSE）
  GET  /ws/echo     音频回显测试（浏览器录音 -> 服务器原样回放）
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque

from aiohttp import web, WSMsgType

log = logging.getLogger("dash")

# 登录 cookie 名
AUTH_COOKIE = "tongtong_auth"


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
</style>
</head>
<body>
<div class="box">
  <p class="eyebrow">TONGTONG · CONTROL CENTER</p>
  <h1>童童监控中心</h1>
  <p class="sub">欢迎回来，连接你的语音助手</p>
  <!--ERROR-->
  <form method="post" action="/login">
    <input type="password" name="password" placeholder="访问口令" autofocus required>
    <button type="submit">登 录</button>
  </form>
</div>
</body>
</html>
"""


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
  .muted { color:var(--muted); font-size:12px; }
  .empty { color:var(--muted); font-size:13px; padding:8px 0; }
  .hint { font-size:12px; color:var(--muted); margin-top:8px; }
  .badge { padding:3px 9px; border-radius:20px; color:#527087; font-size:11px; background:#e8f3f8; }
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
  @media (max-width:760px) { header { align-items:flex-start; } .header-spacer { display:none; } main { grid-template-columns:1fr; padding-top:18px; }
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
  <label class="muted"><input type="checkbox" id="autorefresh" checked> 自动刷新</label>
  <button class="btn" onclick="refresh()">刷新</button>
</header>

<main>
  <div class="card" id="card-devices">
    <h2>设备状态</h2>
    <table>
      <thead><tr><th>设备</th><th>版本</th><th>状态</th><th>在线/最后活跃</th><th>Session</th></tr></thead>
      <tbody id="device-body"><tr><td colspan="5" class="empty">加载中…</td></tr></tbody>
    </table>
    <div class="hint" id="ota-hint"></div>
    <div class="hint">说明：设备待机时按省电设计断开连接（显示"待机中"），唤醒对话时自动上线。</div>
  </div>

  <div class="card">
    <h2>后端配置</h2>
    <dl class="kv" id="cfg-kv"></dl>
  </div>

  <div class="card">
    <h2>调试测试模式</h2>
    <div class="row">
      <select id="debug-device" onchange="updateDebugModePanel()" aria-label="选择设备"></select>
      <button class="btn warn" id="debug-mode-btn" onclick="toggleDebugMode()" disabled>启用调试模式</button>
    </div>
    <div class="hint" id="debug-mode-status">等待设备上线…</div>
    <div class="hint">启用后会持久化到设备：软件麦克风采集和唤醒词被关闭，设备保持测试 WebSocket 在线。仅在此模式下可执行直连 MCP 台架测试。测试电机前请先让车轮悬空。</div>
  </div>

  <div class="card">
    <h2>语音检测 (VAD) 控制</h2>
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
    <h2>模型 / 音色 / 人物设定</h2>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">模型</label>
      <input type="text" id="cfg-model" placeholder="qwen3.5-omni-flash-realtime" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
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
        <option value="custom">自定义（填下方输入框）</option>
      </select>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">自定义音色</label>
      <input type="text" id="cfg-voice-custom" placeholder="音色 ID，如 my_voice_01" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px;align-self:flex-start">人物设定</label>
      <textarea id="cfg-instructions" rows="4" placeholder="你是童童，一个友好、热情的语音助手……" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4;resize:vertical;font-family:Consolas,monospace;font-size:12px"></textarea>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">Workspace ID</label>
      <input type="text" id="cfg-workspace" placeholder="llm-xxxx" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">输入/输出采样率</label>
      <input type="number" id="cfg-in-rate" min="8000" max="48000" step="8000" value="16000" style="flex:1;max-width:140px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
      <input type="number" id="cfg-out-rate" min="8000" max="48000" step="8000" value="24000" style="flex:1;max-width:140px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row">
      <button class="btn" id="save-model-btn" onclick="saveModel()">保存模型 / 音色 / 设定</button>
      <span class="muted" id="model-status"></span>
    </div>
    <div class="hint">
      修改后<b>下一轮对话生效</b>，并持久化保存（重启仍生效）。
      模型需为百炼 Realtime 系列（如 qwen3.5-omni-flash-realtime / qwen3.5-omni-plus-realtime）。
    </div>
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

  <div class="card full">
    <h2>音频回显测试</h2>
    <div class="row">
      <button class="btn" id="rec-btn" onclick="toggleRec()">开始录音</button>
      <span class="muted" id="rec-status">未录音</span>
      <button class="btn" onclick="sendAudio()" id="send-btn" disabled>发送到服务器</button>
      <button class="btn" onclick="playEcho()" id="play-btn" disabled>回放回显</button>
    </div>
    <div class="hint">
      浏览器录音 → WebSocket 发到 <span class="badge">/ws/echo</span> → 服务器原样回传 →
      浏览器播放。用于验证「网络 → 后端 → 回程」音频通路（不经过设备协议）。
    </div>
  </div>
</main>
<div id="toast" role="status" aria-live="polite"></div>

<script>
const $ = id => document.getElementById(id);
let autoRefresh = true;
let dashboardDevices = [];

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

  // devices
  const tbody = $("device-body");
  if (!d.devices.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">当前无设备在线</td></tr>';
  } else {
    tbody.innerHTML = d.devices.map(dev => {
      let st;
      if (dev.online) {
        st = '<span class="tag online">在线</span> ' +
          (dev.listening ? '<span class="tag listen">聆听中</span>' : "") +
          (dev.speaking ? '<span class="tag listen">播放中</span>' : "") +
          (dev.debug_mode ? '<span class="tag debug">调试</span>' : "");
      } else if (dev.idle) {
        st = '<span class="tag offline">待机中</span>';
      } else {
        st = '<span class="tag offline">未连接</span>';
      }
      const timeStr = dev.online ? fmtDur(dev.connected_for)
        : (dev.idle ? "最后活跃 " + fmtDur(dev.connected_for) + " 前" : "—");
      return `<tr>
        <td class="mono">${dev.device_id}</td>
        <td>v${dev.bin_version}</td>
        <td>${st}</td>
        <td>${timeStr}</td>
        <td class="mono">${dev.session_id || "—"}</td>
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
    ["AI 模型", c.model + " @ " + c.model_base],
    ["API Key", c.api_key_configured ? '<span style="color:var(--ok)">已配置</span>'
                                     : '<span style="color:var(--warn)">未配置 (回环模式)</span>'],
    ["设备鉴权", c.devices_enabled ? "开启" : "关闭"],
    ["输出采样率", c.output_sample_rate + " Hz"],
  ].map(([k,v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  renderDebugModeControls(d.devices || []);
}

function renderDebugModeControls(devices) {
  const select = $("debug-device");
  const previous = select.value;
  dashboardDevices = devices.filter(dev => dev.online);
  select.innerHTML = dashboardDevices.map(dev =>
    `<option value="${dev.device_id}">${dev.device_id} (v${dev.bin_version})</option>`
  ).join("");
  select.disabled = dashboardDevices.length === 0;
  if (dashboardDevices.some(dev => dev.device_id === previous)) select.value = previous;
  updateDebugModePanel();
}

function updateDebugModePanel() {
  const select = $("debug-device");
  const device = dashboardDevices.find(dev => dev.device_id === select.value);
  const button = $("debug-mode-btn");
  const status = $("debug-mode-status");
  if (!device) {
    button.disabled = true;
    button.textContent = "启用调试模式";
    status.textContent = "没有在线设备。先按住板载触摸键建立一次连接。";
    return;
  }
  button.disabled = false;
  button.textContent = device.debug_mode ? "退出调试模式" : "启用调试模式";
  status.textContent = device.debug_mode
    ? "已启用：语音输入被禁用，测试连接保持在线。"
    : "未启用：可先正常连接设备，再点击启用。";
}

async function toggleDebugMode() {
  const select = $("debug-device");
  const device = dashboardDevices.find(dev => dev.device_id === select.value);
  if (!device) return;
  const enabled = !device.debug_mode;
  const button = $("debug-mode-btn");
  const normalText = button.textContent;
  button.disabled = true;
  button.textContent = enabled ? "启用中…" : "退出中…";
  try {
    const r = await fetch("/api/test/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: device.device_id, enabled }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "request failed");
    device.debug_mode = data.debug_mode;
    updateDebugModePanel();
    showToast(data.debug_mode ? "调试模式已启用" : "已恢复正常语音模式", "ok");
    refresh();
  } catch (e) {
    button.disabled = false;
    button.textContent = normalText;
    showToast("切换失败：" + e.message, "err");
  }
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

// ---- 音频回显测试 ----
let mediaRec = null, chunks = [], recActive = false;
async function toggleRec() {
  if (recActive) {
    mediaRec.stop();
    return;
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  chunks = [];
  mediaRec = new MediaRecorder(stream);
  mediaRec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  mediaRec.onstop = () => {
    recActive = false;
    $("rec-btn").textContent = "开始录音";
    $("rec-status").textContent = "已录 " + (chunks.length ? (chunks[0].size/1024).toFixed(0) : 0) + "KB 音频";
    $("send-btn").disabled = chunks.length === 0;
    stream.getTracks().forEach(t => t.stop());
  };
  mediaRec.start();
  recActive = true;
  $("rec-btn").textContent = "停止录音";
  $("rec-status").textContent = "录音中…";
}
let lastEcho = null;
function sendAudio() {
  if (!chunks.length) return;
  const blob = new Blob(chunks, { type: "audio/webm" });
  $("send-btn").disabled = true;
  $("rec-status").textContent = "发送中…";
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/echo");
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(blob);
  ws.onmessage = e => {
    lastEcho = new Blob([e.data], { type: blob.type });
    ws.close();
    $("rec-status").textContent = "已收到回显 " + (lastEcho.size/1024).toFixed(0) + "KB";
    $("play-btn").disabled = false;
  };
  ws.onerror = () => { $("rec-status").textContent = "发送失败"; $("send-btn").disabled = false; };
}
function playEcho() {
  if (!lastEcho) return;
  const url = URL.createObjectURL(lastEcho);
  const a = new Audio(url);
  a.onended = () => URL.revokeObjectURL(url);
  a.play();
}

// ---- VAD 控制 ----
async function loadVad() {
  try {
    const r = await fetch("/api/vad");
    const d = await r.json();
    $("vad-silence").value = d.silence_duration_ms;
    $("vad-threshold").value = d.energy_threshold;
  } catch (e) {}
}
async function saveVad() {
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
  const body = { silence_duration_ms: silence, energy_threshold: threshold };
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
const VOICE_IDS = ["Tina","Cindy","Liora Mira","Sunnybobi","Raymond","Ethan","Theo Calm",
                   "Serena","Harvey","Maia","Evan","Qiao","Momo","Wil","Angel","Li Cassian",
                   "Mia","Joyner","Gold","Katerina","Ryan","Jennifer","Aiden","Mione","Sunny",
                   "Dylan","Eric","Peter","Joseph Chen","Marcus","Li","Kiki","Rocky","Sohee",
                   "Lenn","Ono Anna","Sonrisa","Bodega","Emilien","Andre","Radio Gol","Alek",
                   "Rizky","Roya","Arda","Hana","Dolce","Jakub","Griet","Eliška","Marina",
                   "Siiri","Ingrid","Sigga","Bea","Chloe"];
async function loadModel() {
  try {
    const r = await fetch("/api/model");
    const d = await r.json();
    $("cfg-model").value = d.model || "";
    $("cfg-instructions").value = d.instructions || "";
    $("cfg-workspace").value = d.workspace_id || "";
    $("cfg-in-rate").value = d.input_sample_rate || 16000;
    $("cfg-out-rate").value = d.output_sample_rate || 24000;
    const voice = d.voice || "";
    if (VOICE_IDS.includes(voice)) {
      $("cfg-voice").value = voice;
      $("cfg-voice-custom").value = "";
    } else {
      $("cfg-voice").value = "custom";
      $("cfg-voice-custom").value = voice;
    }
  } catch (e) {}
}
async function saveModel() {
  let voice = $("cfg-voice").value;
  if (voice === "custom") {
    voice = $("cfg-voice-custom").value.trim();
    if (!voice) {
      $("model-status").textContent = "自定义音色不能为空";
      $("model-status").style.color = "var(--bad)";
      return;
    }
  }
  const body = {
    model: $("cfg-model").value.trim(),
    voice: voice,
    instructions: $("cfg-instructions").value.trim(),
    workspace_id: $("cfg-workspace").value.trim(),
    input_sample_rate: parseInt($("cfg-in-rate").value, 10),
    output_sample_rate: parseInt($("cfg-out-rate").value, 10),
  };
  if (!body.model) {
    $("model-status").textContent = "模型不能为空";
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
    $("model-status").textContent = "已保存: " + d.model + " / " + d.voice;
    $("model-status").style.color = "var(--ok)";
    showToast("模型设置已保存 · " + d.model + " / " + d.voice, "ok");
  } catch (e) {
    $("model-status").textContent = "保存失败";
    $("model-status").style.color = "var(--bad)";
    showToast("保存失败，请检查服务连接后重试", "err");
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = normalText;
  }
}

// ---- auto refresh ----
$("autorefresh").addEventListener("change", e => { autoRefresh = e.target.checked; });
setInterval(() => { if (autoRefresh) refresh(); }, 3000);
refresh();
loadVad();
loadModel();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Dashboard 路由
# ---------------------------------------------------------------------------
class Dashboard:
    def __init__(self, config: dict, sessions: dict, http_api, log_handler: BroadcastLogHandler,
                 device_history: dict = None, save_config: callable = None):
        self.config = config
        self.sessions = sessions  # device_id -> Session
        self.device_history = device_history or {}  # device_id -> {last_seen, client_id}
        self.http_api = http_api
        self.log_handler = log_handler
        self.save_config = save_config
        self.start_time = time.time()
        dash_cfg = config.get("dashboard", {})
        self.password = dash_cfg.get("password", "")
        self.session_ttl = int(dash_cfg.get("session_ttl", 86400))
        self._secret = secrets.token_hex(16)

    def add_routes(self, app: web.Application):
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_get("/logout", self.logout)
        app.router.add_get("/api/status", self.api_status)
        app.router.add_get("/api/logs", self.api_logs)
        app.router.add_get("/ws/echo", self.echo)
        app.router.add_get("/api/vad", self.api_vad_get)
        app.router.add_post("/api/vad", self.api_vad_set)
        app.router.add_get("/api/model", self.api_model_get)
        app.router.add_post("/api/model", self.api_model_set)
        app.router.add_get("/api/test/tools", self.api_test_tools)
        app.router.add_post("/api/test/mcp", self.api_test_mcp)
        app.router.add_post("/api/test/conversation", self.api_test_conversation)
        app.router.add_post("/api/test/mode", self.api_test_mode)

    # ------------------------------------------------------------------
    # 鉴权辅助
    # ------------------------------------------------------------------
    @property
    def auth_enabled(self) -> bool:
        return bool(self.password)

    def _sign(self, value: str) -> str:
        return hmac.new(self._secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def _make_cookie(self, now: float) -> str:
        # token = expires.ts.sign(expires.ts)
        payload = str(int(now) + self.session_ttl)
        return "{}.{}".format(payload, self._sign(payload))

    def _check_cookie(self, request) -> bool:
        if not self.auth_enabled:
            return True
        cookie = request.cookies.get(AUTH_COOKIE, "")
        if not cookie or "." not in cookie:
            return False
        payload, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(payload)):
            return False
        try:
            expires = int(payload)
        except ValueError:
            return False
        return expires > time.time()

    def _redirect_login(self):
        raise web.HTTPFound("/login")

    def _require_auth(self, request):
        if not self._check_cookie(request):
            self._redirect_login()

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def login_page(self, request):
        if self._check_cookie(request):
            raise web.HTTPFound("/")
        return web.Response(text=LOGIN_HTML, content_type="text/html", charset="utf-8")

    async def login(self, request):
        data = await request.post()
        password = data.get("password", "")
        if not self.auth_enabled:
            raise web.HTTPFound("/")
        if password != self.password:
            log.warning("dashboard 登录失败 (ip=%s)", request.remote)
            return web.Response(text=LOGIN_HTML.replace(
                "<!--ERROR-->",
                '<div class="error">密码错误</div>'
            ), content_type="text/html", charset="utf-8")
        log.info("dashboard 登录成功 (ip=%s)", request.remote)
        resp = web.HTTPFound("/")
        resp.set_cookie(AUTH_COOKIE, self._make_cookie(time.time()),
                        max_age=self.session_ttl, httponly=True, samesite="Lax")
        raise resp

    async def logout(self, request):
        resp = web.HTTPFound("/login")
        resp.del_cookie(AUTH_COOKIE)
        raise resp

    def add_routes_public(self, app):  # no-op, 保留接口
        pass

    async def index(self, request):
        if not self._check_cookie(request):
            raise web.HTTPFound("/login")
        return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")

    async def api_status(self, request):
        if not self._check_cookie(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        now = time.time()
        devices = []
        # 在线设备（WS 连接中）
        for device_id, s in list(self.sessions.items()):
            devices.append({
                "device_id": device_id,
                "session_id": s.session_id,
                "bin_version": getattr(s, "bin_version", "?"),
                "online": True,
                "idle": False,
                "listening": getattr(s, "listening", False),
                "speaking": getattr(s, "speaking", False),
                "omni_busy": getattr(s, "omni_busy", False),
                "debug_mode": getattr(s, "debug_mode", False),
                "connected_at": s.connected_at,
                "connected_for": now - s.connected_at,
            })
        # 待机设备（曾连接过，当前断开，省电待机）
        online_ids = {d["device_id"] for d in devices}
        for device_id, info in self.device_history.items():
            if device_id in online_ids:
                continue
            devices.append({
                "device_id": device_id,
                "session_id": "",
                "bin_version": "?",
                "online": False,
                "idle": True,
                "listening": False,
                "speaking": False,
                "omni_busy": False,
                "debug_mode": False,
                "connected_at": info.get("last_seen", 0),
                "connected_for": now - info.get("last_seen", now),
            })

        cfg = self.config
        # OTA 端点从 public_ws_url 推导: ws://host:port/ws -> http://host:port/ota
        base = cfg["server"].get("public_ws_url", "").replace("ws://", "http://").replace("wss://", "https://")
        if base.endswith("/ws"):
            base = base[:-3]
        return web.json_response({
            "health": {"status": "ok", "time": time.time()},
            "server": {"uptime": now - self.start_time},
            "devices": devices,
            "ota_requests": list(getattr(self.http_api, "device_tokens", {}).keys()),
            "config": {
                "ota_url": base.rstrip("/") + "/ota",
                "ws_url": cfg["server"].get("public_ws_url", ""),
                "model": cfg["dashscope"].get("model", ""),
                "model_base": cfg["dashscope"].get("realtime_url", ""),
                "api_key_configured": bool(cfg["dashscope"].get("api_key")),
                "devices_enabled": bool(cfg.get("devices", {}).get("enabled")),
                "output_sample_rate": cfg["dashscope"].get("output_sample_rate", 24000),
            },
        })

    async def api_vad_get(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        vad = self.config.get("vad", {})
        return web.json_response({
            "silence_duration_ms": vad.get("silence_duration_ms", 900),
            "energy_threshold": vad.get("energy_threshold", 120.0),
        })

    async def api_vad_set(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        vad = self.config.setdefault("vad", {})
        if "silence_duration_ms" in data:
            v = int(data["silence_duration_ms"])
            vad["silence_duration_ms"] = max(200, min(6000, v))
        if "energy_threshold" in data:
            v = float(data["energy_threshold"])
            vad["energy_threshold"] = max(1, min(30000, v))
        log.info("VAD 配置更新: %s", vad)
        return web.json_response({
            "silence_duration_ms": vad.get("silence_duration_ms", 900),
            "energy_threshold": vad.get("energy_threshold", 120.0),
        })

    async def api_model_get(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        ds = self.config["dashscope"]
        return web.json_response({
            "model": ds.get("model", ""),
            "voice": ds.get("voice", ""),
            "instructions": ds.get("instructions", ""),
            "workspace_id": ds.get("workspace_id", ""),
            "realtime_url": ds.get("realtime_url", ""),
            "input_sample_rate": ds.get("input_sample_rate", 16000),
            "output_sample_rate": ds.get("output_sample_rate", 24000),
            "api_key_configured": bool(ds.get("api_key")),
        })

    async def api_model_set(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        ds = self.config["dashscope"]
        if "model" in data and isinstance(data["model"], str):
            ds["model"] = data["model"].strip()
        if "voice" in data and isinstance(data["voice"], str):
            ds["voice"] = data["voice"].strip()
        if "instructions" in data and isinstance(data["instructions"], str):
            ds["instructions"] = data["instructions"].strip()
        if "workspace_id" in data and isinstance(data["workspace_id"], str):
            ds["workspace_id"] = data["workspace_id"].strip()
        if "realtime_url" in data and isinstance(data["realtime_url"], str):
            ds["realtime_url"] = data["realtime_url"].strip()
        if "input_sample_rate" in data:
            ds["input_sample_rate"] = int(data["input_sample_rate"])
        if "output_sample_rate" in data:
            ds["output_sample_rate"] = int(data["output_sample_rate"])
        # 持久化到 config.yaml（重启仍生效）
        if self.save_config:
            try:
                self.save_config(self.config)
                log.info("模型/音色/人物设定配置已持久化")
            except Exception as e:
                log.warning("配置持久化失败: %s", e)
        log.info("模型/音色配置更新: model=%s voice=%s", ds.get("model"), ds.get("voice"))
        return web.json_response({
            "model": ds.get("model", ""),
            "voice": ds.get("voice", ""),
            "workspace_id": ds.get("workspace_id", ""),
            "realtime_url": ds.get("realtime_url", ""),
            "api_key_configured": bool(ds.get("api_key")),
        })

    @staticmethod
    def _testable_tools(session):
        """Only expose actuator tools intended for supervised bench tests."""
        mcp = getattr(session, "mcp", None)
        tools = getattr(mcp, "tools", []) if mcp else []
        allowed_prefixes = ("self.chassis.", "self.lamp.")
        result = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            if not isinstance(name, str) or not name.startswith(allowed_prefixes):
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

    async def api_test_tools(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        device_id = request.query.get("device_id", "")
        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        return web.json_response({
            "device_id": device_id,
            "tools": self._testable_tools(session),
        })

    async def api_test_mcp(self, request):
        """Send one supervised bench-test MCP command to an online device."""
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)

        device_id = data.get("device_id", "")
        name = data.get("name", "")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            return web.json_response({"error": "arguments must be an object"}, status=400)
        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        if not getattr(session, "debug_mode", False):
            return web.json_response({"error": "enable device debug mode before running bench commands"}, status=409)
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

    async def api_test_conversation(self, request):
        """Run one virtual-microphone conversation through the actual model.

        The audio is injected only at the ESP32 input boundary.  From there it
        follows the ordinary device Opus uplink, server VAD, Omni inference,
        MCP tool selection, device execution and JSON-RPC callback path.
        """
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        device_id = data.get("device_id", "")
        encoded_pcm = data.get("pcm_b64", "")
        if not isinstance(encoded_pcm, str):
            return web.json_response({"error": "pcm_b64 must be a base64 string"}, status=400)
        try:
            pcm = base64.b64decode(encoded_pcm, validate=True)
        except Exception:
            return web.json_response({"error": "pcm_b64 is not valid base64"}, status=400)
        # 16 kHz, mono, signed 16-bit; keep virtual audio bounded to 8 seconds.
        if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 8:
            return web.json_response({"error": "PCM must be 16 kHz mono s16le and at most 8 seconds"}, status=400)

        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        if not getattr(session, "debug_mode", False):
            return web.json_response({"error": "enable device debug mode before running voice tests"}, status=409)

        all_safe_tools = {tool["name"] for tool in self._testable_tools(session)}
        allow_motion = data.get("allow_motion", False)
        if not isinstance(allow_motion, bool):
            return web.json_response({"error": "allow_motion must be a boolean"}, status=400)
        allowed_tools = {
            name for name in all_safe_tools
            if allow_motion or name.startswith("self.lamp.")
        }
        if not allowed_tools:
            return web.json_response({"error": "no safe device tools are available"}, status=409)
        expected_tool = data.get("expected_tool", "")
        if expected_tool and expected_tool not in allowed_tools:
            return web.json_response({"error": "expected_tool is not permitted for this test"}, status=400)

        frame_bytes = 16000 * 60 // 1000 * 2
        padded_pcm = pcm + b"\x00" * ((-len(pcm)) % frame_bytes)
        try:
            completion = session.begin_e2e_test(allowed_tools)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)

        try:
            await session.send_json({"type": "test_audio", "event": "start"})
            # Wait for the device to enter listening and emit its normal
            # `listen:start` message before sending Opus input frames.
            await asyncio.sleep(0.18)
            for offset in range(0, len(padded_pcm), frame_bytes):
                frame = padded_pcm[offset:offset + frame_bytes]
                await session.send_json({
                    "type": "test_audio",
                    "event": "frame",
                    "pcm_b64": base64.b64encode(frame).decode("ascii"),
                })
                # Match real microphone pacing so the ESP32 encoder/send queue
                # and the server VAD observe ordinary 60 ms frames.
                await asyncio.sleep(0.065)
            await asyncio.sleep(0.25)
            await session.send_json({"type": "test_audio", "event": "end"})
            result = await asyncio.wait_for(asyncio.shield(completion), timeout=70)
        except asyncio.TimeoutError:
            session.cancel_e2e_test(completion)
            log.warning("E2E voice test timed out: device=%s", device_id)
            return web.json_response({"error": "model conversation timed out"}, status=504)
        except Exception as exc:
            session.cancel_e2e_test(completion)
            log.exception("E2E voice test failed: device=%s", device_id)
            return web.json_response({"error": "voice test failed", "detail": str(exc)}, status=502)

        tool_calls = result["tool_calls"]
        matched = (not expected_tool) or any(call["name"] == expected_tool for call in tool_calls)
        log.info("E2E voice test completed: device=%s tools=%s expected=%s matched=%s",
                 device_id, [call["name"] for call in tool_calls], expected_tool, matched)
        return web.json_response({
            "device_id": device_id,
            "tool_calls": tool_calls,
            "expected_tool": expected_tool,
            "matched": matched,
            "model_error": result["error"],
        })

    async def api_test_mode(self, request):
        """Switch the persisted firmware debug mode on an online device."""
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        device_id = data.get("device_id", "")
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return web.json_response({"error": "enabled must be a boolean"}, status=400)
        session = self._get_test_session(device_id)
        if session is None:
            return web.json_response({"error": "device is not online"}, status=404)
        try:
            await session.send_json({"type": "debug_mode", "enabled": enabled})
        except Exception as exc:
            log.exception("Failed to send debug mode command: device=%s", device_id)
            return web.json_response({"error": "could not send debug mode command", "detail": str(exc)}, status=502)
        # The device also declares this value in its next hello after a reboot.
        # Track the accepted command now so the protected test API is usable
        # during the persistent test connection.
        session.debug_mode = enabled
        log.warning("Device debug mode requested: device=%s enabled=%s", device_id, enabled)
        return web.json_response({"device_id": device_id, "debug_mode": enabled})

    async def api_logs(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        # 先推快照（最近的日志），再持续推送新增
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)

        snapshot = self.log_handler.snapshot()
        for entry in snapshot:
            try:
                await resp.write(("data: " + json.dumps(entry, ensure_ascii=False) + "\n\n").encode("utf-8"))
            except (ConnectionError, RuntimeError):
                return resp

        evt = self.log_handler._evt
        last_sequence = snapshot[-1]["id"] if snapshot else 0
        try:
            while True:
                # 检查是否有新日志
                new_entries = self.log_handler.events_after(last_sequence)
                if new_entries:
                    for entry in new_entries:
                        await resp.write(("data: " + json.dumps(entry, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    last_sequence = new_entries[-1]["id"]
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

    async def echo(self, request):
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        log.info("echo client connected from %s", request.remote)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await ws.send_str(msg.data)
                elif msg.type == WSMsgType.CLOSE:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            log.info("echo client disconnected")
        return ws
