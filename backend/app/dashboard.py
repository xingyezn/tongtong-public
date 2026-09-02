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
import secrets
import threading
import time
from collections import deque

from aiohttp import web

log = logging.getLogger("dash")

# 登录 cookie 名
AUTH_COOKIE = "tongtong_auth"
MAX_CAMERA_PHOTO_BYTES = 2 * 1024 * 1024


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
  .test-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-top:12px; }
  .test-group { padding:12px; border:1px solid #dceaf2; border-radius:12px; background:#f8fcfe; }
  .test-group h3 { margin:0 0 9px; font-size:14px; color:var(--fg); }
  .test-actions { display:flex; flex-wrap:wrap; gap:7px; }
  .test-actions .btn { padding:7px 10px; font-size:12px; }
  .test-input { width:72px; padding:6px 7px !important; }
  #hardware-test-result { min-height:42px; max-height:180px; overflow:auto; margin-top:12px; padding:9px 11px; border:1px solid #dceaf2; border-radius:9px; background:#f7fbfd; color:#3b5265; font:12px/1.5 Consolas,monospace; white-space:pre-wrap; }
  #camera-preview { display:none; margin-top:12px; padding:10px; border:1px solid #dceaf2; border-radius:9px; background:#f8fcfe; }
  #camera-preview img { display:block; width:min(100%, 640px); max-height:420px; object-fit:contain; border-radius:6px; background:#edf4f8; }
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

  <div class="card full">
    <h2>硬件手动测试</h2>
    <div class="row">
      <select id="hardware-device" onchange="updateHardwareTestPanel()" aria-label="选择设备"></select>
      <button class="btn" onclick="loadHardwareTests(true)">刷新测试项</button>
    </div>
    <div class="hint" id="hardware-test-status">等待设备上线…</div>
    <div id="hardware-test-groups" class="test-grid"></div>
    <pre id="hardware-test-result">尚未执行测试。</pre>
    <div id="camera-preview"><div class="muted" style="margin-bottom:7px">最近拍摄的照片（仅保存在后端内存，重启后自动清除）</div><img id="camera-preview-image" alt="设备最近拍摄的照片"></div>
    <div class="hint">所有按钮通过设备 MCP 通道执行；运动测试必须先让车轮悬空。设备未声明的摄像头、舵机或屏幕工具会显示为不可用，不会伪造测试结果。</div>
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
      </select>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px;align-self:flex-start">人物设定</label>
      <textarea id="cfg-instructions" rows="4" placeholder="你是童童，一个友好、热情的语音助手……" style="flex:1;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4;resize:vertical;font-family:Consolas,monospace;font-size:12px"></textarea>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">输入/输出采样率</label>
      <input type="number" id="cfg-in-rate" min="8000" max="48000" step="8000" value="16000" style="flex:1;max-width:140px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
      <input type="number" id="cfg-out-rate" min="8000" max="48000" step="8000" value="24000" style="flex:1;max-width:140px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
    </div>
    <div class="row" style="margin-bottom:10px">
      <label class="muted" style="min-width:130px">对话连续时长（分钟）</label>
      <input type="number" id="cfg-conversation-timeout" min="1" max="120" step="1" value="10" style="flex:1;max-width:140px;padding:6px 8px;background:#0f1420;border:1px solid #2a3550;border-radius:6px;color:#dbe4f4">
      <span class="muted">无新对话超过此时长后重置</span>
    </div>
    <div class="row">
      <button class="btn" id="save-model-btn" onclick="saveModel()">保存模型 / 音色 / 设定</button>
      <span class="muted" id="model-status"></span>
    </div>
    <div class="hint">
      修改后<b>下一轮对话生效</b>，并持久化保存（重启仍生效）。
      对话连续时长可设置为 1～120 分钟，默认 10 分钟。
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
          (dev.speaking ? '<span class="tag listen">播放中</span>' : "");
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
  renderHardwareTestControls(d.devices || []);
}

function renderHardwareTestControls(devices) {
  const select = $("hardware-device");
  const previous = select.value;
  dashboardDevices = devices.filter(dev => dev.online);
  select.innerHTML = dashboardDevices.map(dev =>
    `<option value="${dev.device_id}">${dev.device_id} (v${dev.bin_version})</option>`
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
let hardwareTools = {};
let hardwareToolsDevice = "";

const HARDWARE_TEST_GROUPS = [
  { title: "电机驱动", items: [
    ["self.chassis.go_forward", "前进"], ["self.chassis.go_back", "后退"],
    ["self.chassis.turn_left", "左转"], ["self.chassis.turn_right", "右转"],
    ["self.chassis.spin", "原地转圈"], ["self.chassis.stop", "停止"]
  ]},
  { title: "摄像头", items: [["self.camera.take_photo", "拍照测试"]] },
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
  if (name === "self.led.set_color") return {
    red: Math.max(0, Math.min(255, parseInt($("test-led-red").value, 10) || 0)),
    green: Math.max(0, Math.min(255, parseInt($("test-led-green").value, 10) || 0)),
    blue: Math.max(0, Math.min(255, parseInt($("test-led-blue").value, 10) || 0)),
  };
  if (name.indexOf("self.chassis.") === 0 && name !== "self.chassis.stop") {
    return { speed: parseInt($("test-speed").value, 10) || 30,
             duration_ms: parseInt($("test-duration").value, 10) || 500 };
  }
  if (name === "self.camera.take_photo") {
    return { question: $("test-camera-question").value.trim() || "检查摄像头是否能正常拍照" };
  }
  if (name === "self.screen.set_brightness") {
    return { brightness: parseInt($("test-brightness").value, 10) || 50 };
  }
  if (name === "self.screen.set_theme") {
    return { theme: $("test-theme").value };
  }
  return {};
}

function renderHardwareTests() {
  const box = $("hardware-test-groups");
  const device = selectedHardwareDevice();
  const canRun = !!device;
  const previousValues = {};
  ["test-speed", "test-duration", "test-brightness", "test-theme", "test-camera-question", "test-led-red", "test-led-green", "test-led-blue"].forEach(id => {
    const input = $(id);
    if (input) previousValues[id] = input.value;
  });
  if (!device) {
    box.innerHTML = '<div class="empty">没有在线设备。</div>';
    return;
  }
  let html = '<div class="row" style="grid-column:1/-1;margin-bottom:2px">' +
    '<label class="muted">电机速度 <input class="test-input" id="test-speed" type="number" min="0" max="100" value="30"></label>' +
    '<label class="muted">持续时间(ms) <input class="test-input" id="test-duration" type="number" min="1" max="10000" value="500"></label>' +
    '<label class="muted">屏幕亮度 <input class="test-input" id="test-brightness" type="number" min="0" max="100" value="50"></label>' +
    '<label class="muted">主题 <select id="test-theme"><option value="light">浅色</option><option value="dark">深色</option></select></label>' +
    '<label class="muted" style="flex:1;min-width:220px">拍照问题 <input id="test-camera-question" value="检查摄像头是否能正常拍照" style="width:100%;padding:6px 8px"></label></div>';
  html += HARDWARE_TEST_GROUPS.map(group => {
    const actions = group.items.map(([name, label]) => {
      const available = !!hardwareTools[name];
      const disabled = !canRun || !available;
      const reason = !available ? " title=\"设备未声明此工具\"" : "";
      return '<button class="btn"' + reason + (disabled ? " disabled" : "") +
        ' onclick=\'runHardwareTest("' + name + '", this)\'>' + label +
        (available ? "" : "（不可用）") + '</button>';
    }).join("");
    const colorInputs = group.title === "RGB 指示灯"
      ? '<div class="row" style="margin-bottom:8px"><label class="muted">R <input class="test-input" id="test-led-red" type="number" min="0" max="255" value="0"></label>' +
        '<label class="muted">G <input class="test-input" id="test-led-green" type="number" min="0" max="255" value="160"></label>' +
        '<label class="muted">B <input class="test-input" id="test-led-blue" type="number" min="0" max="255" value="255"></label></div>' : '';
    return '<div class="test-group"><h3>' + group.title + '</h3>' + colorInputs + '<div class="test-actions">' + actions + '</div></div>';
  }).join("");
  box.innerHTML = html;
  Object.entries(previousValues).forEach(([id, value]) => {
    const input = $(id);
    if (input) input.value = value;
  });
}

async function loadHardwareTests(force) {
  const device = selectedHardwareDevice();
  if (!device) { hardwareTools = {}; renderHardwareTests(); return; }
  if (!force && hardwareToolsDevice === device.device_id) { renderHardwareTests(); return; }
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
    $("hardware-test-result").textContent = JSON.stringify(data.result, null, 2);
    if (name === "self.camera.take_photo") showCameraPreview(device.device_id);
    showToast(name + " 测试完成", "ok");
  } catch (e) {
    $("hardware-test-result").textContent = "失败：" + e.message;
    showToast("硬件测试失败：" + e.message, "err");
  } finally {
    renderHardwareTests();
  }
}

async function loadModel() {
  try {
    const r = await fetch("/api/model");
    const d = await r.json();
    $("cfg-model").value = d.model || "";
    $("cfg-instructions").value = d.instructions || "";
    $("cfg-in-rate").value = d.input_sample_rate || 16000;
    $("cfg-out-rate").value = d.output_sample_rate || 24000;
    $("cfg-conversation-timeout").value = d.conversation_timeout_minutes || 10;
    const voice = d.voice || "";
    if (Array.from($("cfg-voice").options).some(option => option.value === voice)) {
      $("cfg-voice").value = voice;
    }
  } catch (e) {}
}
async function saveModel() {
  const voice = $("cfg-voice").value;
  const body = {
    model: $("cfg-model").value.trim(),
    voice: voice,
    instructions: $("cfg-instructions").value.trim(),
    input_sample_rate: parseInt($("cfg-in-rate").value, 10),
    output_sample_rate: parseInt($("cfg-out-rate").value, 10),
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
        # Latest JPEG per device. Photos are intentionally ephemeral: they
        # are lost on restart and never written to disk.
        self._camera_photos = {}

    def add_routes(self, app: web.Application):
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_get("/logout", self.logout)
        app.router.add_get("/api/status", self.api_status)
        app.router.add_get("/api/logs", self.api_logs)
        app.router.add_get("/api/vad", self.api_vad_get)
        app.router.add_post("/api/vad", self.api_vad_set)
        app.router.add_get("/api/model", self.api_model_get)
        app.router.add_post("/api/model", self.api_model_set)
        app.router.add_get("/api/test/tools", self.api_test_tools)
        app.router.add_post("/api/test/mcp", self.api_test_mcp)
        app.router.add_post("/api/camera/upload", self.api_camera_upload)
        app.router.add_get("/api/camera/latest", self.api_camera_latest)

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
            "conversation_timeout_minutes": ds.get("conversation_timeout_minutes", 10),
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
        if "conversation_timeout_minutes" in data:
            try:
                minutes = float(data["conversation_timeout_minutes"])
            except (TypeError, ValueError):
                return web.json_response({"error": "conversation_timeout_minutes must be a number"}, status=400)
            if not math.isfinite(minutes) or not 1 <= minutes <= 120:
                return web.json_response({"error": "conversation_timeout_minutes must be between 1 and 120"}, status=400)
            ds["conversation_timeout_minutes"] = minutes
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
            "instructions": ds.get("instructions", ""),
            "input_sample_rate": ds.get("input_sample_rate", 16000),
            "output_sample_rate": ds.get("output_sample_rate", 24000),
            "conversation_timeout_minutes": ds.get("conversation_timeout_minutes", 10),
            "api_key_configured": bool(ds.get("api_key")),
        })

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
        if not self._check_cookie(request):
            raise web.HTTPUnauthorized()
        device_id = request.query.get("device_id", "")
        photo = self._camera_photos.get(device_id)
        if photo is None:
            raise web.HTTPNotFound(text="no camera photo for this device")
        return web.Response(
            body=photo["data"],
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

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
