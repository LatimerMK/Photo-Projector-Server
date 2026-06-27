"""
Photo Projector Server
- Галерея фото з drag-to-pan і масштабом
- Трансляція області екрану (MJPEG стрім)
- Авторизація по паролю з cookie-токеном
Запуск: python server.py -> http://localhost:8080
"""

import http.server
import json
import io
import time
import secrets
import threading
import urllib.parse
import socket
from pathlib import Path

# ── КОНФІГУРАЦІЯ ──────────────────────────────────────────────────────────────
PORT               = 8080
PASSWORD           = "1234"
TOKEN_COOKIE_DAYS  = 30
PHOTOS_DIR         = Path("photo")
CAPTURE_CONFIG     = Path("capture.json")
UI_HIDE_DELAY_MS   = 3000
DEFAULT_FPS        = 5
DEFAULT_QUALITY    = 75
SUPPORTED_EXT      = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}

# ── ГЛОБАЛЬНИЙ СТАН ───────────────────────────────────────────────────────────
_valid_tokens    = set()
_stream_fps      = DEFAULT_FPS
_stream_quality  = DEFAULT_QUALITY
_stream_lock     = threading.Lock()

# ── BROADCAST СТРІМ ───────────────────────────────────────────────────────────
_broadcast_clients      = []   # список черг підключених клієнтів
_broadcast_clients_lock = threading.Lock()
_broadcast_thread       = None
_last_frame             = None  # останній кадр для нових клієнтів

# ── ПЕРЕВІРКА ЗАЛЕЖНОСТЕЙ ─────────────────────────────────────────────────────
try:
    import mss
    MSS_OK = True
except ImportError:
    MSS_OK = False

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

STREAM_OK = MSS_OK and PIL_OK


# ══════════════════════════════════════════════════════════════════════════════
# HTML — будуємо рядками, БЕЗ f-string щоб уникнути конфлікту з JS {}
# ══════════════════════════════════════════════════════════════════════════════

HTML_LOGIN = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Вхід — Проектор</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #000; color: #fff;
    font-family: 'Segoe UI', system-ui, sans-serif;
    height: 100vh; display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 12px; padding: 40px 36px;
    width: 320px; display: flex; flex-direction: column; gap: 20px;
  }
  h1 { font-size: 18px; font-weight: 500; color: #eee; text-align: center; }
  input[type=password] {
    width: 100%; padding: 10px 14px;
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.2);
    border-radius: 7px; color: #fff; font-size: 15px; outline: none;
  }
  input[type=password]:focus { border-color: rgba(255,255,255,0.5); }
  button {
    width: 100%; padding: 10px;
    background: rgba(255,255,255,0.9); color: #000;
    border: none; border-radius: 7px;
    font-size: 15px; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #fff; }
  .error { color: #f66; font-size: 13px; text-align: center; }
  .hint  { color: #555; font-size: 12px; text-align: center; }
</style>
</head>
<body>
<div class="card">
  <h1>Проектор</h1>
  <input type="password" id="pw" placeholder="Пароль" autofocus>
  <button onclick="login()">Увійти</button>
  <p class="error" id="err" style="display:none">Невірний пароль</p>
  <p class="hint">Сесія запам'ятовується на 30 днів</p>
</div>
<script>
document.getElementById('pw').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') login();
});
async function login() {
  var pw = document.getElementById('pw').value;
  var r  = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: pw })
  });
  if (r.ok) { location.href = '/'; }
  else { document.getElementById('err').style.display = ''; }
}
</script>
</body>
</html>"""


def _build_main_html():
    """
    Будує HTML головної сторінки як звичайну конкатенацію рядків.
    Уникаємо f-string щоб фігурні дужки JS не конфліктували з Python.
    """
    fps_val  = str(DEFAULT_FPS)
    qual_val = str(DEFAULT_QUALITY)
    hide_ms  = str(UI_HIDE_DELAY_MS)

    css = """
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: #000; color: #fff;
  font-family: 'Segoe UI', system-ui, sans-serif;
  overflow: hidden; height: 100vh; width: 100vw; cursor: none;
}
body.ui-visible { cursor: default; }

/* ── TABS ── */
#tab-bar {
  position: fixed; top: 0; left: 50%; transform: translateX(-50%);
  display: flex; gap: 4px; padding: 10px;
  background: rgba(0,0,0,0.7); border-radius: 0 0 10px 10px;
  z-index: 20; transition: opacity 0.4s;
}
#tab-bar.hidden { opacity: 0; pointer-events: none; }

.tab-btn {
  background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.2);
  color: #fff; padding: 6px 20px; border-radius: 6px;
  font-size: 13px; cursor: pointer; transition: background 0.15s;
}
.tab-btn:hover  { background: rgba(255,255,255,0.2); }
.tab-btn.active { background: rgba(255,255,255,0.9); color: #000; border-color: transparent; }

/* ── PANELS ── */
.panel { display: none; width: 100vw; height: 100vh; }
.panel.active { display: flex; align-items: center; justify-content: center; }

/* ── STAGE ── */
.stage {
  width: 100vw; height: 100vh;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; cursor: grab; position: relative;
}
.stage:active { cursor: grabbing; }

/* ── IMAGES ── */
#main-img {
  display: block; transform-origin: center center;
  user-select: none; -webkit-user-drag: none;
}
#main-img.mode-fit    { width: 100vw; height: auto; }
#main-img.mode-fill   { width: 100vw; height: 100vh; object-fit: cover; }
#main-img.mode-custom { max-width: none; max-height: none; }

#stream-img {
  display: block; width: 100vw; height: auto;
  user-select: none; -webkit-user-drag: none;
}

/* ── EMPTY STATE ── */
.empty-state {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 16px; color: #444; font-size: 16px; text-align: center; padding: 20px;
}
.empty-state svg { opacity: 0.25; }

/* ── UI OVERLAY ── */
#ui-overlay {
  position: fixed; inset: 0; pointer-events: none;
  transition: opacity 0.4s ease; z-index: 10;
}
#ui-overlay.hidden { opacity: 0; }
#ui-overlay * { pointer-events: auto; }

/* ── CONTROLS ── */
#controls {
  position: absolute; top: 0; left: 0; right: 0;
  padding: 52px 20px 12px;
  display: flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: nowrap;
  background: linear-gradient(to bottom, rgba(0,0,0,0.8) 0%, transparent 100%);
  overflow-x: auto;
}
#file-name {
  font-size: 13px; color: #ccc;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px;
}
#counter { font-size: 13px; color: #888; white-space: nowrap; }

/* ── BUTTONS ── */
.btn {
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.2);
  color: #fff; padding: 6px 14px; border-radius: 6px;
  font-size: 13px; cursor: pointer; transition: background 0.15s;
  white-space: nowrap; line-height: 1.4;
}
.btn:hover  { background: rgba(255,255,255,0.25); }
.btn.active { background: rgba(255,255,255,0.9); color: #000; border-color: transparent; }

/* ── STEPPERS (замість слайдерів) ── */
.stepper { display: flex; align-items: center; gap: 0; }
.stepper-label { font-size: 12px; color: #999; white-space: nowrap; margin-right: 6px; }
.stepper-btn {
  background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.2);
  color: #fff; width: 26px; height: 26px; border-radius: 5px;
  font-size: 15px; cursor: pointer; line-height: 1;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s; flex-shrink: 0;
}
.stepper-btn:hover { background: rgba(255,255,255,0.3); }
.stepper-val {
  font-size: 13px; color: #ccc; min-width: 42px; text-align: center;
  padding: 0 4px;
}

/* ── NAV ARROWS ── */
.nav-arrow {
  position: absolute; top: 50%; transform: translateY(-50%);
  background: rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.15);
  color: #fff; width: 48px; height: 64px; border-radius: 6px;
  font-size: 22px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s; user-select: none;
}
.nav-arrow:hover { background: rgba(255,255,255,0.2); }
#arrow-prev { left: 12px; }
#arrow-next { right: 12px; }

/* ── THUMBNAILS ── */
#thumbnails-bar {
  position: absolute; bottom: 0; left: 0; right: 0;
  padding: 10px 16px;
  background: linear-gradient(to top, rgba(0,0,0,0.8) 0%, transparent 100%);
  display: flex; gap: 8px; overflow-x: auto; scroll-behavior: smooth;
  scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.3) transparent;
}
#thumbnails-bar::-webkit-scrollbar { height: 4px; }
#thumbnails-bar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.3); border-radius: 2px; }

.thumb {
  flex-shrink: 0; width: 80px; height: 60px; object-fit: cover;
  border-radius: 4px; cursor: pointer; opacity: 0.55;
  border: 2px solid transparent; transition: opacity 0.15s, border-color 0.15s;
}
.thumb:hover  { opacity: 0.85; }
.thumb.active { opacity: 1; border-color: #fff; }

/* ── STREAM CONTROLS ── */
#stream-controls {
  position: absolute; bottom: 0; left: 0; right: 0;
  padding: 12px 20px;
  background: linear-gradient(to top, rgba(0,0,0,0.8) 0%, transparent 100%);
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
#stream-status {
  font-size: 12px; margin-left: auto;
  padding: 3px 10px; border-radius: 20px;
  background: rgba(255,255,255,0.1); color: #aaa;
}
#stream-status.live { background: rgba(80,200,80,0.2); color: #6d6; }

#fullscreen-btn {
  font-size: 18px; padding: 4px 10px; line-height: 1;
  margin-left: 8px;
}

/* Fullscreen: ховаємо UI overlay крім кнопки виходу */
:fullscreen #ui-overlay,
:-webkit-full-screen #ui-overlay {
  pointer-events: none;
}
:fullscreen #stream-controls,
:-webkit-full-screen #stream-controls {
  pointer-events: auto;
}
</style>"""

    html_body = """
<div id="tab-bar">
  <button class="tab-btn active" onclick="switchTab('stream')">Трансляція</button>
  <button class="tab-btn"        onclick="switchTab('photos')">Фото</button>
</div>

<div class="panel active" id="panel-stream">
  <div class="stage" id="stream-stage">
    <div class="empty-state" id="stream-empty">
      <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="2" y="3" width="20" height="14" rx="2"/>
        <line x1="8" y1="21" x2="16" y2="21"/>
        <line x1="12" y1="17" x2="12" y2="21"/>
      </svg>
      <span id="stream-hint">Запустіть overlay.py щоб обрати область екрану</span>
    </div>
    <img id="stream-img" src="" alt="" style="display:none">
  </div>
</div>

<div class="panel" id="panel-photos">
  <div class="stage" id="photo-stage">
    <div class="empty-state" id="photo-empty">
      <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="3" y="3" width="18" height="18" rx="2"/>
        <circle cx="8.5" cy="8.5" r="1.5"/>
        <polyline points="21 15 16 10 5 21"/>
      </svg>
      <span>Додайте фото у папку photo/ і перезавантажте сторінку</span>
    </div>
    <img id="main-img" class="mode-fit" src="" alt="" style="display:none">
  </div>
</div>

<div id="ui-overlay">
  <div id="controls">
    <span id="file-name">—</span>
    <span id="counter"></span>
    <button class="btn active" data-mode="fit">Вписати</button>
    <button class="btn" data-mode="fill">Заповнити</button>
    <div class="stepper">
      <span class="stepper-label">Масштаб</span>
      <button class="stepper-btn" id="scale-down">&#8722;</button>
      <span class="stepper-val" id="scale-label">100%</span>
      <button class="stepper-btn" id="scale-up">&#43;</button>
    </div>
  </div>
  <button class="nav-arrow" id="arrow-prev">&#8249;</button>
  <button class="nav-arrow" id="arrow-next">&#8250;</button>
  <div id="thumbnails-bar"></div>
  <div id="stream-controls">
    <div class="stepper">
      <span class="stepper-label">FPS</span>
      <button class="stepper-btn" id="fps-down">&#8722;</button>
      <span class="stepper-val" id="fps-label">""" + fps_val + """</span>
      <button class="stepper-btn" id="fps-up">&#43;</button>
    </div>
    <div class="stepper">
      <span class="stepper-label">Якість</span>
      <button class="stepper-btn" id="qual-down">&#8722;</button>
      <span class="stepper-val" id="qual-label">""" + qual_val + """%</span>
      <button class="stepper-btn" id="qual-up">&#43;</button>
    </div>
    <span id="stream-status">очікування...</span>
    <button class="btn" id="fullscreen-btn" onclick="toggleFullscreen()" title="Повний екран">⛶</button>
  </div>
</div>"""

    # JS — окремим рядком, без f-string
    js = """
<script>
// ── STATE ──────────────────────────────────────────────────────────────────
var STATE = {
  currentTab:   'stream',
  uiTimer:      null,
  photos:       [],
  index:        0,
  mode:         'fit',
  pan:          { x: 0, y: 0 },
  drag:         { active: false, startX: 0, startY: 0, originX: 0, originY: 0 },
  streamActive:    false,
  streamTabActive: false,   // чи відкрита вкладка трансляції
  streamConnected: false,   // чи підключений MJPEG стрім
  pollTimer:       null,    // таймер polling /api/stream-info
  fps:          """ + fps_val + """,
  quality:      """ + qual_val + """,
};

var UI_HIDE_MS = """ + hide_ms + """;

// ── DOM CACHE ──────────────────────────────────────────────────────────────
var DOM = {};
function cacheDOM() {
  DOM.tabBar       = document.getElementById('tab-bar');
  DOM.panelStream  = document.getElementById('panel-stream');
  DOM.panelPhotos  = document.getElementById('panel-photos');
  DOM.streamStage  = document.getElementById('stream-stage');
  DOM.streamImg    = document.getElementById('stream-img');
  DOM.streamEmpty  = document.getElementById('stream-empty');
  DOM.streamHint   = document.getElementById('stream-hint');
  DOM.streamStatus = document.getElementById('stream-status');
  DOM.streamCtrl   = document.getElementById('stream-controls');
  DOM.fpsDown      = document.getElementById('fps-down');
  DOM.fpsUp        = document.getElementById('fps-up');
  DOM.fpsLabel     = document.getElementById('fps-label');
  DOM.qualDown     = document.getElementById('qual-down');
  DOM.qualUp       = document.getElementById('qual-up');
  DOM.qualLabel    = document.getElementById('qual-label');
  DOM.photoStage   = document.getElementById('photo-stage');
  DOM.mainImg      = document.getElementById('main-img');
  DOM.photoEmpty   = document.getElementById('photo-empty');
  DOM.fileName     = document.getElementById('file-name');
  DOM.counter      = document.getElementById('counter');
  DOM.controls     = document.getElementById('controls');
  DOM.modeBtns     = document.querySelectorAll('[data-mode]');
  DOM.scaleDown    = document.getElementById('scale-down');
  DOM.scaleUp      = document.getElementById('scale-up');
  DOM.scaleLabel   = document.getElementById('scale-label');
  DOM.thumbsBar    = document.getElementById('thumbnails-bar');
  DOM.arrowPrev    = document.getElementById('arrow-prev');
  DOM.arrowNext    = document.getElementById('arrow-next');
  DOM.overlay      = document.getElementById('ui-overlay');
}

// ── UI HIDE ────────────────────────────────────────────────────────────────
function showUI() {
  document.body.classList.add('ui-visible');
  DOM.overlay.classList.remove('hidden');
  DOM.tabBar.classList.remove('hidden');
  clearTimeout(STATE.uiTimer);
  STATE.uiTimer = setTimeout(hideUI, UI_HIDE_MS);
}
function hideUI() {
  document.body.classList.remove('ui-visible');
  DOM.overlay.classList.add('hidden');
  DOM.tabBar.classList.add('hidden');
}

// ── TABS ───────────────────────────────────────────────────────────────────
function switchTab(tab) {
  STATE.currentTab = tab;
  DOM.panelStream.classList.toggle('active', tab === 'stream');
  DOM.panelPhotos.classList.toggle('active', tab === 'photos');
  DOM.controls.style.display   = tab === 'photos' ? '' : 'none';
  DOM.arrowPrev.style.display  = tab === 'photos' ? '' : 'none';
  DOM.arrowNext.style.display  = tab === 'photos' ? '' : 'none';
  DOM.thumbsBar.style.display  = tab === 'photos' ? '' : 'none';
  DOM.streamCtrl.style.display = tab === 'stream' ? '' : 'none';
  document.querySelectorAll('.tab-btn').forEach(function(b, i) {
    b.classList.toggle('active', (i === 0) === (tab === 'stream'));
  });
  if (tab === 'stream') startStream();
  else stopStream();
  showUI();
}

// ── PAN ────────────────────────────────────────────────────────────────────
function resetPan() {
  STATE.pan = { x: 0, y: 0 };
  DOM.mainImg.style.transform = 'translate(0px, 0px)';
}
function applyPanTransform() {
  DOM.mainImg.style.transform = 'translate(' + STATE.pan.x + 'px, ' + STATE.pan.y + 'px)';
}

// ── SCALE ──────────────────────────────────────────────────────────────────
function applyCustomScale(pct) {
  pct = Math.max(10, Math.min(400, pct));
  DOM.mainImg.className      = 'mode-custom';
  DOM.mainImg.style.width    = pct + 'vw';
  DOM.mainImg.style.height   = '';
  DOM.scaleLabel.textContent = pct + '%';
  STATE.scale = pct; STATE.mode = 'custom';
  DOM.modeBtns.forEach(function(b) { b.classList.remove('active'); });
}
function applyMode(mode) {
  DOM.mainImg.className    = 'mode-' + mode;
  DOM.mainImg.style.width  = '';
  DOM.mainImg.style.height = '';
  STATE.mode = mode;
  DOM.modeBtns.forEach(function(b) {
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  resetPan();
  STATE.scale = 100;
  DOM.scaleLabel.textContent = '100%';
}

// ── API ────────────────────────────────────────────────────────────────────
async function fetchPhotos() {
  try {
    var r = await fetch('/api/photos');
    var d = await r.json();
    return d.photos || [];
  } catch(e) { console.error('[fetchPhotos]', e); return []; }
}
async function pushStreamSettings(fps, quality) {
  try {
    await fetch('/api/stream-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fps: fps, quality: quality })
    });
  } catch(e) { console.error('[pushStreamSettings]', e); }
}

// ── STREAM ─────────────────────────────────────────────────────────────────

// Запускає стрім — показує зображення з /stream
function _connectStream() {
  DOM.streamEmpty.style.display = 'none';
  DOM.streamImg.style.display   = 'block';
  DOM.streamImg.src = '/stream?' + Date.now();
  DOM.streamImg.onload = function() {
    DOM.streamStatus.textContent = 'LIVE';
    DOM.streamStatus.classList.add('live');
  };
  DOM.streamImg.onerror = function() {
    DOM.streamStatus.textContent = "помилка з'єднання";
    DOM.streamStatus.classList.remove('live');
    // При помилці повертаємось до polling
    DOM.streamImg.style.display   = 'none';
    DOM.streamEmpty.style.display = '';
    STATE.streamConnected = false;
    _pollStreamInfo();
  };
}

// Polling — кожні 2с питає /api/stream-info поки active не стане true
function _pollStreamInfo() {
  if (!STATE.streamTabActive || STATE.streamConnected) return;
  fetch('/api/stream-info').then(function(r) { return r.json(); }).then(function(info) {
    DOM.streamHint.innerHTML = info.message || 'Очікування...';
    if (info.available) {
      STATE.streamConnected = true;
      _connectStream();
    } else {
      // Повторюємо через 2 секунди
      STATE.pollTimer = setTimeout(_pollStreamInfo, 2000);
    }
  }).catch(function() {
    DOM.streamHint.textContent = "Немає з'єднання з сервером...";
    STATE.pollTimer = setTimeout(_pollStreamInfo, 2000);
  });
}

function startStream() {
  STATE.streamTabActive  = true;
  STATE.streamConnected  = false;
  clearTimeout(STATE.pollTimer);
  DOM.streamStatus.textContent = 'очікування...';
  DOM.streamStatus.classList.remove('live');
  _pollStreamInfo();
}

function stopStream() {
  STATE.streamTabActive  = false;
  STATE.streamConnected  = false;
  clearTimeout(STATE.pollTimer);
  DOM.streamImg.src  = '';
  DOM.streamImg.style.display   = 'none';
  DOM.streamEmpty.style.display = '';
  DOM.streamStatus.textContent  = 'очікування...';
  DOM.streamStatus.classList.remove('live');
}

// ── PHOTOS ─────────────────────────────────────────────────────────────────
function showPhoto(index) {
  if (!STATE.photos.length) return;
  STATE.index = (index + STATE.photos.length) % STATE.photos.length;
  var p = STATE.photos[STATE.index];
  DOM.mainImg.src              = p.url;
  DOM.mainImg.style.display    = 'block';
  DOM.photoEmpty.style.display = 'none';
  resetPan();
  DOM.fileName.textContent = p.name;
  DOM.counter.textContent  = (STATE.index + 1) + ' / ' + STATE.photos.length;
  Array.from(DOM.thumbsBar.children).forEach(function(el, i) {
    el.classList.toggle('active', i === STATE.index);
  });
  var thumb = DOM.thumbsBar.children[STATE.index];
  if (thumb) thumb.scrollIntoView({ inline: 'nearest', behavior: 'smooth' });
  showUI();
}
function buildThumbnails(photos) {
  DOM.thumbsBar.innerHTML = '';
  photos.forEach(function(p, i) {
    var img       = document.createElement('img');
    img.src       = p.url;
    img.title     = p.name;
    img.className = 'thumb' + (i === 0 ? ' active' : '');
    img.addEventListener('click', function() { showPhoto(i); });
    DOM.thumbsBar.appendChild(img);
  });
}

// ── DRAG-TO-PAN ────────────────────────────────────────────────────────────
function initDrag() {
  DOM.photoStage.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    STATE.drag = {
      active: true,
      startX: e.clientX, startY: e.clientY,
      originX: STATE.pan.x, originY: STATE.pan.y
    };
    DOM.photoStage.style.cursor = 'grabbing';
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (STATE.drag.active) {
      STATE.pan.x = STATE.drag.originX + (e.clientX - STATE.drag.startX);
      STATE.pan.y = STATE.drag.originY + (e.clientY - STATE.drag.startY);
      applyPanTransform();
      return;
    }
    showUI();
  });
  document.addEventListener('mouseup', function() {
    STATE.drag.active = false;
    DOM.photoStage.style.cursor = '';
  });
  DOM.photoStage.addEventListener('mouseenter', function() {
    if (!STATE.drag.active) DOM.photoStage.style.cursor = 'grab';
  });
  DOM.photoStage.addEventListener('mouseleave', function() {
    DOM.photoStage.style.cursor = '';
  });
}

// ── EVENTS ─────────────────────────────────────────────────────────────────
// ── FULLSCREEN ─────────────────────────────────────────────────────────────
function toggleFullscreen() {
  var el  = document.getElementById('panel-stream');
  var btn = document.getElementById('fullscreen-btn');
  if (!document.fullscreenElement && !document.webkitFullscreenElement) {
    var req = el.requestFullscreen || el.webkitRequestFullscreen;
    if (req) req.call(el);
  } else {
    var exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (exit) exit.call(document);
  }
}

// Оновлюємо іконку кнопки при зміні fullscreen
document.addEventListener('fullscreenchange', function() {
  var btn = document.getElementById('fullscreen-btn');
  if (btn) btn.textContent = document.fullscreenElement ? '✕' : '⛶';
});
document.addEventListener('webkitfullscreenchange', function() {
  var btn = document.getElementById('fullscreen-btn');
  if (btn) btn.textContent = document.webkitFullscreenElement ? '✕' : '⛶';
});

function initEvents() {
  DOM.modeBtns.forEach(function(b) {
    b.addEventListener('click', function() { applyMode(b.dataset.mode); });
  });

  // Stepper: масштаб
  DOM.scaleDown.addEventListener('click', function() {
    var cur = STATE.scale || 100;
    applyCustomScale(cur - 10);
  });
  DOM.scaleUp.addEventListener('click', function() {
    var cur = STATE.scale || 100;
    applyCustomScale(cur + 10);
  });

  DOM.arrowPrev.addEventListener('click', function() { showPhoto(STATE.index - 1); });
  DOM.arrowNext.addEventListener('click', function() { showPhoto(STATE.index + 1); });

  // Stepper: FPS
  DOM.fpsDown.addEventListener('click', function() {
    STATE.fps = Math.max(1, STATE.fps - 1);
    DOM.fpsLabel.textContent = STATE.fps;
    pushStreamSettings(STATE.fps, STATE.quality);
  });
  DOM.fpsUp.addEventListener('click', function() {
    STATE.fps = Math.min(10, STATE.fps + 1);
    DOM.fpsLabel.textContent = STATE.fps;
    pushStreamSettings(STATE.fps, STATE.quality);
  });

  // Stepper: якість
  DOM.qualDown.addEventListener('click', function() {
    STATE.quality = Math.max(10, STATE.quality - 5);
    DOM.qualLabel.textContent = STATE.quality + '%';
    pushStreamSettings(STATE.fps, STATE.quality);
  });
  DOM.qualUp.addEventListener('click', function() {
    STATE.quality = Math.min(95, STATE.quality + 5);
    DOM.qualLabel.textContent = STATE.quality + '%';
    pushStreamSettings(STATE.fps, STATE.quality);
  });

  document.addEventListener('keydown', function(e) {
    if (STATE.currentTab === 'photos') {
      if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   showPhoto(STATE.index - 1);
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown')  showPhoto(STATE.index + 1);
      if (e.key === 'r' || e.key === 'R') resetPan();
    }
    if (e.key === 'Escape') showUI();
  });
  document.addEventListener('touchstart', showUI);
}

// ── INIT ───────────────────────────────────────────────────────────────────
(async function() {
  cacheDOM();
  initDrag();
  initEvents();

  var photos = await fetchPhotos();
  STATE.photos = photos;
  if (photos.length) {
    buildThumbnails(photos);
    showPhoto(0);
  }

  switchTab('stream');
  showUI();
})();
</script>"""

    return (
        '<!DOCTYPE html>\n<html lang="uk">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>Проектор</title>\n'
        + css +
        '\n</head>\n<body>\n'
        + html_body +
        '\n' + js +
        '\n</body>\n</html>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# АВТОРИЗАЦІЯ
# ══════════════════════════════════════════════════════════════════════════════

def _make_token():
    return secrets.token_hex(32)

def _check_token(cookie_header):
    if not cookie_header:
        return False
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("auth="):
            return part[5:].strip() in _valid_tokens
    return False

def _set_auth_cookie(token):
    max_age = TOKEN_COOKIE_DAYS * 24 * 3600
    return "auth=" + token + "; Max-Age=" + str(max_age) + "; Path=/; HttpOnly; SameSite=Strict"


# ══════════════════════════════════════════════════════════════════════════════
# MJPEG СТРІМ
# ══════════════════════════════════════════════════════════════════════════════

def _read_capture_config():
    """Читає capture.json, повертає dict або None якщо файл відсутній/битий."""
    try:
        if not CAPTURE_CONFIG.exists():
            return None
        with open(CAPTURE_CONFIG) as f:
            return json.load(f)
    except Exception as e:
        print("[_read_capture_config] Помилка: " + str(e))
        return None


def _capture_frame():
    """
    Знімає кадр з області з capture.json.
    Повертає JPEG bytes або None якщо:
    - бібліотеки відсутні
    - файл конфігурації відсутній
    - active != true
    - розмір менший за мінімальний
    """
    if not STREAM_OK:
        return None
    try:
        cfg = _read_capture_config()
        if cfg is None:
            return None
        # Перевіряємо прапорець підтвердження
        if not cfg.get("active", False):
            return None
        w = int(cfg["width"])
        h = int(cfg["height"])
        # Перевіряємо мінімальний розмір
        if w < 10 or h < 10:
            print("[_capture_frame] Розмір занадто малий: " + str(w) + "x" + str(h))
            return None
        region = {
            "left":   int(cfg["x"]),
            "top":    int(cfg["y"]),
            "width":  w,
            "height": h,
        }
        with _stream_lock:
            quality = _stream_quality
        with mss.MSS() as sct:
            shot = sct.grab(region)
            img  = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        return buf.getvalue()
    except Exception as e:
        print("[_capture_frame] Помилка: " + str(e))
        return None


def _broadcast_loop():
    """
    Окремий потік: знімає кадри і розсилає всім підключеним клієнтам.
    Запускається один раз при першому підключенні до /stream.
    """
    global _last_frame
    boundary = b"--frame"
    print("[broadcast] Потік запущено")
    while True:
        try:
            with _stream_lock:
                fps = _stream_fps
            frame = _capture_frame()
            if frame is None:
                time.sleep(0.5)
                continue
            _last_frame = frame
            packet = (
                boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame + b"\r\n"
            )
            with _broadcast_clients_lock:
                clients = list(_broadcast_clients)
            for q in clients:
                try:
                    q.put_nowait(packet)
                except Exception:
                    pass  # черга переповнена — клієнт повільний
            time.sleep(1.0 / max(fps, 1))
        except Exception as e:
            print("[broadcast] Помилка: " + str(e))
            time.sleep(1.0)


def _ensure_broadcast_thread():
    """Запускає broadcast-потік якщо він ще не запущений."""
    global _broadcast_thread
    with _broadcast_clients_lock:
        if _broadcast_thread is None or not _broadcast_thread.is_alive():
            _broadcast_thread = threading.Thread(
                target=_broadcast_loop, daemon=True, name="broadcast"
            )
            _broadcast_thread.start()


def _stream_generator(handler):
    """Підписує клієнта на broadcast-чергу і надсилає кадри."""
    import queue as _queue
    client_ip = handler.client_address[0]
    print("[stream] Підключено: " + client_ip)
    _ensure_broadcast_thread()

    q = _queue.Queue(maxsize=3)
    with _broadcast_clients_lock:
        _broadcast_clients.append(q)

    # Одразу надсилаємо останній кадр якщо є
    if _last_frame:
        try:
            boundary = b"--frame"
            packet = (
                boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(_last_frame)).encode() + b"\r\n\r\n"
                + _last_frame + b"\r\n"
            )
            handler.wfile.write(packet)
            handler.wfile.flush()
        except Exception:
            pass

    try:
        while True:
            try:
                packet = q.get(timeout=5.0)
                handler.wfile.write(packet)
                handler.wfile.flush()
            except _queue.Empty:
                # Надсилаємо keepalive щоб з'єднання не рвалось
                handler.wfile.write(b"--frame\r\n\r\n")
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    except Exception as e:
        print("[stream] Помилка (" + client_ip + "): " + str(e))
    finally:
        with _broadcast_clients_lock:
            try:
                _broadcast_clients.remove(q)
            except ValueError:
                pass
        print("[stream] Відключено: " + client_ip)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP HANDLER
# ══════════════════════════════════════════════════════════════════════════════

# Кешуємо HTML при старті один раз
_HTML_MAIN = None

class ProjectorHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            path   = urllib.parse.urlparse(self.path).path
            cookie = self.headers.get("Cookie", "")

            if path == "/login":
                self._send_html(HTML_LOGIN)
                return

            if not _check_token(cookie):
                self._redirect("/login")
                return

            if path in ("/", "/index.html"):
                self._send_html(_HTML_MAIN)
            elif path == "/stream":
                self._handle_stream()
            elif path == "/api/photos":
                self._api_photos()
            elif path == "/api/stream-info":
                self._api_stream_info()
            elif path.startswith("/photos/"):
                self._send_photo(path)
            else:
                self._send_404()

        except Exception as e:
            print("[ProjectorHandler.do_GET] Помилка: " + str(e))

    def do_POST(self):
        try:
            path   = urllib.parse.urlparse(self.path).path
            cookie = self.headers.get("Cookie", "")

            if path == "/api/login":
                self._api_login()
            elif path == "/api/stream-settings" and _check_token(cookie):
                self._api_stream_settings()
            else:
                self._send_404()
        except Exception as e:
            print("[ProjectorHandler.do_POST] Помилка: " + str(e))

    # ── API ────────────────────────────────────────────────────────────────────

    def _api_login(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            pw     = body.get("password", "")
            if pw == PASSWORD:
                token = _make_token()
                _valid_tokens.add(token)
                data = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", _set_auth_cookie(token))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                print("  Вхід виконано, токен виданий")
            else:
                self._send_json({"error": "wrong password"}, 401)
                print("  Невірний пароль")
        except Exception as e:
            print("[_api_login] Помилка: " + str(e))
            self._send_json({"error": str(e)}, 500)

    def _api_stream_info(self):
        if not STREAM_OK:
            self._send_json({"available": False,
                             "message": "Встановіть: pip install mss pillow"})
            return
        cfg = _read_capture_config()
        if cfg is None:
            self._send_json({"available": False,
                             "message": "Запустіть overlay.py і оберіть область захвату"})
            return
        if not cfg.get("active", False):
            self._send_json({"available": False,
                             "message": "Натисніть 'Почати трансляцію' в overlay.py"})
            return
        self._send_json({"available": True})

    def _api_stream_settings(self):
        global _stream_fps, _stream_quality
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            with _stream_lock:
                _stream_fps     = max(1, min(10, int(body.get("fps",     _stream_fps))))
                _stream_quality = max(10, min(95, int(body.get("quality", _stream_quality))))
            self._send_json({"fps": _stream_fps, "quality": _stream_quality})
        except Exception as e:
            print("[_api_stream_settings] Помилка: " + str(e))
            self._send_json({"error": str(e)}, 500)

    def _api_photos(self):
        try:
            if not PHOTOS_DIR.exists():
                PHOTOS_DIR.mkdir()
            photos = []
            for f in sorted(PHOTOS_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT:
                    photos.append({
                        "name": f.name,
                        "url":  "/photos/" + urllib.parse.quote(f.name)
                    })
            self._send_json({"photos": photos})
        except Exception as e:
            print("[_api_photos] Помилка: " + str(e))
            self._send_json({"photos": []})

    # ── СТРІМ ──────────────────────────────────────────────────────────────────

    def _handle_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        _stream_generator(self)

    # ── СТАТИКА ────────────────────────────────────────────────────────────────

    def _send_photo(self, url_path):
        try:
            filename  = urllib.parse.unquote(url_path[len("/photos/"):])
            file_path = PHOTOS_DIR / filename
            if not file_path.is_file():
                self._send_404()
                return
            mime_map = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",  ".gif": "image/gif",
                ".bmp": "image/bmp",  ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }
            mime = mime_map.get(file_path.suffix.lower(), "application/octet-stream")
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print("[_send_photo] Помилка: " + str(e))
            self._send_404()

    # ── УТИЛІТИ ────────────────────────────────────────────────────────────────

    def _send_html(self, content):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _send_404(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        msg = str(args[0]) if args else ""
        if "/photos/" not in msg and "/stream" not in msg:
            status = str(args[1]) if len(args) > 1 else ""
            print("  " + msg + " -> " + status)


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДУ
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Визначити локальну IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    if not PHOTOS_DIR.exists():
        PHOTOS_DIR.mkdir()

    photo_count = sum(
        1 for f in PHOTOS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
    )

    # Будуємо HTML один раз при старті
    _HTML_MAIN = _build_main_html()

    print("=" * 50)
    print("  Projector Server")
    print("=" * 50)
    print("  Пароль         : " + PASSWORD)
    print("  Локальна мережа: http://" + local_ip + ":" + str(PORT))
    print("  Localhost      : http://localhost:" + str(PORT))
    print("  Фото           : " + str(photo_count) + " шт. в папці " + str(PHOTOS_DIR))
    stream_status = "готовий (mss + pillow)" if STREAM_OK else "pip install mss pillow"
    print("  Стрім          : " + stream_status)
    print("=" * 50)
    print("  Зупинити: Ctrl+C")
    print()

    server = http.server.HTTPServer(("", PORT), ProjectorHandler)
    server.allow_reuse_address = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер зупинено.")