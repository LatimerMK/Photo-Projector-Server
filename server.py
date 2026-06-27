"""
Photo Projector Server
— Галерея фото з drag-to-pan і масштабом
— Трансляція області екрану (MJPEG стрім)
— Авторизація по паролю з cookie-токеном
Запуск: python server.py  →  http://localhost:8080
"""

import http.server
import json
import os
import io
import time
import hashlib
import secrets
import threading
import urllib.parse
import socket
from pathlib import Path

# ── КОНФІГУРАЦІЯ ──────────────────────────────────────────────────────────────
PORT               = 8080             # Порт веб-сервера
PASSWORD           = "1234"           # Пароль для входу (змінити тут)
TOKEN_COOKIE_DAYS  = 30               # Скільки днів зберігається cookie "запам'ятати"
PHOTOS_DIR         = Path("photo")    # Папка з фотографіями
CAPTURE_CONFIG     = Path("capture.json")  # Координати рамки від overlay.py
UI_HIDE_DELAY_MS   = 3000             # Мс до приховування UI при бездіяльності
DEFAULT_FPS        = 5                # Початкова частота кадрів стріму
DEFAULT_QUALITY    = 75               # Початкова якість JPEG (1–95)
SUPPORTED_EXT      = {                # Підтримувані формати фото
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"
}

# ── ГЛОБАЛЬНИЙ СТАН ───────────────────────────────────────────────────────────
# Зберігається між запитами в пам'яті процесу
_valid_tokens: set  = set()           # Активні сесійні токени
_stream_fps:   int  = DEFAULT_FPS     # Поточний FPS (змінюється з браузера)
_stream_quality: int = DEFAULT_QUALITY  # Поточна якість JPEG
_stream_lock   = threading.Lock()     # Захист _stream_fps / _stream_quality

# ── ПЕРЕВІРКА ЗАЛЕЖНОСТЕЙ ─────────────────────────────────────────────────────
try:
    import mss
    import mss.tools
    MSS_OK = True
except ImportError:
    MSS_OK = False

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

STREAM_OK = MSS_OK and PIL_OK   # True якщо стрім доступний


# ══════════════════════════════════════════════════════════════════════════════
# HTML — СТОРІНКА АВТОРИЗАЦІЇ
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
  <h1>🎨 Проектор</h1>
  <input type="password" id="pw" placeholder="Пароль" autofocus>
  <button onclick="login()">Увійти</button>
  <p class="error" id="err" style="display:none">Невірний пароль</p>
  <p class="hint">Сесія запам'ятовується на 30 днів</p>
</div>
<script>
document.getElementById('pw').addEventListener('keydown', e => {
  if (e.key === 'Enter') login();
});
async function login() {
  const pw = document.getElementById('pw').value;
  const r  = await fetch('/api/login', {
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


# ══════════════════════════════════════════════════════════════════════════════
# HTML — ГОЛОВНА СТОРІНКА (галерея + стрім)
# ══════════════════════════════════════════════════════════════════════════════
HTML_MAIN = f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Проектор</title>
<style>
/* ── RESET ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  background: #000; color: #fff;
  font-family: 'Segoe UI', system-ui, sans-serif;
  overflow: hidden; height: 100vh; width: 100vw;
  cursor: none;
}}
body.ui-visible {{ cursor: default; }}

/* ── TABS ── */
#tab-bar {{
  position: fixed; top: 0; left: 50%; transform: translateX(-50%);
  display: flex; gap: 4px; padding: 10px;
  background: rgba(0,0,0,0.7); border-radius: 0 0 10px 10px;
  z-index: 20; transition: opacity 0.4s;
}}
#tab-bar.hidden {{ opacity: 0; pointer-events: none; }}

.tab-btn {{
  background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.2);
  color: #fff; padding: 6px 20px; border-radius: 6px;
  font-size: 13px; cursor: pointer; transition: background 0.15s;
}}
.tab-btn:hover  {{ background: rgba(255,255,255,0.2); }}
.tab-btn.active {{ background: rgba(255,255,255,0.9); color: #000; border-color: transparent; }}

/* ── PANELS ── */
.panel {{ display: none; width: 100vw; height: 100vh; }}
.panel.active {{ display: flex; align-items: center; justify-content: center; }}

/* ── STAGE (спільний) ── */
.stage {{
  width: 100vw; height: 100vh;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; cursor: grab; position: relative;
}}
.stage:active {{ cursor: grabbing; }}

/* ── ФОТО ── */
#main-img {{
  display: block; transform-origin: center center;
  user-select: none; -webkit-user-drag: none;
}}
#main-img.mode-fit    {{ max-width: 100vw; max-height: 100vh; width: auto; height: auto; }}
#main-img.mode-fill   {{ width: 100vw; height: 100vh; object-fit: cover; }}
#main-img.mode-custom {{ max-width: none; max-height: none; }}

/* ── СТРІМ ── */
#stream-img {{
  display: block; max-width: 100vw; max-height: 100vh;
  width: auto; height: auto;
  user-select: none; -webkit-user-drag: none;
  transform-origin: center center;
}}

/* ── ПОРОЖНІЙ СТАН ── */
.empty-state {{
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 16px; color: #444; font-size: 16px; text-align: center; padding: 20px;
}}
.empty-state svg {{ opacity: 0.25; }}

/* ── UI OVERLAY ── */
#ui-overlay {{
  position: fixed; inset: 0; pointer-events: none;
  transition: opacity 0.4s ease; z-index: 10;
}}
#ui-overlay.hidden {{ opacity: 0; }}
#ui-overlay * {{ pointer-events: auto; }}

/* ── ПАНЕЛЬ КЕРУВАННЯ ── */
#controls {{
  position: absolute; top: 0; left: 0; right: 0;
  padding: 52px 20px 12px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: linear-gradient(to bottom, rgba(0,0,0,0.8) 0%, transparent 100%);
}}

#file-name {{
  font-size: 14px; color: #ccc; margin-right: auto;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 280px;
}}
#counter {{ font-size: 13px; color: #888; white-space: nowrap; }}

/* ── КНОПКИ ── */
.btn {{
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.2);
  color: #fff; padding: 6px 14px; border-radius: 6px;
  font-size: 13px; cursor: pointer; transition: background 0.15s;
  white-space: nowrap; line-height: 1.4;
}}
.btn:hover  {{ background: rgba(255,255,255,0.25); }}
.btn.active {{ background: rgba(255,255,255,0.9); color: #000; border-color: transparent; }}

/* ── ПОВЗУНОК ── */
.slider-wrap {{ display: flex; align-items: center; gap: 8px; }}
.slider-label {{ font-size: 12px; color: #999; white-space: nowrap; }}
.slider-val   {{ font-size: 13px; color: #ccc; min-width: 44px; text-align: right; }}

input[type=range] {{
  -webkit-appearance: none; height: 4px;
  background: rgba(255,255,255,0.3); border-radius: 2px; outline: none; cursor: pointer;
}}
input[type=range].w100 {{ width: 100px; }}
input[type=range].w80  {{ width: 80px; }}
input[type=range]::-webkit-slider-thumb {{
  -webkit-appearance: none; width: 16px; height: 16px;
  background: #fff; border-radius: 50%;
}}

/* ── СТРІЛКИ ── */
.nav-arrow {{
  position: absolute; top: 50%; transform: translateY(-50%);
  background: rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.15);
  color: #fff; width: 48px; height: 64px; border-radius: 6px;
  font-size: 22px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s; user-select: none;
}}
.nav-arrow:hover {{ background: rgba(255,255,255,0.2); }}
#arrow-prev {{ left: 12px; }}
#arrow-next {{ right: 12px; }}

/* ── МІНІАТЮРИ ── */
#thumbnails-bar {{
  position: absolute; bottom: 0; left: 0; right: 0;
  padding: 10px 16px;
  background: linear-gradient(to top, rgba(0,0,0,0.8) 0%, transparent 100%);
  display: flex; gap: 8px; overflow-x: auto; scroll-behavior: smooth;
  scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.3) transparent;
}}
#thumbnails-bar::-webkit-scrollbar {{ height: 4px; }}
#thumbnails-bar::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.3); border-radius: 2px; }}

.thumb {{
  flex-shrink: 0; width: 80px; height: 60px; object-fit: cover;
  border-radius: 4px; cursor: pointer; opacity: 0.55;
  border: 2px solid transparent; transition: opacity 0.15s, border-color 0.15s;
}}
.thumb:hover  {{ opacity: 0.85; }}
.thumb.active {{ opacity: 1; border-color: #fff; }}

/* ── СТРІМ CONTROLS ── */
#stream-controls {{
  position: absolute; bottom: 0; left: 0; right: 0;
  padding: 12px 20px;
  background: linear-gradient(to top, rgba(0,0,0,0.8) 0%, transparent 100%);
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}}
#stream-status {{
  font-size: 12px; margin-left: auto;
  padding: 3px 10px; border-radius: 20px;
  background: rgba(255,255,255,0.1); color: #aaa;
}}
#stream-status.live {{ background: rgba(80,200,80,0.2); color: #6d6; }}
#no-stream-msg {{ color: #555; font-size: 14px; }}
</style>
</head>
<body>

<!-- Перемикач вкладок -->
<div id="tab-bar">
  <button class="tab-btn active" onclick="switchTab('stream')">📡 Трансляція</button>
  <button class="tab-btn"        onclick="switchTab('photos')">🖼 Фото</button>
</div>

<!-- ══ ПАНЕЛЬ ТРАНСЛЯЦІЇ ══ -->
<div class="panel active" id="panel-stream">
  <div class="stage" id="stream-stage">
    <div class="empty-state" id="stream-empty">
      <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="2" y="3" width="20" height="14" rx="2"/>
        <line x1="8" y1="21" x2="16" y2="21"/>
        <line x1="12" y1="17" x2="12" y2="21"/>
      </svg>
      <span id="stream-hint">Запустіть <strong>overlay.py</strong> щоб обрати область екрану</span>
    </div>
    <img id="stream-img" src="" alt="" style="display:none">
  </div>
</div>

<!-- ══ ПАНЕЛЬ ФОТО ══ -->
<div class="panel" id="panel-photos">
  <div class="stage" id="photo-stage">
    <div class="empty-state" id="photo-empty">
      <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="3" y="3" width="18" height="18" rx="2"/>
        <circle cx="8.5" cy="8.5" r="1.5"/>
        <polyline points="21 15 16 10 5 21"/>
      </svg>
      <span>Додайте фото у папку <strong>photo/</strong> і перезавантажте сторінку</span>
    </div>
    <img id="main-img" class="mode-fit" src="" alt="" style="display:none">
  </div>
</div>

<!-- ══ UI OVERLAY ══ -->
<div id="ui-overlay">

  <!-- Верхня панель керування (фото) -->
  <div id="controls">
    <span id="file-name">—</span>
    <span id="counter"></span>
    <button class="btn active" data-mode="fit">Вписати</button>
    <button class="btn" data-mode="fill">Заповнити</button>
    <button class="btn" data-scale="100">100%</button>
    <button class="btn" data-scale="150">150%</button>
    <button class="btn" data-scale="200">200%</button>
    <div class="slider-wrap">
      <input type="range" class="w100" id="scale-slider" min="10" max="400" value="100">
      <span class="slider-val" id="scale-label">100%</span>
    </div>
  </div>

  <!-- Стрілки навігації (фото) -->
  <button class="nav-arrow" id="arrow-prev">&#8249;</button>
  <button class="nav-arrow" id="arrow-next">&#8250;</button>

  <!-- Мініатюри (фото) -->
  <div id="thumbnails-bar"></div>

  <!-- Нижня панель стріму -->
  <div id="stream-controls">
    <div class="slider-wrap">
      <span class="slider-label">FPS</span>
      <input type="range" class="w80" id="fps-slider" min="1" max="10" value="{DEFAULT_FPS}">
      <span class="slider-val" id="fps-label">{DEFAULT_FPS}</span>
    </div>
    <div class="slider-wrap">
      <span class="slider-label">Якість</span>
      <input type="range" class="w80" id="qual-slider" min="10" max="95" value="{DEFAULT_QUALITY}">
      <span class="slider-val" id="qual-label">{DEFAULT_QUALITY}%</span>
    </div>
    <span id="stream-status">очікування...</span>
  </div>

</div><!-- /ui-overlay -->

<script>
// ── STATE ────────────────────────────────────────────────────────────────────
const STATE = {{
  // Загальне
  currentTab: 'stream',
  uiTimer:    null,

  // Фото
  photos:  [],
  index:   0,
  mode:    'fit',
  scale:   100,
  pan:     {{ x: 0, y: 0 }},
  drag:    {{ active: false, startX: 0, startY: 0, originX: 0, originY: 0 }},

  // Стрім
  streamActive: false,
  fpsTimer:     null,   // setInterval для оновлення кадрів
  fps:          {DEFAULT_FPS},
  quality:      {DEFAULT_QUALITY},
}};

// ── DOM CACHE ────────────────────────────────────────────────────────────────
const DOM = {{
  tabBar:       document.getElementById('tab-bar'),
  panelStream:  document.getElementById('panel-stream'),
  panelPhotos:  document.getElementById('panel-photos'),

  // Стрім
  streamStage:  document.getElementById('stream-stage'),
  streamImg:    document.getElementById('stream-img'),
  streamEmpty:  document.getElementById('stream-empty'),
  streamHint:   document.getElementById('stream-hint'),
  streamStatus: document.getElementById('stream-status'),
  streamCtrl:   document.getElementById('stream-controls'),
  fpsSlider:    document.getElementById('fps-slider'),
  fpsLabel:     document.getElementById('fps-label'),
  qualSlider:   document.getElementById('qual-slider'),
  qualLabel:    document.getElementById('qual-label'),

  // Фото
  photoStage:   document.getElementById('photo-stage'),
  mainImg:      document.getElementById('main-img'),
  photoEmpty:   document.getElementById('photo-empty'),
  fileName:     document.getElementById('file-name'),
  counter:      document.getElementById('counter'),
  controls:     document.getElementById('controls'),
  modeBtns:     document.querySelectorAll('[data-mode]'),
  scaleBtns:    document.querySelectorAll('[data-scale]'),
  scaleSlider:  document.getElementById('scale-slider'),
  scaleLabel:   document.getElementById('scale-label'),
  thumbsBar:    document.getElementById('thumbnails-bar'),
  arrowPrev:    document.getElementById('arrow-prev'),
  arrowNext:    document.getElementById('arrow-next'),

  overlay:      document.getElementById('ui-overlay'),
  tabBarEl:     document.getElementById('tab-bar'),
}};

// ── HELPERS: UI HIDE ─────────────────────────────────────────────────────────
function showUI() {{
  document.body.classList.add('ui-visible');
  DOM.overlay.classList.remove('hidden');
  DOM.tabBarEl.classList.remove('hidden');
  clearTimeout(STATE.uiTimer);
  STATE.uiTimer = setTimeout(hideUI, {UI_HIDE_DELAY_MS});
}}
function hideUI() {{
  document.body.classList.remove('ui-visible');
  DOM.overlay.classList.add('hidden');
  DOM.tabBarEl.classList.add('hidden');
}}

// ── HELPERS: TABS ────────────────────────────────────────────────────────────
function switchTab(tab) {{
  STATE.currentTab = tab;

  DOM.panelStream.classList.toggle('active', tab === 'stream');
  DOM.panelPhotos.classList.toggle('active', tab === 'photos');

  // Показати/сховати панелі UI відповідно до вкладки
  DOM.controls.style.display      = tab === 'photos' ? '' : 'none';
  DOM.arrowPrev.style.display     = tab === 'photos' ? '' : 'none';
  DOM.arrowNext.style.display     = tab === 'photos' ? '' : 'none';
  DOM.thumbsBar.style.display     = tab === 'photos' ? '' : 'none';
  DOM.streamCtrl.style.display    = tab === 'stream' ? '' : 'none';

  // Оновити кнопки вкладок
  document.querySelectorAll('.tab-btn').forEach((b, i) => {{
    b.classList.toggle('active', (i === 0) === (tab === 'stream'));
  }});

  if (tab === 'stream') startStream();
  else stopStream();

  showUI();
}}

// ── HELPERS: PAN ─────────────────────────────────────────────────────────────
function resetPan() {{
  STATE.pan = {{ x: 0, y: 0 }};
  applyPanTransform();
}}
function applyPanTransform() {{
  DOM.mainImg.style.transform = `translate(${{STATE.pan.x}}px, ${{STATE.pan.y}}px)`;
}}

// ── HELPERS: SCALE ───────────────────────────────────────────────────────────
function applyCustomScale(pct) {{
  DOM.mainImg.className       = 'mode-custom';
  DOM.mainImg.style.width     = pct + 'vw';
  DOM.mainImg.style.height    = '';
  DOM.scaleSlider.value       = pct;
  DOM.scaleLabel.textContent  = pct + '%';
  STATE.scale = pct; STATE.mode = 'custom';
  updateModeButtons(null);
}}
function updateModeButtons(activeMode) {{
  DOM.modeBtns.forEach(b => b.classList.toggle('active', b.dataset.mode === activeMode));
}}
function applyMode(mode) {{
  DOM.mainImg.className    = 'mode-' + mode;
  DOM.mainImg.style.width  = '';
  DOM.mainImg.style.height = '';
  STATE.mode = mode;
  updateModeButtons(mode);
  resetPan();
  DOM.scaleSlider.value      = 100;
  DOM.scaleLabel.textContent = '100%';
}}

// ── API ──────────────────────────────────────────────────────────────────────
async function fetchPhotos() {{
  try {{
    const r = await fetch('/api/photos');
    const d = await r.json();
    return d.photos || [];
  }} catch(e) {{ console.error('[fetchPhotos]', e); return []; }}
}}

async function pushStreamSettings(fps, quality) {{
  try {{
    await fetch('/api/stream-settings', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ fps, quality }})
    }});
  }} catch(e) {{ console.error('[pushStreamSettings]', e); }}
}}

// ── STREAM ───────────────────────────────────────────────────────────────────
function startStream() {{
  if (STATE.streamActive) return;
  STATE.streamActive = true;

  // Перевіряємо чи є стрім на сервері
  fetch('/api/stream-info').then(r => r.json()).then(info => {{
    if (!info.available) {{
      DOM.streamHint.innerHTML = info.message || 'Стрім недоступний';
      return;
    }}
    // Показуємо зображення — браузер сам отримує MJPEG
    DOM.streamEmpty.style.display = 'none';
    DOM.streamImg.style.display   = 'block';
    DOM.streamImg.src = '/stream?' + Date.now();

    DOM.streamImg.onload = () => {{
      DOM.streamStatus.textContent = '● LIVE';
      DOM.streamStatus.classList.add('live');
    }};
    DOM.streamImg.onerror = () => {{
      DOM.streamStatus.textContent = 'помилка';
      DOM.streamStatus.classList.remove('live');
    }};
  }}).catch(() => {{
    DOM.streamHint.textContent = 'Сервер не відповідає';
  }});
}}

function stopStream() {{
  STATE.streamActive = false;
  DOM.streamImg.src  = '';
  DOM.streamImg.style.display   = 'none';
  DOM.streamEmpty.style.display = '';
  DOM.streamStatus.textContent  = 'очікування...';
  DOM.streamStatus.classList.remove('live');
}}

// ── PHOTOS UI ────────────────────────────────────────────────────────────────
function showPhoto(index) {{
  if (!STATE.photos.length) return;
  STATE.index = (index + STATE.photos.length) % STATE.photos.length;
  const p = STATE.photos[STATE.index];

  DOM.mainImg.src            = p.url;
  DOM.mainImg.style.display  = 'block';
  DOM.photoEmpty.style.display = 'none';
  resetPan();

  DOM.fileName.textContent = p.name;
  DOM.counter.textContent  = `${{STATE.index + 1}} / ${{STATE.photos.length}}`;

  Array.from(DOM.thumbsBar.children).forEach((el, i) =>
    el.classList.toggle('active', i === STATE.index));

  const thumb = DOM.thumbsBar.children[STATE.index];
  if (thumb) thumb.scrollIntoView({{ inline: 'nearest', behavior: 'smooth' }});

  showUI();
}}

function buildThumbnails(photos) {{
  DOM.thumbsBar.innerHTML = '';
  photos.forEach((p, i) => {{
    const img     = document.createElement('img');
    img.src       = p.url;
    img.title     = p.name;
    img.className = 'thumb' + (i === 0 ? ' active' : '');
    img.addEventListener('click', () => showPhoto(i));
    DOM.thumbsBar.appendChild(img);
  }});
}}

// ── DRAG-TO-PAN (фото) ───────────────────────────────────────────────────────
DOM.photoStage.addEventListener('mousedown', e => {{
  if (e.button !== 0) return;
  STATE.drag = {{ active: true, startX: e.clientX, startY: e.clientY,
                  originX: STATE.pan.x, originY: STATE.pan.y }};
  DOM.photoStage.style.cursor = 'grabbing';
  e.preventDefault();
}});

document.addEventListener('mousemove', e => {{
  if (STATE.drag.active) {{
    STATE.pan.x = STATE.drag.originX + (e.clientX - STATE.drag.startX);
    STATE.pan.y = STATE.drag.originY + (e.clientY - STATE.drag.startY);
    applyPanTransform();
    return;
  }}
  showUI();
}});

document.addEventListener('mouseup', () => {{
  STATE.drag.active = false;
  DOM.photoStage.style.cursor = '';
}});

DOM.photoStage.addEventListener('mouseenter', () => {{
  if (!STATE.drag.active) DOM.photoStage.style.cursor = 'grab';
}});
DOM.photoStage.addEventListener('mouseleave', () => {{
  DOM.photoStage.style.cursor = '';
}});

// ── ПОДІЇ ────────────────────────────────────────────────────────────────────
DOM.modeBtns.forEach(b => b.addEventListener('click', () => applyMode(b.dataset.mode)));
DOM.scaleBtns.forEach(b => b.addEventListener('click', () => applyCustomScale(+b.dataset.scale)));
DOM.scaleSlider.addEventListener('input', () => applyCustomScale(+DOM.scaleSlider.value));
DOM.arrowPrev.addEventListener('click', () => showPhoto(STATE.index - 1));
DOM.arrowNext.addEventListener('click', () => showPhoto(STATE.index + 1));

// FPS повзунок
DOM.fpsSlider.addEventListener('input', () => {{
  STATE.fps = +DOM.fpsSlider.value;
  DOM.fpsLabel.textContent = STATE.fps;
  pushStreamSettings(STATE.fps, STATE.quality);
}});

// Якість повзунок
DOM.qualSlider.addEventListener('input', () => {{
  STATE.quality = +DOM.qualSlider.value;
  DOM.qualLabel.textContent = STATE.quality + '%';
  pushStreamSettings(STATE.fps, STATE.quality);
}});

// Клавіатура
document.addEventListener('keydown', e => {{
  if (STATE.currentTab === 'photos') {{
    if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   showPhoto(STATE.index - 1);
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown')  showPhoto(STATE.index + 1);
    if (e.key === 'r' || e.key === 'R') resetPan();
  }}
  if (e.key === 'Escape') showUI();
}});

document.addEventListener('touchstart', showUI);

// ── ІНІЦІАЛІЗАЦІЯ ────────────────────────────────────────────────────────────
(async () => {{
  // Завантажити фото
  const photos = await fetchPhotos();
  STATE.photos = photos;
  if (photos.length) {{
    buildThumbnails(photos);
    showPhoto(0);
  }}

  // Стартуємо на вкладці трансляції
  switchTab('stream');
  showUI();
}})();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# АВТОРИЗАЦІЯ
# ══════════════════════════════════════════════════════════════════════════════

def _make_token() -> str:
    """Генерує унікальний сесійний токен."""
    return secrets.token_hex(32)

def _check_token(cookie_header: str) -> bool:
    """Перевіряє наявність валідного токену в Cookie заголовку."""
    if not cookie_header:
        return False
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("auth="):
            token = part[5:].strip()
            return token in _valid_tokens
    return False

def _set_auth_cookie(token: str) -> str:
    """Повертає рядок Set-Cookie з токеном і терміном дії."""
    max_age = TOKEN_COOKIE_DAYS * 24 * 3600
    return f"auth={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"


# ══════════════════════════════════════════════════════════════════════════════
# MJPEG СТРІМ
# ══════════════════════════════════════════════════════════════════════════════

def _capture_frame() -> bytes | None:
    """
    Знімає кадр з області визначеної в capture.json.
    Повертає JPEG bytes або None при помилці.
    """
    if not STREAM_OK:
        return None
    try:
        # Читаємо координати рамки
        if not CAPTURE_CONFIG.exists():
            return None
        with open(CAPTURE_CONFIG) as f:
            cfg = json.load(f)

        region = {{
            "left":   int(cfg["x"]),
            "top":    int(cfg["y"]),
            "width":  int(cfg["width"]),
            "height": int(cfg["height"]),
        }}

        with _stream_lock:
            quality = _stream_quality

        with mss.mss() as sct:
            shot = sct.grab(region)
            img  = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        return buf.getvalue()

    except Exception as e:
        print(f"[_capture_frame] Помилка: {{e}}")
        return None


def _stream_generator(handler):
    """
    Генератор MJPEG стріму. Надсилає кадри поки з'єднання активне.
    """
    boundary = b"--frame"
    while True:
        try:
            with _stream_lock:
                fps = _stream_fps
            delay = 1.0 / max(fps, 1)

            frame = _capture_frame()
            if frame is None:
                time.sleep(0.5)
                continue

            header = (
                boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
            )
            handler.wfile.write(header + frame + b"\r\n")
            handler.wfile.flush()
            time.sleep(delay)

        except (BrokenPipeError, ConnectionResetError):
            break
        except Exception as e:
            print(f"[_stream_generator] Помилка: {{e}}")
            break


# ══════════════════════════════════════════════════════════════════════════════
# HTTP HANDLER
# ══════════════════════════════════════════════════════════════════════════════

class ProjectorHandler(http.server.BaseHTTPRequestHandler):
    """Обробляє всі HTTP-запити: авторизація, стрім, фото, API."""

    # ── Маршрутизація ──────────────────────────────────────────────────────────

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            cookie = self.headers.get("Cookie", "")

            # Публічний маршрут: сторінка логіну
            if path == "/login":
                self._send_html(HTML_LOGIN)
                return

            # Захищені маршрути: перевіряємо токен
            if not _check_token(cookie):
                self._redirect("/login")
                return

            if path in ("/", "/index.html"):
                self._send_html(HTML_MAIN)
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
            print(f"[ProjectorHandler.do_GET] Помилка: {{e}}")

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path

            if path == "/api/login":
                self._api_login()
            elif path == "/api/stream-settings":
                cookie = self.headers.get("Cookie", "")
                if _check_token(cookie):
                    self._api_stream_settings()
                else:
                    self._send_json({{"error": "unauthorized"}}, 401)
            else:
                self._send_404()

        except Exception as e:
            print(f"[ProjectorHandler.do_POST] Помилка: {{e}}")

    # ── API ────────────────────────────────────────────────────────────────────

    def _api_login(self):
        """POST /api/login — перевірка пароля, видача cookie."""
        try:
            length  = int(self.headers.get("Content-Length", 0))
            body    = json.loads(self.rfile.read(length))
            pw      = body.get("password", "")

            if pw == PASSWORD:
                token = _make_token()
                _valid_tokens.add(token)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", _set_auth_cookie(token))
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{{}}")
                print(f"  ✓ Новий вхід, токен виданий")
            else:
                self._send_json({{"error": "wrong password"}}, 401)
                print(f"  ✗ Невірний пароль")

        except Exception as e:
            print(f"[ProjectorHandler._api_login] Помилка: {{e}}")
            self._send_json({{"error": str(e)}}, 500)

    def _api_stream_info(self):
        """GET /api/stream-info — чи доступний стрім."""
        if not STREAM_OK:
            msg = "Встановіть бібліотеки: pip install mss pillow"
            self._send_json({{"available": False, "message": msg}})
            return
        if not CAPTURE_CONFIG.exists():
            self._send_json({{
                "available": False,
                "message": "Запустіть <strong>overlay.py</strong> і оберіть область захвату"
            }})
            return
        self._send_json({{"available": True}})

    def _api_stream_settings(self):
        """POST /api/stream-settings — змінює FPS і якість."""
        global _stream_fps, _stream_quality
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            with _stream_lock:
                _stream_fps     = max(1, min(10, int(body.get("fps", _stream_fps))))
                _stream_quality = max(10, min(95, int(body.get("quality", _stream_quality))))
            self._send_json({{"fps": _stream_fps, "quality": _stream_quality}})
        except Exception as e:
            print(f"[_api_stream_settings] Помилка: {{e}}")
            self._send_json({{"error": str(e)}}, 500)

    def _api_photos(self):
        """GET /api/photos — список фото з папки."""
        try:
            if not PHOTOS_DIR.exists():
                PHOTOS_DIR.mkdir()

            photos = []
            for f in sorted(PHOTOS_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT:
                    photos.append({{
                        "name": f.name,
                        "url":  f"/photos/{{urllib.parse.quote(f.name)}}"
                    }})
            self._send_json({{"photos": photos}})
        except Exception as e:
            print(f"[_api_photos] Помилка: {{e}}")
            self._send_json({{"photos": []}})

    # ── СТРІМ ──────────────────────────────────────────────────────────────────

    def _handle_stream(self):
        """GET /stream — MJPEG стрім."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        _stream_generator(self)

    # ── СТАТИКА ────────────────────────────────────────────────────────────────

    def _send_photo(self, url_path: str):
        """GET /photos/<name> — відправити файл зображення."""
        try:
            filename  = urllib.parse.unquote(url_path[len("/photos/"):])
            file_path = PHOTOS_DIR / filename
            if not file_path.is_file():
                self._send_404(); return

            mime_map = {{
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",  ".gif": "image/gif",
                ".bmp": "image/bmp",  ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }}
            mime = mime_map.get(file_path.suffix.lower(), "application/octet-stream")
            data = file_path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"[_send_photo] Помилка: {{e}}")
            self._send_404()

    # ── УТИЛІТИ ────────────────────────────────────────────────────────────────

    def _send_html(self, content: str):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: dict, status: int = 200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _send_404(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        # Фільтруємо шум від фото і кадрів стріму
        if "/photos/" not in args[0] and "/stream" not in args[0]:
            print(f"  {{args[0]}} → {{args[1]}}")


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДУ
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Визначити локальну IP адресу
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    # Переконатись що папка фото існує
    if not PHOTOS_DIR.exists():
        PHOTOS_DIR.mkdir()

    photo_count = sum(
        1 for f in PHOTOS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
    )

    print("=" * 50)
    print("  🎨  Projector Server")
    print("=" * 50)
    print(f"  Пароль входу : {PASSWORD}")
    print(f"  Локальна мережа: http://{local_ip}:{PORT}")
    print(f"  Localhost      : http://localhost:{PORT}")
    print(f"  Фото в папці   : {photo_count} шт.")
    print(f"  Стрім          : {'✓ готовий (mss + pillow)' if STREAM_OK else '✗ pip install mss pillow'}")
    if not STREAM_OK:
        missing = []
        if not MSS_OK: missing.append("mss")
        if not PIL_OK: missing.append("pillow")
        print(f"  Встановіть    : pip install {' '.join(missing)}")
    print("=" * 50)
    print("  Зупинити: Ctrl+C")
    print()

    server = http.server.HTTPServer(("", PORT), ProjectorHandler)
    server.allow_reuse_address = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер зупинено.")
