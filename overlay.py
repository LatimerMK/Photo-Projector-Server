"""
Overlay — вибір області захвату екрану
Запусти цей файл окремо від server.py.
З'явиться червона рамка поверх усіх вікон — рухай і змінюй розмір мишкою.
Координати зберігаються в capture.json і одразу підхоплюються сервером.
"""

import json
import tkinter as tk
from pathlib import Path

# ── КОНФІГУРАЦІЯ ──────────────────────────────────────────────────────────────
CAPTURE_CONFIG   = Path("capture.json")  # Файл з координатами для server.py
BORDER_COLOR     = "#ff2222"             # Колір рамки
BORDER_WIDTH     = 3                     # Товщина рамки (px)
HANDLE_SIZE      = 14                    # Розмір куточків для зміни розміру (px)
MIN_WIDTH        = 100                   # Мінімальна ширина рамки
MIN_HEIGHT       = 80                    # Мінімальна висота рамки
SAVE_DELAY_MS    = 80                    # Затримка збереження після руху (ms)
DEFAULT_X        = 100                   # Початкова позиція X
DEFAULT_Y        = 100                   # Початкова позиція Y
DEFAULT_W        = 800                   # Початкова ширина
DEFAULT_H        = 600                   # Початкова висота

# ── ГОЛОВНИЙ КЛАС ─────────────────────────────────────────────────────────────

class CaptureOverlay:
    """
    Прозоре вікно tkinter поверх усього з червоною рамкою.
    Підтримує: перетягування за тіло, зміну розміру за куточки.
    """

    def __init__(self, root: tk.Tk):
        self.root = root

        # Стан перетягування і ресайзу
        self._drag   = {"active": False, "sx": 0, "sy": 0, "ox": 0, "oy": 0}
        self._resize = {"active": False, "corner": "", "sx": 0, "sy": 0,
                        "ox": 0, "oy": 0, "ow": 0, "oh": 0}
        self._save_job = None   # pending after() для збереження

        self._setup_window()
        self._build_ui()
        self._load_config()
        self._save_config()     # Зберегти початкові координати одразу

    # ── ІНІЦІАЛІЗАЦІЯ ВІКНА ───────────────────────────────────────────────────

    def _setup_window(self):
        """Налаштовує вікно: прозоре, поверх усіх, без рамки ОС."""
        r = self.root
        r.overrideredirect(True)           # Без заголовка і рамки ОС
        r.attributes("-topmost", True)     # Завжди поверх
        r.attributes("-transparentcolor", "black")  # Чорний = прозорий
        r.configure(bg="black")
        r.geometry(f"{DEFAULT_W}x{DEFAULT_H}+{DEFAULT_X}+{DEFAULT_Y}")
        r.resizable(False, False)

    def _build_ui(self):
        """Будує canvas з рамкою, куточками і підказкою."""
        self.canvas = tk.Canvas(
            self.root, bg="black", highlightthickness=0, cursor="fleur"
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Червона рамка (малюється через update_frame при кожному resize)
        self._draw_frame()

        # Підказка по центру
        self.label = self.canvas.create_text(
            0, 0, text="", fill="#ff6666",
            font=("Segoe UI", 10), anchor="center"
        )

        # ── Прив'язка подій ──
        # Перетягування (натискання в центрі, не на куточках)
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        # Подвійний клік — вийти
        self.canvas.bind("<Double-Button-1>", lambda e: self._quit())

        # Оновити мітку після першого відображення
        self.root.after(50, self._update_label)

    def _draw_frame(self):
        """Малює червону рамку і куточки ресайзу на canvas."""
        self.canvas.delete("all")
        w = self.canvas.winfo_width()  or DEFAULT_W
        h = self.canvas.winfo_height() or DEFAULT_H
        b = BORDER_WIDTH
        s = HANDLE_SIZE

        # Зовнішня червона рамка
        self.canvas.create_rectangle(
            b, b, w - b, h - b,
            outline=BORDER_COLOR, width=b, fill=""
        )

        # Куточки для ресайзу (залиті червоним квадратиком)
        corners = {
            "nw": (0,   0,   s,   s),
            "ne": (w-s, 0,   w,   s),
            "sw": (0,   h-s, s,   h),
            "se": (w-s, h-s, w,   h),
        }
        cursors = {"nw": "size_nw_se", "ne": "size_ne_sw",
                   "sw": "size_ne_sw", "se": "size_nw_se"}

        for corner, (x1, y1, x2, y2) in corners.items():
            rect = self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=BORDER_COLOR, fill=BORDER_COLOR, width=1,
                tags=f"handle_{corner}"
            )
            self.canvas.tag_bind(
                rect, "<ButtonPress-1>",
                lambda e, c=corner: self._resize_start(e, c)
            )
            self.canvas.tag_bind(rect, "<B1-Motion>",   self._resize_motion)
            self.canvas.tag_bind(rect, "<ButtonRelease-1>", self._resize_end)
            self.canvas.tag_bind(
                rect, "<Enter>",
                lambda e, cur=cursors[corner]: self.canvas.configure(cursor=cur)
            )
            self.canvas.tag_bind(
                rect, "<Leave>",
                lambda e: self.canvas.configure(cursor="fleur")
            )

        # Мітка координат (поновлюється окремо)
        self.label = self.canvas.create_text(
            w // 2, h // 2, text="", fill="#ff6666",
            font=("Segoe UI", 10), anchor="center"
        )

    # ── ПЕРЕТЯГУВАННЯ ────────────────────────────────────────────────────────

    def _on_press(self, e):
        self._drag = {
            "active": True,
            "sx": e.x_root, "sy": e.y_root,
            "ox": self.root.winfo_x(), "oy": self.root.winfo_y(),
        }

    def _on_motion(self, e):
        if not self._drag["active"] or self._resize["active"]:
            return
        dx = e.x_root - self._drag["sx"]
        dy = e.y_root - self._drag["sy"]
        nx = self._drag["ox"] + dx
        ny = self._drag["oy"] + dy
        self.root.geometry(f"+{nx}+{ny}")
        self._schedule_save()
        self._update_label()

    def _on_release(self, e):
        self._drag["active"] = False

    # ── ЗМІНА РОЗМІРУ ────────────────────────────────────────────────────────

    def _resize_start(self, e, corner: str):
        self._drag["active"] = False   # Скасувати drag
        self._resize = {
            "active": True,
            "corner": corner,
            "sx": e.x_root, "sy": e.y_root,
            "ox": self.root.winfo_x(), "oy": self.root.winfo_y(),
            "ow": self.root.winfo_width(), "oh": self.root.winfo_height(),
        }

    def _resize_motion(self, e):
        r = self._resize
        if not r["active"]:
            return

        dx = e.x_root - r["sx"]
        dy = e.y_root - r["sy"]
        corner = r["corner"]

        x, y   = r["ox"], r["oy"]
        w, h   = r["ow"], r["oh"]

        if "e" in corner: w = max(MIN_WIDTH,  w + dx)
        if "s" in corner: h = max(MIN_HEIGHT, h + dy)
        if "w" in corner:
            new_w = max(MIN_WIDTH, w - dx)
            x = x + (w - new_w)
            w = new_w
        if "n" in corner:
            new_h = max(MIN_HEIGHT, h - dy)
            y = y + (h - new_h)
            h = new_h

        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.after(10, self._draw_frame)
        self._schedule_save()
        self._update_label()

    def _resize_end(self, e):
        self._resize["active"] = False

    # ── ЗБЕРЕЖЕННЯ КОНФІГУРАЦІЇ ───────────────────────────────────────────────

    def _schedule_save(self):
        """Відкладене збереження — не викликає диск на кожен піксель руху."""
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, self._save_config)

    def _save_config(self):
        """Записує поточні координати в capture.json."""
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
            print(f"[CaptureOverlay._save_config] Помилка: {e}")

    def _load_config(self):
        """Відновлює позицію з попереднього сеансу якщо файл існує."""
        try:
            if CAPTURE_CONFIG.exists():
                with open(CAPTURE_CONFIG) as f:
                    cfg = json.load(f)
                x = cfg.get("x", DEFAULT_X)
                y = cfg.get("y", DEFAULT_Y)
                w = cfg.get("width",  DEFAULT_W)
                h = cfg.get("height", DEFAULT_H)
                self.root.geometry(f"{w}x{h}+{x}+{y}")
                print(f"  ✓ Відновлено позицію: {w}×{h} на ({x}, {y})")
        except Exception as e:
            print(f"[CaptureOverlay._load_config] Помилка: {e}")

    # ── МІТКА З РОЗМІРОМ ─────────────────────────────────────────────────────

    def _update_label(self):
        """Оновлює підпис з поточними координатами і розміром."""
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            text = f"{w} × {h}   ({x}, {y})\nПодвійний клік — вийти"
            self.canvas.itemconfig(self.label, text=text)
            # Центрувати мітку
            self.canvas.coords(self.label, w // 2, h // 2)
        except Exception as e:
            print(f"[_update_label] Помилка: {e}")

    def _quit(self):
        print("  Overlay закрито.")
        self.root.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДУ
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("  🔲  Capture Overlay")
    print("=" * 50)
    print("  Рухай рамку мишкою — область захвату")
    print("  Куточки         — змінити розмір")
    print("  Подвійний клік  — закрити")
    print(f"  Конфіг          : {CAPTURE_CONFIG.resolve()}")
    print("=" * 50)
    print()

    root = tk.Tk()
    app  = CaptureOverlay(root)

    # Оновлювати мітку і перемальовувати рамку при зміні розміру вікна
    root.bind("<Configure>", lambda e: (app._update_label(), app._draw_frame()))

    root.mainloop()
