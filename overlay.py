"""
Overlay — вибір області захвату екрану
Запусти окремо від server.py.
Червона рамка поверх усіх вікон — рухай і змінюй розмір мишкою.
Координати зберігаються в capture.json і одразу підхоплюються сервером.
"""

import json
import tkinter as tk
from pathlib import Path

# ── КОНФІГУРАЦІЯ ──────────────────────────────────────────────────────────────
CAPTURE_CONFIG  = Path("capture.json")  # Файл координат для server.py
BORDER_COLOR    = "#ff2222"             # Колір рамки
BORDER_WIDTH    = 3                     # Товщина рамки px
HANDLE_SIZE     = 18                    # Розмір куточків px
MIN_WIDTH       = 100                   # Мінімальна ширина
MIN_HEIGHT      = 80                    # Мінімальна висота
SAVE_DELAY_MS   = 80                    # Затримка збереження після руху ms
DEFAULT_X       = 100
DEFAULT_Y       = 100
DEFAULT_W       = 800
DEFAULT_H       = 600


class CaptureOverlay:
    """
    Прозоре вікно поверх усього з червоною рамкою.
    Motion і release прив'язані до document (root), а не до куточків —
    щоб ресайз не переривався при виході миші за межі handle.
    """

    def __init__(self, root: tk.Tk):
        self.root = root

        # Режим взаємодії: None | 'drag' | 'resize'
        self._mode      = None
        self._corner    = None          # 'nw' | 'ne' | 'sw' | 'se'

        # Знімок стану на момент натискання
        self._start_mx  = 0            # mouse x при натисканні
        self._start_my  = 0            # mouse y при натисканні
        self._start_wx  = 0            # window x при натисканні
        self._start_wy  = 0            # window y при натисканні
        self._start_ww  = 0            # window width при натисканні
        self._start_wh  = 0            # window height при натисканні

        self._save_job  = None         # pending after() для збереження

        self._setup_window()
        self._build_canvas()
        self._bind_events()
        self._load_config()
        self._save_config()

    # ── ВІКНО ─────────────────────────────────────────────────────────────────

    def _setup_window(self):
        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-transparentcolor", "black")
        r.configure(bg="black")
        r.geometry(str(DEFAULT_W) + "x" + str(DEFAULT_H) + "+" + str(DEFAULT_X) + "+" + str(DEFAULT_Y))

    def _build_canvas(self):
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._draw_frame()

    # ── МАЛЮВАННЯ РАМКИ ───────────────────────────────────────────────────────

    def _draw_frame(self):
        """Перемальовує рамку і куточки. Викликається при кожній зміні розміру."""
        self.canvas.delete("all")
        w = self.root.winfo_width()  or DEFAULT_W
        h = self.root.winfo_height() or DEFAULT_H
        b = BORDER_WIDTH
        s = HANDLE_SIZE

        # Червона рамка
        self.canvas.create_rectangle(
            b, b, w - b, h - b,
            outline=BORDER_COLOR, width=b, fill=""
        )

        # Куточки — теги використовуємо тільки для курсору, клік ловимо через координати
        corners = {
            "nw": (0,     0,     s,   s),
            "ne": (w - s, 0,     w,   s),
            "sw": (0,     h - s, s,   h),
            "se": (w - s, h - s, w,   h),
        }
        cursors = {
            "nw": "size_nw_se",
            "ne": "size_ne_sw",
            "sw": "size_ne_sw",
            "se": "size_nw_se",
        }
        self._corners_coords = corners   # зберігаємо для hit-test у _press

        for corner, (x1, y1, x2, y2) in corners.items():
            tag = "handle_" + corner
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=BORDER_COLOR, fill=BORDER_COLOR,
                width=1, tags=tag
            )
            # Міняємо курсор при вході/виході з куточка
            self.canvas.tag_bind(
                tag, "<Enter>",
                lambda e, cur=cursors[corner]: self.canvas.configure(cursor=cur)
            )
            self.canvas.tag_bind(
                tag, "<Leave>",
                lambda e: self.canvas.configure(cursor="fleur")
            )

        # Підпис з розміром по центру
        self._label = self.canvas.create_text(
            w // 2, h // 2,
            text="", fill="#ff6666",
            font=("Segoe UI", 10), anchor="center"
        )
        self._update_label()

    # ── ПІДПИС ────────────────────────────────────────────────────────────────

    def _update_label(self):
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            text = (str(w) + " x " + str(h) +
                    "   (" + str(x) + ", " + str(y) + ")\n"
                    "Подвійний клік — вийти")
            self.canvas.itemconfig(self._label, text=text)
            self.canvas.coords(self._label, w // 2, h // 2)
        except Exception as e:
            print("[_update_label] Помилка: " + str(e))

    # ── ПРИВ'ЯЗКА ПОДІЙ ───────────────────────────────────────────────────────

    def _bind_events(self):
        """
        ButtonPress на canvas визначає режим (drag/resize).
        Motion і Release — на root, щоб не втрачати їх при швидкому русі миші.
        """
        self.canvas.bind("<ButtonPress-1>",    self._on_press)
        self.canvas.bind("<Double-Button-1>",  lambda e: self._quit())

        # Motion і Release на рівні root — ловить навіть якщо миша вийшла за межі
        self.root.bind("<B1-Motion>",          self._on_motion)
        self.root.bind("<ButtonRelease-1>",    self._on_release)

        # Оновлення рамки і мітки при зміні розміру вікна
        self.root.bind("<Configure>",          self._on_configure)

    # ── ОБРОБНИКИ ─────────────────────────────────────────────────────────────

    def _on_press(self, e):
        """Визначає: клік у куточку → resize, інакше → drag."""
        # Знімаємо стан вікна один раз при натисканні
        self._start_mx = e.x_root
        self._start_my = e.y_root
        self._start_wx = self.root.winfo_x()
        self._start_wy = self.root.winfo_y()
        self._start_ww = self.root.winfo_width()
        self._start_wh = self.root.winfo_height()

        # Hit-test: чи клік потрапив у куточок?
        hit = self._hit_corner(e.x, e.y)
        if hit:
            self._mode   = "resize"
            self._corner = hit
        else:
            self._mode   = "drag"
            self._corner = None

    def _on_motion(self, e):
        if self._mode == "drag":
            self._do_drag(e)
        elif self._mode == "resize":
            self._do_resize(e)

    def _on_release(self, e):
        self._mode   = None
        self._corner = None
        self.canvas.configure(cursor="fleur")

    def _on_configure(self, e):
        """Викликається при будь-якій зміні вікна — перемальовуємо рамку."""
        self._draw_frame()

    # ── DRAG ──────────────────────────────────────────────────────────────────

    def _do_drag(self, e):
        dx = e.x_root - self._start_mx
        dy = e.y_root - self._start_my
        nx = self._start_wx + dx
        ny = self._start_wy + dy
        self.root.geometry("+" + str(nx) + "+" + str(ny))
        self._schedule_save()
        self._update_label()

    # ── RESIZE ────────────────────────────────────────────────────────────────

    def _do_resize(self, e):
        dx = e.x_root - self._start_mx
        dy = e.y_root - self._start_my
        corner = self._corner

        x = self._start_wx
        y = self._start_wy
        w = self._start_ww
        h = self._start_wh

        if "e" in corner:
            w = max(MIN_WIDTH,  w + dx)
        if "s" in corner:
            h = max(MIN_HEIGHT, h + dy)
        if "w" in corner:
            new_w = max(MIN_WIDTH, w - dx)
            x = x + (w - new_w)
            w = new_w
        if "n" in corner:
            new_h = max(MIN_HEIGHT, h - dy)
            y = y + (h - new_h)
            h = new_h

        self.root.geometry(str(w) + "x" + str(h) + "+" + str(x) + "+" + str(y))
        self._schedule_save()
        self._update_label()

    # ── HIT-TEST ──────────────────────────────────────────────────────────────

    def _hit_corner(self, cx, cy):
        """Повертає назву куточка ('nw','ne','sw','se') або None."""
        for corner, (x1, y1, x2, y2) in self._corners_coords.items():
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return corner
        return None

    # ── ЗБЕРЕЖЕННЯ ────────────────────────────────────────────────────────────

    def _schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, self._save_config)

    def _save_config(self):
        try:
            cfg = {
                "x":      self.root.winfo_x(),
                "y":      self.root.winfo_y(),
                "width":  self.root.winfo_width(),
                "height": self.root.winfo_height(),
            }
            with open(CAPTURE_CONFIG, "w") as f:
                json.dump(cfg, f)
        except Exception as e:
            print("[_save_config] Помилка: " + str(e))

    def _load_config(self):
        try:
            if CAPTURE_CONFIG.exists():
                with open(CAPTURE_CONFIG) as f:
                    cfg = json.load(f)
                x = cfg.get("x", DEFAULT_X)
                y = cfg.get("y", DEFAULT_Y)
                w = cfg.get("width",  DEFAULT_W)
                h = cfg.get("height", DEFAULT_H)
                self.root.geometry(str(w) + "x" + str(h) + "+" + str(x) + "+" + str(y))
                print("  Відновлено позицію: " + str(w) + "x" + str(h) +
                      " на (" + str(x) + ", " + str(y) + ")")
        except Exception as e:
            print("[_load_config] Помилка: " + str(e))

    def _quit(self):
        print("  Overlay закрито.")
        self.root.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДУ
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("  Capture Overlay")
    print("=" * 50)
    print("  Рухай рамку мишкою     — переміщення")
    print("  Куточки (червоні)      — змінити розмір")
    print("  Подвійний клік         — закрити")
    print("  Конфіг: " + str(CAPTURE_CONFIG.resolve()))
    print("=" * 50)
    print()

    root = tk.Tk()
    app  = CaptureOverlay(root)
    root.mainloop()