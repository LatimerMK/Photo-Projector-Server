"""
Photo Projector Server
Запускає локальний веб-сервер для показу фото з папки /photos на проектор.
Відкрий http://localhost:8080 у браузері після запуску.
"""

import http.server
import json
import os
import urllib.parse
from pathlib import Path

# ── КОНФІГУРАЦІЯ ──────────────────────────────────────────────────────────────
PORT = 8080                        # Порт сервера
PHOTOS_DIR = Path("photos")         # Папка з фотографіями (photo або photos)
SUPPORTED_EXTENSIONS = {           # Підтримувані формати зображень
    ".jpg", ".jpeg", ".png",
    ".gif", ".bmp", ".webp", ".svg"
}
UI_HIDE_DELAY_MS = 3000            # Мс до приховування UI при бездіяльності

# ── HTML СТОРІНКА ─────────────────────────────────────────────────────────────
HTML = f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Фото Проектор</title>
<style>
  /* ── RESET & BASE ── */
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: #000;
    color: #fff;
    font-family: 'Segoe UI', system-ui, sans-serif;
    overflow: hidden;
    height: 100vh;
    width: 100vw;
    cursor: none;
  }}

  body.ui-visible {{ cursor: default; }}

  /* ── ГОЛОВНЕ ЗОБРАЖЕННЯ ── */
  #stage {{
    width: 100vw;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: grab;
  }}

  #stage:active {{ cursor: grabbing; }}

  #main-img {{
    display: block;
    transform-origin: center center;
    user-select: none;
    -webkit-user-drag: none;
  }}

  /* Режими масштабу */
  #main-img.mode-fit   {{ max-width: 100vw; max-height: 100vh; width: auto; height: auto; }}
  #main-img.mode-fill  {{ width: 100vw; height: 100vh; object-fit: cover; }}
  #main-img.mode-custom {{ max-width: none; max-height: none; }}

  /* ── ПОРОЖНІЙ СТАН ── */
  #empty-state {{
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    color: #444;
    font-size: 18px;
  }}

  #empty-state svg {{ opacity: 0.3; }}

  /* ── UI OVERLAY ── */
  #ui-overlay {{
    position: fixed;
    inset: 0;
    pointer-events: none;
    transition: opacity 0.4s ease;
    z-index: 10;
  }}

  #ui-overlay.hidden {{ opacity: 0; }}

  /* Всі інтерактивні елементи всередині overlay */
  #ui-overlay * {{ pointer-events: auto; }}

  /* ── ПАНЕЛЬ КЕРУВАННЯ ── */
  #controls {{
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    background: linear-gradient(to bottom, rgba(0,0,0,0.75) 0%, transparent 100%);
    flex-wrap: wrap;
  }}

  /* Назва файлу */
  #file-name {{
    font-size: 14px;
    color: #ccc;
    margin-right: auto;
    letter-spacing: 0.03em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 300px;
  }}

  /* Лічильник */
  #counter {{
    font-size: 13px;
    color: #888;
    white-space: nowrap;
  }}

  /* ── КНОПКИ ── */
  .btn {{
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.2);
    color: #fff;
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
    transition: background 0.15s;
    white-space: nowrap;
    line-height: 1.4;
  }}

  .btn:hover {{ background: rgba(255,255,255,0.25); }}
  .btn.active {{
    background: rgba(255,255,255,0.9);
    color: #000;
    border-color: transparent;
  }}

  /* ── ПОВЗУНОК МАСШТАБУ ── */
  #scale-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  #scale-label {{
    font-size: 13px;
    color: #ccc;
    min-width: 44px;
    text-align: right;
  }}

  input[type=range] {{
    -webkit-appearance: none;
    width: 120px;
    height: 4px;
    background: rgba(255,255,255,0.3);
    border-radius: 2px;
    outline: none;
    cursor: pointer;
  }}

  input[type=range]::-webkit-slider-thumb {{
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    background: #fff;
    border-radius: 50%;
  }}

  /* ── СТРІЛКИ НАВІГАЦІЇ ── */
  .nav-arrow {{
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    background: rgba(0,0,0,0.5);
    border: 1px solid rgba(255,255,255,0.15);
    color: #fff;
    width: 48px;
    height: 64px;
    border-radius: 6px;
    font-size: 22px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s;
    user-select: none;
  }}

  .nav-arrow:hover {{ background: rgba(255,255,255,0.2); }}
  #arrow-prev {{ left: 12px; }}
  #arrow-next {{ right: 12px; }}

  /* ── СТРІЧКА МІНІАТЮР ── */
  #thumbnails-bar {{
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 10px 16px;
    background: linear-gradient(to top, rgba(0,0,0,0.8) 0%, transparent 100%);
    display: flex;
    gap: 8px;
    overflow-x: auto;
    scroll-behavior: smooth;
    scrollbar-width: thin;
    scrollbar-color: rgba(255,255,255,0.3) transparent;
  }}

  #thumbnails-bar::-webkit-scrollbar {{ height: 4px; }}
  #thumbnails-bar::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.3); border-radius: 2px; }}

  .thumb {{
    flex-shrink: 0;
    width: 80px;
    height: 60px;
    object-fit: cover;
    border-radius: 4px;
    cursor: pointer;
    opacity: 0.55;
    border: 2px solid transparent;
    transition: opacity 0.15s, border-color 0.15s;
  }}

  .thumb:hover {{ opacity: 0.85; }}
  .thumb.active {{ opacity: 1; border-color: #fff; }}
</style>
</head>
<body>

<!-- Головна сцена -->
<div id="stage">
  <div id="empty-state">
    <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
      <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>
      <polyline points="21 15 16 10 5 21"/>
    </svg>
    <span>Додайте фото у папку <strong>photos/</strong> і перезавантажте сторінку</span>
  </div>
  <img id="main-img" class="mode-fit" src="" alt="" style="display:none">
</div>

<!-- UI шар (ховається при бездіяльності) -->
<div id="ui-overlay">

  <!-- Верхня панель -->
  <div id="controls">
    <span id="file-name">—</span>
    <span id="counter"></span>

    <!-- Кнопки масштабу -->
    <button class="btn active" data-mode="fit">Вписати</button>
    <button class="btn" data-mode="fill">Заповнити</button>
    <button class="btn" data-scale="100">100%</button>
    <button class="btn" data-scale="150">150%</button>
    <button class="btn" data-scale="200">200%</button>

    <!-- Повзунок -->
    <div id="scale-wrap">
      <input type="range" id="scale-slider" min="10" max="400" value="100">
      <span id="scale-label">100%</span>
    </div>
  </div>

  <!-- Стрілки навігації -->
  <button class="nav-arrow" id="arrow-prev">&#8249;</button>
  <button class="nav-arrow" id="arrow-next">&#8250;</button>

  <!-- Стрічка мініатюр -->
  <div id="thumbnails-bar" id="thumbnails-bar"></div>

</div>

<script>
// ── STATE ────────────────────────────────────────────────────────────────────
const STATE = {{
  photos:   [],        // масив {{name, url}}
  index:    0,         // поточний індекс
  mode:     'fit',     // 'fit' | 'fill' | 'custom'
  scale:    100,       // масштаб у % (лише для custom)
  uiTimer:  null,      // таймер приховування UI

  // Drag-to-pan
  pan:      {{ x: 0, y: 0 }},   // поточне зміщення px
  drag:     {{                   // стан перетягування
    active: false,
    startX: 0,
    startY: 0,
    originX: 0,
    originY: 0,
  }},
}};

// ── HELPERS ──────────────────────────────────────────────────────────────────

/** Скидає pan-позицію до центру */
function resetPan() {{
  STATE.pan = {{ x: 0, y: 0 }};
  applyPanTransform();
}}

/** Застосовує translate до зображення */
function applyPanTransform() {{
  DOM.mainImg.style.transform = `translate(${{STATE.pan.x}}px, ${{STATE.pan.y}}px)`;
}}

/** Встановлює масштаб зображення в режимі custom */
function applyCustomScale(pct) {{
  const img = DOM.mainImg;
  img.className = 'mode-custom';
  img.style.width  = pct + 'vw';
  img.style.height = '';
  DOM.scaleSlider.value = pct;
  DOM.scaleLabel.textContent = pct + '%';
  STATE.scale = pct;
  STATE.mode = 'custom';
  updateModeButtons(null);
  // Не скидаємо pan — зручно підлаштовувати масштаб без зсуву
}}

/** Оновлює активну кнопку режиму */
function updateModeButtons(activeMode) {{
  DOM.modeBtns.forEach(btn => {{
    btn.classList.toggle('active', btn.dataset.mode === activeMode);
  }});
}}

/** Показати UI і перезапустити таймер приховування */
function showUI() {{
  document.body.classList.add('ui-visible');
  DOM.overlay.classList.remove('hidden');
  clearTimeout(STATE.uiTimer);
  STATE.uiTimer = setTimeout(hideUI, {UI_HIDE_DELAY_MS});
}}

function hideUI() {{
  document.body.classList.remove('ui-visible');
  DOM.overlay.classList.add('hidden');
}}

/** Прокрутити стрічку до активної мініатюри */
function scrollThumbIntoView(index) {{
  const thumb = DOM.thumbsBar.children[index];
  if (thumb) thumb.scrollIntoView({{ inline: 'nearest', behavior: 'smooth' }});
}}

// ── API ──────────────────────────────────────────────────────────────────────

/** Отримує список фото з сервера */
async function fetchPhotos() {{
  try {{
    const res  = await fetch('/api/photos');
    const data = await res.json();
    return data.photos || [];
  }} catch (e) {{
    console.error('[fetchPhotos] Помилка:', e);
    return [];
  }}
}}

// ── UI ───────────────────────────────────────────────────────────────────────

// Кешування DOM-елементів
const DOM = {{
  stage:       document.getElementById('stage'),
  emptyState:  document.getElementById('empty-state'),
  mainImg:     document.getElementById('main-img'),
  overlay:     document.getElementById('ui-overlay'),
  fileName:    document.getElementById('file-name'),
  counter:     document.getElementById('counter'),
  modeBtns:    document.querySelectorAll('[data-mode]'),
  scaleBtns:   document.querySelectorAll('[data-scale]'),
  scaleSlider: document.getElementById('scale-slider'),
  scaleLabel:  document.getElementById('scale-label'),
  thumbsBar:   document.getElementById('thumbnails-bar'),
  arrowPrev:   document.getElementById('arrow-prev'),
  arrowNext:   document.getElementById('arrow-next'),
}};

/** Показати фото за індексом */
function showPhoto(index) {{
  const photos = STATE.photos;
  if (!photos.length) return;

  STATE.index = (index + photos.length) % photos.length;
  const photo = photos[STATE.index];

  // Оновити зображення і скинути позицію
  DOM.mainImg.src = photo.url;
  DOM.mainImg.style.display = 'block';
  DOM.emptyState.style.display = 'none';
  resetPan();

  // Оновити підпис і лічильник
  DOM.fileName.textContent = photo.name;
  DOM.counter.textContent  = `${{STATE.index + 1}} / ${{photos.length}}`;

  // Оновити мініатюри
  Array.from(DOM.thumbsBar.children).forEach((el, i) => {{
    el.classList.toggle('active', i === STATE.index);
  }});
  scrollThumbIntoView(STATE.index);

  showUI();
}}

/** Побудувати стрічку мініатюр */
function buildThumbnails(photos) {{
  DOM.thumbsBar.innerHTML = '';
  photos.forEach((photo, i) => {{
    const img = document.createElement('img');
    img.src       = photo.url;
    img.title     = photo.name;
    img.className = 'thumb' + (i === 0 ? ' active' : '');
    img.addEventListener('click', () => showPhoto(i));
    DOM.thumbsBar.appendChild(img);
  }});
}}

/** Застосувати режим масштабу fit/fill */
function applyMode(mode) {{
  const img = DOM.mainImg;
  img.className    = 'mode-' + mode;
  img.style.width  = '';
  img.style.height = '';
  STATE.mode = mode;
  updateModeButtons(mode);
  resetPan();

  DOM.scaleSlider.value      = 100;
  DOM.scaleLabel.textContent = '100%';
}}

// ── ПОДІЇ: DRAG-TO-PAN ───────────────────────────────────────────────────────

DOM.stage.addEventListener('mousedown', e => {{
  // Тільки ліва кнопка, не на елементах UI
  if (e.button !== 0) return;
  STATE.drag.active  = true;
  STATE.drag.startX  = e.clientX;
  STATE.drag.startY  = e.clientY;
  STATE.drag.originX = STATE.pan.x;
  STATE.drag.originY = STATE.pan.y;
  DOM.stage.style.cursor = 'grabbing';
  e.preventDefault();
}});

document.addEventListener('mousemove', e => {{
  if (STATE.drag.active) {{
    // Перетягування: оновлюємо pan
    STATE.pan.x = STATE.drag.originX + (e.clientX - STATE.drag.startX);
    STATE.pan.y = STATE.drag.originY + (e.clientY - STATE.drag.startY);
    applyPanTransform();
    // Не показуємо UI під час drag
    return;
  }}
  // Звичайний рух миші → показати UI
  showUI();
}});

document.addEventListener('mouseup', e => {{
  if (!STATE.drag.active) return;
  STATE.drag.active      = false;
  DOM.stage.style.cursor = '';
}});

// Скасування drag якщо миша вийшла з вікна
document.addEventListener('mouseleave', () => {{
  STATE.drag.active      = false;
  DOM.stage.style.cursor = '';
}});

// Курсор grab на stage (коли не dragging)
DOM.stage.addEventListener('mouseenter', () => {{
  if (!STATE.drag.active) DOM.stage.style.cursor = 'grab';
}});
DOM.stage.addEventListener('mouseleave', () => {{
  DOM.stage.style.cursor = '';
}});

// ── ПОДІЇ: НАВІГАЦІЯ ─────────────────────────────────────────────────────────

DOM.modeBtns.forEach(btn => {{
  btn.addEventListener('click', () => applyMode(btn.dataset.mode));
}});

DOM.scaleBtns.forEach(btn => {{
  btn.addEventListener('click', () => applyCustomScale(Number(btn.dataset.scale)));
}});

DOM.scaleSlider.addEventListener('input', () => {{
  applyCustomScale(Number(DOM.scaleSlider.value));
}});

DOM.arrowPrev.addEventListener('click', () => showPhoto(STATE.index - 1));
DOM.arrowNext.addEventListener('click', () => showPhoto(STATE.index + 1));

document.addEventListener('keydown', e => {{
  if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')    showPhoto(STATE.index - 1);
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown')   showPhoto(STATE.index + 1);
  // R — скинути pan до центру
  if (e.key === 'r' || e.key === 'R') resetPan();
  if (e.key === 'Escape') showUI();
}});

document.addEventListener('touchstart', showUI);

// ── ІНІЦІАЛІЗАЦІЯ ────────────────────────────────────────────────────────────
(async () => {{
  const photos = await fetchPhotos();
  STATE.photos = photos;

  if (photos.length) {{
    buildThumbnails(photos);
    showPhoto(0);
  }} else {{
    DOM.arrowPrev.style.display = 'none';
    DOM.arrowNext.style.display = 'none';
  }}

  showUI();
}})();
</script>
</body>
</html>
"""


# ── СЕРВЕР ────────────────────────────────────────────────────────────────────

class PhotoHandler(http.server.BaseHTTPRequestHandler):
    """Обробляє HTTP-запити: головна сторінка, API і статичні файли."""

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/" or path == "/index.html":
                self._send_html(HTML)

            elif path == "/api/photos":
                self._send_json_photos()

            elif path.startswith("/photos/"):
                self._send_photo(path)

            else:
                self._send_404()

        except Exception as e:
            print(f"[PhotoHandler.do_GET] Помилка: {e}")
            self._send_404()

    def _send_html(self, content: str):
        """Відправити HTML-сторінку."""
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _send_json_photos(self):
        """Повернути JSON-список фото з папки photos/."""
        try:
            if not PHOTOS_DIR.exists():
                PHOTOS_DIR.mkdir()
                print(f"[PhotoHandler] Створено папку: {PHOTOS_DIR.resolve()}")

            photos = []
            for f in sorted(PHOTOS_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                    photos.append({
                        "name": f.name,
                        "url": f"/photos/{urllib.parse.quote(f.name)}"
                    })

            data = json.dumps({"photos": photos}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[PhotoHandler._send_json_photos] Помилка: {e}")
            self._send_404()

    def _send_photo(self, url_path: str):
        """Відправити файл зображення."""
        try:
            filename = urllib.parse.unquote(url_path[len("/photos/"):])
            file_path = PHOTOS_DIR / filename

            if not file_path.exists() or not file_path.is_file():
                self._send_404()
                return

            ext = file_path.suffix.lower()
            mime_map = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",  ".gif": "image/gif",
                ".bmp": "image/bmp",  ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }
            mime = mime_map.get(ext, "application/octet-stream")

            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[PhotoHandler._send_photo] Помилка: {e}")
            self._send_404()

    def _send_404(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        # Прибираємо стандартні логи GET-запитів до фото (шум)
        if "/photos/" not in args[0]:
            print(f"  {args[0]} → {args[1]}")


# ── ТОЧКА ВХОДУ ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Переконатись що папка photos існує
    if not PHOTOS_DIR.exists():
        PHOTOS_DIR.mkdir()
        print(f"✓ Створено папку: {PHOTOS_DIR.resolve()}")
    else:
        count = sum(
            1 for f in PHOTOS_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        print(f"✓ Папка photos/ — знайдено {count} фото")

    print(f"✓ Сервер запущено: http://localhost:{PORT}")
    print(f"  Клавіші: ← → для навігації, миша для показу панелі")
    print(f"  Зупинити: Ctrl+C\n")

    server = http.server.HTTPServer(("", PORT), PhotoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер зупинено.")