import io
import os
import shutil
import time
import threading
import hmac
from collections import deque

import cv2
import numpy as np
from flask import Flask, jsonify, request, render_template_string, session, redirect

from recognition import FaceRecognitionStore

PORT = int(os.getenv("FACE_DETECT_PORT", "8090"))
HOST = os.getenv("FACE_DETECT_HOST", "127.0.0.1")
MAX_BYTES = int(os.getenv("FACE_DETECT_MAX_BYTES", "10")) * 1024 * 1024
FACE_DB = os.getenv("FACE_RECOGNITION_DB", "/opt/face-detect-service/data/faces.db")
FACE_YUNET_MODEL = os.getenv("FACE_YUNET_MODEL", "/opt/face-detect-service/model/recognition/face_detection_yunet_2023mar.onnx")
FACE_SFACE_MODEL = os.getenv("FACE_SFACE_MODEL", "/opt/face-detect-service/model/recognition/face_recognition_sface_2021dec.onnx")
FACE_RECOGNITION_THRESHOLD = float(os.getenv("FACE_RECOGNITION_THRESHOLD", "0.6"))
FACE_ADMIN_USERNAME = os.getenv("FACE_ADMIN_USERNAME", "")
FACE_ADMIN_PASSWORD = os.getenv("FACE_ADMIN_PASSWORD", "")
FACE_AUTH_SECRET = os.getenv("FACE_AUTH_SECRET", "")

app = Flask(__name__)
app.secret_key = FACE_AUTH_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=False)
# Keep the human-readable summary as the first field in JSON responses.
app.config["JSON_SORT_KEYS"] = False
if hasattr(app, "json"):
    app.json.sort_keys = False
_lock = threading.Lock()
_history_lock = threading.Lock()
_history = deque(maxlen=10)
_request_seq = 0
_total_requests = 0
_successful_requests = 0
_failed_requests = 0
_cpu_prev = None

os.makedirs(os.path.dirname(FACE_DB), exist_ok=True)

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>人脸检测服务</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }
    body { margin: 0; background: #f3f6fb; color: #172033; }
    main { max-width: 1180px; margin: 0 auto; padding: 28px 18px 48px; }
    h1 { margin: 0 0 6px; font-size: 27px; }
    .muted { color: #667085; font-size: 14px; }
    .card { background: #fff; border: 1px solid #e4e8f0; border-radius: 12px; padding: 18px; margin-top: 18px; box-shadow: 0 2px 8px #1822300b; }
    .form { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    input[type=file] { max-width: 100%; }
    button { border: 0; border-radius: 8px; padding: 10px 17px; background: #2563eb; color: white; cursor: pointer; font-size: 14px; }
    button:disabled { opacity: .6; cursor: wait; }
    #result { white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; background: #101828; color: #d1fadf; border-radius: 8px; padding: 13px; margin: 0; min-height: 22px; height: 100%; font: 13px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; overflow: auto; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 800px; }
    th, td { text-align: left; border-bottom: 1px solid #edf0f5; padding: 10px 8px; vertical-align: top; }
    th { color: #667085; font-weight: 600; background: #fafbfc; }
    .ok { color: #067647; font-weight: 600; } .bad { color: #b42318; font-weight: 600; }
    .faces { max-width: 420px; white-space: pre-wrap; word-break: break-word; color: #475467; }
    .thumb { width: 72px; height: 54px; object-fit: cover; border-radius: 5px; border: 1px solid #d0d5dd; background: #f2f4f7; }
    .test-visual { margin-top: 14px; overflow: auto; background: #f8fafc; border: 1px solid #e4e8f0; border-radius: 8px; padding: 8px; max-width: 480px; height: 320px; display: flex; align-items: center; justify-content: center; }
    #test-canvas { display: block; max-width: 480px; max-height: 320px; width: auto; height: auto; }
    .test-result-layout { display: grid; grid-template-columns: minmax(0, 480px) minmax(300px, 1fr); gap: 14px; align-items: stretch; margin-top: 14px; }
    .test-result-layout .test-visual { margin-top: 0; min-width: 0; }
    .test-result-layout .json-panel { min-width: 0; height: 320px; }
    @media(max-width:800px){.test-result-layout{grid-template-columns:1fr;}.test-result-layout .json-panel{min-height:220px;}}
    .face-form { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; align-items:end; }
    .face-form label { display:flex; flex-direction:column; gap:5px; color:#667085; font-size:13px; }
    .face-form input { box-sizing:border-box; width:100%; padding:9px; border:1px solid #d0d5dd; border-radius:7px; font-size:14px; }
    .face-actions { display:flex; gap:7px; flex-wrap:wrap; }
    .secondary { background:#475467; } .danger { background:#b42318; }
    .topline { display:flex; justify-content:space-between; gap:12px; align-items: baseline; flex-wrap: wrap; }
    .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:12px; margin-top:12px; }
    .metric { background:#f8fafc; border:1px solid #e4e8f0; border-radius:9px; padding:13px; }
    .metric-label { color:#667085; font-size:13px; } .metric-value { font-size:22px; font-weight:650; margin-top:5px; }
    .metric-sub { color:#98a2b3; font-size:12px; margin-top:4px; }
  </style>
</head>
<body>
<main>
  <div class="topline"><div><h1>人脸识别服务</h1><div class="muted">YuNet + SFace · 最近 10 次识别请求</div></div><div><span id="health" class="muted">检查服务状态中…</span>　<a href="/logout">退出登录</a></div></div>
  <section class="card">
    <div class="topline"><h2>检测统计</h2><span id="metrics-time" class="muted">加载中…</span></div>
    <div class="metrics">
      <div class="metric"><div class="metric-label">检测请求总数</div><div id="m-requests" class="metric-value">-</div><div id="m-requests-sub" class="metric-sub">-</div></div>
      <div class="metric"><div class="metric-label">近 1 分钟请求</div><div id="m-rate" class="metric-value">-</div><div id="m-rate-sub" class="metric-sub">平均耗时 -</div></div>
    </div>
    <div hidden><span id="m-cpu"></span><span id="m-load"></span><span id="m-memory"></span><span id="m-memory-sub"></span><span id="m-disk"></span><span id="m-disk-sub"></span></div>
  </section>
  <section class="card">
    <h2>手动测试</h2>
    <div class="form"><input id="image" type="file" accept="image/jpeg,image/png,image/webp"><button id="test" onclick="runTest()">上传并识别</button></div>
    <div class="test-result-layout">
      <div id="test-visual" class="test-visual" hidden><canvas id="test-canvas"></canvas></div>
      <div class="json-panel"><div id="result">请选择一张图片后点击“上传并识别”。</div></div>
    </div>
  </section>
  <section class="card">
    <div class="topline"><h2>人脸库管理</h2><span id="face-status" class="muted">加载中…</span></div>
    <form id="face-form" class="face-form" onsubmit="saveFace(event)">
      <input id="face-id" type="hidden">
      <label>姓名<input id="face-name" required maxlength="100" placeholder="例如：张三"></label>
      <label>外部 ID<input id="face-external-id" maxlength="100" placeholder="例如：user-001"></label>
      <label>备注<input id="face-note" maxlength="500" placeholder="可选"></label>
      <label>样本图片（新增必填，必须包含 1 张人脸）<input id="face-image" type="file" accept="image/jpeg,image/png,image/webp" required></label>
      <div class="face-actions"><button id="face-submit" type="submit">新增人脸</button><button class="secondary" type="button" onclick="resetFaceForm()">清空</button></div>
    </form>
    <div id="recognize-result" style="margin-top:14px"></div>
    <div class="table-wrap" style="margin-top:16px"><table><thead><tr><th>ID</th><th>图片</th><th>姓名</th><th>外部 ID</th><th>备注</th><th>更新时间</th><th>操作</th></tr></thead><tbody id="faces"><tr><td colspan="7" class="muted">暂无人脸资料</td></tr></tbody></table></div>
  </section>
  <section class="card">
    <div class="topline"><h2>最近请求</h2><div><button onclick="loadHistory()">刷新</button> <button class="secondary" onclick="clearHistory()">清空记录</button></div></div>
    <div class="table-wrap"><table><thead><tr><th>时间</th><th>来源</th><th>图片</th><th>结果</th><th>耗时</th><th>人脸详情</th><th>操作</th></tr></thead><tbody id="history"><tr><td colspan="7" class="muted">暂无请求记录</td></tr></tbody></table></div>
  </section>
</main>
<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function loadHistory() {
  const res = await fetch('/api/recent', {cache:'no-store'});
  const rows = await res.json();
  document.getElementById('history').innerHTML = rows.length ? rows.map(r =>
    '<tr><td>' + esc(r.time) + '</td><td>' + esc(r.client_ip) + '<br>' + esc(r.method) + ' ' + esc(r.path) +
    '</td><td>' + (r.image_url ? '<a href="' + esc(r.image_url) + '" target="_blank"><img class="thumb" src="' + esc(r.image_url) + '" alt="请求图片"></a>' : '-') +
    '<br>' + esc(r.size || '-') + '<br>' + esc(r.content_type || '-') + '</td><td class="' + (r.ok ? 'ok' : 'bad') + '">' +
    (r.ok ? '成功<br>' + esc(r.count + ' 张人脸') : '失败<br>' + esc(r.error)) + '</td><td>' +
    (r.total_ms == null ? '-' : esc(r.total_ms + ' ms')) + '</td><td class="faces">' +
    (r.faces ? esc(JSON.stringify(r.faces)) : '-') + '</td><td><button class="secondary" onclick="deleteHistory(' + Number(r.id) + ')">删除</button></td></tr>'
  ).join('') : '<tr><td colspan="7" class="muted">暂无请求记录</td></tr>';
}
async function deleteHistory(id) {
  if (!confirm('确认删除这条请求记录及其图片？')) return;
  const res = await fetch('/api/recent/' + encodeURIComponent(id), {method:'DELETE'});
  if (!res.ok) { alert('删除失败'); return; }
  loadHistory();
}
async function clearHistory() {
  if (!confirm('确认清空最近请求及其图片？')) return;
  const res = await fetch('/api/recent', {method:'DELETE'});
  if (!res.ok) { alert('清空失败'); return; }
  loadHistory();
}
async function runTest() {
  const file = document.getElementById('image').files[0], btn = document.getElementById('test'), out = document.getElementById('result');
  if (!file) { out.textContent = '请先选择图片。'; return; }
  btn.disabled = true; out.textContent = '检测中…'; const fd = new FormData(); fd.append('image', file);
  try { const res = await fetch('/api/recognize', {method:'POST', body:fd}); const data=await res.json(); out.textContent = JSON.stringify(data, null, 2); drawTestResult(file, data); }
  catch (e) { out.textContent = '请求失败：' + e; } finally { btn.disabled = false; loadHistory(); }
}
function drawTestResult(file, data) {
  const holder=document.getElementById('test-visual'), canvas=document.getElementById('test-canvas');
  const image=new Image(); image.onload=()=>{const maxWidth=480, maxHeight=320, scale=Math.min(1,maxWidth/image.width,maxHeight/image.height); canvas.width=Math.round(image.width*scale); canvas.height=Math.round(image.height*scale); const ctx=canvas.getContext('2d'); ctx.drawImage(image,0,0,canvas.width,canvas.height); ctx.lineWidth=Math.max(2,3*scale); ctx.font=`${Math.max(13,18*scale)}px sans-serif`; (data.detected_faces||[]).forEach(face=>{const [x,y,w,h]=face.box||[]; const color=face.recognized?'#16a34a':'#f59e0b'; ctx.strokeStyle=color;ctx.fillStyle=color;ctx.strokeRect(x*scale,y*scale,w*scale,h*scale);const label=(face.name||'unknown')+' '+(face.score==null?'未匹配':Number(face.score).toFixed(3));const tw=ctx.measureText(label).width+10, ty=Math.max(20,y*scale);ctx.fillRect(x*scale,ty-20,tw,20);ctx.fillStyle='#fff';ctx.fillText(label,x*scale+5,ty-5);}); holder.hidden=false;}; image.src=URL.createObjectURL(file);
}
async function loadFaces() {
  try { const data=await (await fetch('/api/faces')).json(); document.getElementById('face-status').textContent=data.recognition_ready?'识别模型正常 · '+data.count+' 条':'识别模型未就绪'; const rows=data.faces||[]; document.getElementById('faces').innerHTML=rows.length?rows.map(f=>`<tr><td>${esc(f.id)}</td><td>${f.image_url?`<a href="${esc(f.image_url)}" target="_blank"><img class="thumb" src="${esc(f.image_url)}"></a>`:'-'}</td><td>${esc(f.name)}</td><td>${esc(f.external_id||'-')}</td><td>${esc(f.note||'-')}</td><td>${esc(f.updated_at)}</td><td><button class="secondary" onclick="editFace(${f.id})">编辑</button> <button class="danger" onclick="deleteFace(${f.id})">删除</button></td></tr>`).join(''):'<tr><td colspan="7" class="muted">暂无人脸资料</td></tr>'; window.faceRows=rows; } catch(e) { document.getElementById('face-status').textContent='读取失败'; }
}
function resetFaceForm() { document.getElementById('face-form').reset(); document.getElementById('face-id').value=''; document.getElementById('face-submit').textContent='新增人脸'; }
function editFace(id) { const f=(window.faceRows||[]).find(x=>x.id===id); if(!f)return; document.getElementById('face-id').value=f.id; document.getElementById('face-name').value=f.name; document.getElementById('face-external-id').value=f.external_id||''; document.getElementById('face-note').value=f.note||''; document.getElementById('face-image').value=''; document.getElementById('face-submit').textContent='保存修改'; window.scrollTo({top:document.getElementById('face-form').offsetTop-20,behavior:'smooth'}); }
async function saveFace(e) { e.preventDefault(); const id=document.getElementById('face-id').value, fd=new FormData(); fd.append('name',document.getElementById('face-name').value); fd.append('external_id',document.getElementById('face-external-id').value); fd.append('note',document.getElementById('face-note').value); const file=document.getElementById('face-image').files[0]; if(file)fd.append('image',file); if(!id&&!file){alert('新增人脸必须选择一张样本图片');return;} const res=await fetch(id?'/api/faces/'+id:'/api/faces',{method:id?'PATCH':'POST',body:id&& !file?JSON.stringify({name:fd.get('name'),external_id:fd.get('external_id'),note:fd.get('note')}):fd,headers:id&&!file?{'Content-Type':'application/json'}:{}}); const data=await res.json(); if(!res.ok){alert(data.error||'操作失败');return;} resetFaceForm(); loadFaces(); }
async function deleteFace(id) { if(!confirm('确定删除这条人脸资料吗？'))return; const res=await fetch('/api/faces/'+id,{method:'DELETE'}); const data=await res.json(); if(!res.ok)alert(data.error||'删除失败'); loadFaces(); }
async function recognizeFromPage() { const file=document.getElementById('recognize-image').files[0], out=document.getElementById('recognize-result'); if(!file){out.textContent='请先选择识别图片。';return;} const fd=new FormData();fd.append('image',file);out.textContent='识别中…'; try{const res=await fetch('/api/recognize',{method:'POST',body:fd});out.textContent=JSON.stringify(await res.json(),null,2);}catch(e){out.textContent='识别失败：'+e;} }
const fmtBytes = n => { if (n == null) return '-'; const u=['B','KB','MB','GB','TB']; let i=0; while(n>=1024 && i<u.length-1){n/=1024;i++;} return n.toFixed(i?1:0)+' '+u[i]; };
async function loadMetrics() {
  try {
    const m = await (await fetch('/api/metrics', {cache:'no-store'})).json();
    document.getElementById('m-cpu').textContent = m.cpu_percent == null ? '采样中…' : m.cpu_percent+'%';
    document.getElementById('m-load').textContent = '负载 '+(m.load_1m == null ? '-' : m.load_1m)+' · '+m.cpu_cores+' 核';
    document.getElementById('m-memory').textContent = m.memory.percent+'%';
    document.getElementById('m-memory-sub').textContent = fmtBytes(m.memory.used)+' / '+fmtBytes(m.memory.total);
    document.getElementById('m-disk').textContent = m.disk.percent+'%';
    document.getElementById('m-disk-sub').textContent = fmtBytes(m.disk.used)+' / '+fmtBytes(m.disk.total);
    document.getElementById('m-requests').textContent = m.requests.total;
    document.getElementById('m-requests-sub').textContent = '成功 '+m.requests.success+' · 失败 '+m.requests.failed;
    document.getElementById('m-rate').textContent = m.requests.last_minute;
    document.getElementById('m-rate-sub').textContent = '平均耗时 '+(m.requests.avg_ms_last_minute == null ? '-' : m.requests.avg_ms_last_minute+' ms');
    document.getElementById('metrics-time').textContent = '更新于 '+m.time;
  } catch(e) { document.getElementById('metrics-time').textContent='监控暂不可用'; }
}
async function loadHealth() { try { const r=await fetch('/health', {cache:'no-store'}); if(!r.ok) throw Error('health '+r.status); const j=await r.json(); document.getElementById('health').textContent='● 服务正常 · '+j.model; document.getElementById('health').style.color='#067647'; } catch(e) { document.getElementById('health').textContent='● 服务不可用'; document.getElementById('health').style.color='#b42318'; } }
// Edit face records in place so the operator keeps the current table context.
function editFace(id) {
  const row = document.querySelector('#faces button[onclick="editFace(' + id + ')"]')?.closest('tr');
  const face = (window.faceRows || []).find(item => item.id === id);
  if (!row || !face || row.dataset.editing === '1') return;
  row.dataset.editing = '1';
  row.innerHTML = '<td>' + esc(face.id) + '</td>'
    + '<td><input class="inline-face-image" type="file" accept="image/jpeg,image/png,image/webp"></td>'
    + '<td><input class="inline-face-name" value="' + esc(face.name) + '" maxlength="100"></td>'
    + '<td><input class="inline-face-external" value="' + esc(face.external_id || '') + '" maxlength="100" placeholder="同名时必填"></td>'
    + '<td><input class="inline-face-note" value="' + esc(face.note || '') + '" maxlength="500"></td>'
    + '<td>' + esc(face.updated_at) + '</td>'
    + '<td><button class="secondary" onclick="saveInlineFace(' + id + ',this)">保存</button> '
    + '<button class="secondary" onclick="loadFaces()">取消</button></td>';
}
async function saveInlineFace(id, button) {
  const row = button.closest('tr');
  const name = row.querySelector('.inline-face-name').value.trim();
  const externalId = row.querySelector('.inline-face-external').value.trim();
  const note = row.querySelector('.inline-face-note').value.trim();
  const image = row.querySelector('.inline-face-image').files[0];
  if (!name) { alert('姓名不能为空'); return; }
  const sameName = (window.faceRows || []).some(item => item.id !== id && item.name === name);
  if (sameName && !externalId) { alert('已有同名人脸资料，请填写外部 ID 后再保存'); return; }
  const body = new FormData(); body.append('name', name); body.append('external_id', externalId); body.append('note', note);
  if (image) body.append('image', image);
  const response = await fetch('/api/faces/' + id, {method:'PATCH', body:body});
  const data = await response.json();
  if (!response.ok) { alert(data.error || '保存失败'); return; }
  loadFaces();
}
loadHealth(); loadHistory(); loadMetrics(); loadFaces(); setInterval(loadHealth, 10000); setInterval(loadHistory, 5000); setInterval(loadMetrics, 3000); setInterval(loadFaces, 10000);
</script>
</body></html>"""

LOGIN_PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 - 人脸检测服务</title><style>body{margin:0;background:#f3f6fb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#172033}.box{width:min(360px,calc(100% - 36px));margin:12vh auto;background:#fff;border:1px solid #e4e8f0;border-radius:12px;padding:26px;box-shadow:0 2px 8px #1822300b}h1{font-size:23px;margin:0 0 20px}label{display:block;color:#667085;font-size:14px;margin:14px 0 6px}input{box-sizing:border-box;width:100%;padding:10px;border:1px solid #d0d5dd;border-radius:7px;font-size:15px}button{width:100%;margin-top:20px;border:0;border-radius:7px;padding:11px;background:#2563eb;color:#fff;font-size:15px;cursor:pointer}.error{color:#b42318;background:#fef3f2;padding:9px;border-radius:7px;font-size:14px;margin-bottom:10px}</style></head><body><div class="box"><h1>人脸检测服务后台</h1>{% if error %}<div class="error">{{ error }}</div>{% endif %}<form method="post"><label>用户名</label><input name="username" autocomplete="username" required autofocus><label>密码</label><input name="password" type="password" autocomplete="current-password" required><button type="submit">登录</button></form></div></body></html>"""


@app.before_request
def require_login():
    public_paths = ("/login", "/logout", "/health")
    if request.path in public_paths:
        return None
    if session.get("face_admin"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required", "login": "/login"}), 401
    return redirect("/login")

# The legacy YOLO/OpenVINO detector is intentionally disabled.  All face
# detection and recognition now goes through YuNet + SFace below.
_detector = None
_model_desc = "YuNet + SFace"
_face_store = FaceRecognitionStore(FACE_DB, FACE_YUNET_MODEL, FACE_SFACE_MODEL, FACE_RECOGNITION_THRESHOLD)


def _decode_image(data):
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cannot decode image (need JPEG/PNG bytes)")
    return img


def _image_from_request():
    if request.files:
        f = request.files.get("image")
        if f is None:
            raise ValueError("multipart field 'image' not found")
        data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("image too large")
        return _decode_image(data), data, f.mimetype or "application/octet-stream"
    data = request.get_data(cache=False)
    if not data:
        raise ValueError("empty body: send raw image bytes or multipart 'image'")
    if len(data) > MAX_BYTES:
        raise ValueError("image too large")
    return _decode_image(data), data, request.content_type or "application/octet-stream"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": _model_desc, "recognition_ready": _face_store.ready})


@app.route("/", methods=["GET"])
def index():
    return render_template_string(PAGE)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username, FACE_ADMIN_USERNAME) and hmac.compare_digest(password, FACE_ADMIN_PASSWORD):
            session.clear()
            session["face_admin"] = True
            return redirect("/")
        return render_template_string(LOGIN_PAGE, error="用户名或密码错误")
    return render_template_string(LOGIN_PAGE, error=None)


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect("/login")


def _record(entry):
    global _request_seq, _total_requests, _successful_requests, _failed_requests
    with _history_lock:
        _request_seq += 1
        _total_requests += 1
        if entry.get("ok"):
            _successful_requests += 1
        else:
            _failed_requests += 1
        entry["id"] = _request_seq
        _history.appendleft(entry)


@app.route("/api/recent", methods=["GET"])
def recent():
    with _history_lock:
        rows = []
        for item in _history:
            row = {k: v for k, v in item.items() if not k.startswith("_")}
            if item.get("_image_data"):
                row["image_url"] = "/api/recent/{}/image".format(item["id"])
            rows.append(row)
        return jsonify(rows)


@app.route("/api/recent", methods=["DELETE"])
def clear_recent():
    with _history_lock:
        deleted = len(_history)
        _history.clear()
    return jsonify({"success": True, "deleted": deleted})


@app.route("/api/recent/<int:request_id>", methods=["DELETE"])
def delete_recent(request_id):
    with _history_lock:
        item = next((x for x in _history if x.get("id") == request_id), None)
        if item is None:
            return jsonify({"error": "request record not found"}), 404
        _history.remove(item)
    return jsonify({"success": True, "id": request_id})


@app.route("/api/recent/<int:request_id>/image", methods=["GET"])
def recent_image(request_id):
    with _history_lock:
        item = next((x for x in _history if x.get("id") == request_id), None)
        if item is None or not item.get("_image_data"):
            return jsonify({"error": "image not found (history may have rotated)"}), 404
        data = item["_image_data"]
        mimetype = item.get("_image_type", "application/octet-stream")
    return app.response_class(data, mimetype=mimetype, headers={"Cache-Control": "no-store"})


def _recognition_request():
    if request.files:
        f = request.files.get("image")
        if f is None:
            raise ValueError("multipart field 'image' not found")
        data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("image too large")
        return _decode_image(data), data, f.mimetype or "application/octet-stream"
    data = request.get_data(cache=False)
    if not data:
        raise ValueError("empty body: send raw image bytes or multipart 'image'")
    if len(data) > MAX_BYTES:
        raise ValueError("image too large")
    return _decode_image(data), data, request.content_type or "application/octet-stream"


def _face_fields(required=False):
    if request.is_json:
        body = request.get_json(silent=True) or {}
    else:
        body = request.form
    name = str(body.get("name", "")).strip()
    if required and not name:
        raise ValueError("name is required")
    return name, str(body.get("external_id", "")).strip(), str(body.get("note", "")).strip()


@app.route("/api/faces", methods=["GET"])
def face_list():
    faces = _face_store.list()
    for item in faces:
        if item["has_image"]:
            item["image_url"] = "/api/faces/{}/image".format(item["id"])
    return jsonify({"faces": faces, "count": len(faces), "recognition_ready": _face_store.ready})


@app.route("/api/faces/<int:face_id>", methods=["GET"])
def face_detail(face_id):
    item = _face_store.get(face_id)
    if not item:
        return jsonify({"error": "face not found"}), 404
    if item["has_image"]:
        item["image_url"] = "/api/faces/{}/image".format(face_id)
    return jsonify(item)


@app.route("/api/faces/<int:face_id>/image", methods=["GET"])
def face_image(face_id):
    item = _face_store.image(face_id)
    if not item:
        return jsonify({"error": "face image not found"}), 404
    return app.response_class(item[0], mimetype=item[1], headers={"Cache-Control": "no-store"})


@app.route("/api/faces", methods=["POST"])
def face_create():
    try:
        name, external_id, note = _face_fields(required=True)
        image, image_data, image_type = _recognition_request()
        item = _face_store.create(name, external_id, note, image, image_data, image_type)
        item["image_url"] = "/api/faces/{}/image".format(item["id"])
        return jsonify(item), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/faces/<int:face_id>", methods=["PUT", "PATCH"])
def face_update(face_id):
    try:
        old = _face_store.get(face_id)
        if not old:
            return jsonify({"error": "face not found"}), 404
        name, external_id, note = _face_fields(required=False)
        name = name or old["name"]
        external_id = external_id if (request.is_json or "external_id" in request.form) else old["external_id"]
        note = note if (request.is_json or "note" in request.form) else old["note"]
        image = image_data = image_type = None
        if request.files:
            image, image_data, image_type = _recognition_request()
        item = _face_store.update(face_id, name, external_id, note, image, image_data, image_type)
        if item["has_image"]:
            item["image_url"] = "/api/faces/{}/image".format(face_id)
        return jsonify(item)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/faces/<int:face_id>", methods=["DELETE"])
def face_delete(face_id):
    if not _face_store.delete(face_id):
        return jsonify({"error": "face not found"}), 404
    return jsonify({"deleted": True, "id": face_id})


@app.route("/api/recognize", methods=["POST"])
def recognize_face():
    started = time.perf_counter()
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client_ip": request.headers.get("X-Forwarded-For", request.remote_addr or "-"),
        "method": request.method,
        "path": request.path,
        "content_type": request.content_type or "-",
        "ok": False,
    }
    try:
        image, image_data, image_type = _recognition_request()
        result = _face_store.recognize(image)
        record.update({
            "count": result.get("count", 0),
            "faces": result.get("faces", []),
            "detected_faces": result.get("detected_faces", []),
            "threshold": result.get("threshold"),
            "total_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "ok": True,
            "_image_data": image_data,
            "_image_type": image_type,
        })
        _record(record)
        return jsonify(result)
    except (ValueError, RuntimeError) as exc:
        record.update({
            "error": str(exc),
            "total_ms": round((time.perf_counter() - started) * 1000.0, 2),
        })
        _record(record)
        return jsonify({"error": str(exc)}), 400


def _read_cpu():
    global _cpu_prev
    try:
        fields = open("/proc/stat", "r").readline().split()[1:]
        values = [int(x) for x in fields[:8]]
        idle = values[3] + values[4]
        total = sum(values)
        current = (total, idle)
        if _cpu_prev is None:
            _cpu_prev = current
            return None
        total_delta = total - _cpu_prev[0]
        idle_delta = idle - _cpu_prev[1]
        _cpu_prev = current
        return round(max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)), 1) if total_delta else None
    except (OSError, ValueError, IndexError):
        return None


def _read_memory():
    values = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = max(0, total - available)
        return {"total": total, "used": used, "available": available, "percent": round(used * 100.0 / total, 1) if total else 0}
    except (OSError, ValueError):
        return {"total": 0, "used": 0, "available": 0, "percent": 0}


@app.route("/api/metrics", methods=["GET"])
def metrics():
    cpu_percent = _read_cpu()
    memory = _read_memory()
    disk_usage = shutil.disk_usage("/")
    disk = {"total": disk_usage.total, "used": disk_usage.used, "free": disk_usage.free,
            "percent": round(disk_usage.used * 100.0 / disk_usage.total, 1) if disk_usage.total else 0}
    now = time.time()
    with _history_lock:
        recent_rows = [x for x in _history if now - time.mktime(time.strptime(x["time"], "%Y-%m-%d %H:%M:%S")) <= 60]
        avg_ms = [x["total_ms"] for x in recent_rows if x.get("total_ms") is not None]
        requests = {"total": _total_requests, "success": _successful_requests, "failed": _failed_requests,
                    "last_minute": len(recent_rows), "avg_ms_last_minute": round(sum(avg_ms) / len(avg_ms), 2) if avg_ms else None}
    try:
        load_1m = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        load_1m = None
    return jsonify({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "cpu_percent": cpu_percent,
                    "cpu_cores": os.cpu_count() or 1, "load_1m": load_1m,
                    "memory": memory, "disk": disk, "requests": requests})


@app.route("/detect", methods=["POST"])
def detect():
    started = time.perf_counter()
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client_ip": request.headers.get("X-Forwarded-For", request.remote_addr or "-"),
        "method": request.method,
        "path": request.path,
        "content_type": request.content_type or "-",
        "ok": False,
    }
    if _detector is None:
        record["error"] = _model_desc
        _record(record)
        return jsonify({"error": _model_desc}), 410
    try:
        img, image_data, image_type = _image_from_request()
    except ValueError as exc:
        record["error"] = str(exc)
        record["total_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        _record(record)
        return jsonify({"error": str(exc)}), 400

    t0 = time.perf_counter()
    try:
        with _lock:
            boxes, scores = _detector(img)
    except Exception as exc:
        record["error"] = str(exc)
        record["total_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        _record(record)
        return jsonify({"error": "inference failed: " + str(exc)}), 500
    total_ms = (time.perf_counter() - t0) * 1000.0

    faces = [
        {"box": [round(float(v), 1) for v in b], "confidence": round(float(s), 4)}
        for b, s in zip(boxes, scores)
    ]
    h, w = img.shape[:2]
    response = {
        "count": len(faces),
        "faces": faces,
        "size": [w, h],
        "total_ms": round(total_ms, 2),
    }
    record.update({"size": f"{w}×{h}", "count": len(faces), "faces": faces, "total_ms": response["total_ms"], "ok": True,
                   "_image_data": image_data, "_image_type": image_type})
    _record(record)
    return jsonify(response)


if __name__ == "__main__":
    from waitress import serve
    serve(app, host=HOST, port=PORT, threads=4)
