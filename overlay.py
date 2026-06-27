"""
Overlay — вибір області захвату екрану
1. Рухай і змінюй розмір червоної рамки
2. Натисни "Почати трансляцію" — запишеться capture.json з active=true
3. Сервер починає знімати кадри
4. Натисни "Зупинити" — capture.json отримує active=false
"""

import json
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

# ── КОНФІГУРАЦІЯ ──────────────────────────────────────────────────────────────
CAPTURE_CONFIG  = Path("capture.json")
BORDER_COLOR    = "#ff2222"
BORDER_WIDTH    = 3
HANDLE_SIZE     = 18
MIN_WIDTH       = 100
MIN_HEIGHT      = 80
SAVE_DELAY_MS   = 80
DEFAULT_X       = 200
DEFAULT_Y       = 150
DEFAULT_W       = 800
DEFAULT_H       = 600

# Висота панелі керування внизу рамки
PANEL_H = 44


class CaptureOverlay:

    def __init__(self, root: tk.Tk):
        self.root = root
        self._mode      = None      # None | 'drag' | 'resize'
        self._corner    = None
        self._start_mx  = 0
        self._start_my  = 0
        self._start_wx  = 0
        self._start_wy  = 0
        self._start_ww  = 0
        self._start_wh  = 0
        self._save_job  = None
        self._streaming = False     # Чи активна трансляція зараз
        self._corners_coords = {}

        self._setup_window()
        self._build_canvas()
        self._build_panel()
        self._bind_events()
        self._apply_geometry(DEFAULT_X, DEFAULT_Y, DEFAULT_W, DEFAULT_H)
        # Зберігаємо початковий стан БЕЗ active=true
        self._save_config(active=False)

    # ── ВІКНО ─────────────────────────────────────────────────────────────────

    def _setup_window(self):
        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-transparentcolor", "black")
        r.configure(bg="black")

    def _apply_geometry(self, x, y, w, h):
        """Встановлює геометрію і чекає поки tkinter її реально застосує."""
        self.root.geometry(str(w) + "x" + str(h + PANEL_H) + "+" + str(x) + "+" + str(y))
        self.root.update_idletasks()

    # ── CANVAS (рамка) ────────────────────────────────────────────────────────

    def _build_canvas(self):
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self._draw_frame()

    def _draw_frame(self):
        self.canvas.delete("all")
        w = self.root.winfo_width()  or DEFAULT_W
        h = self.root.winfo_height() or (DEFAULT_H + PANEL_H)
        # Рамка займає все КРІМ панелі внизу
        frame_h = h - PANEL_H
        b = BORDER_WIDTH
        s = HANDLE_SIZE

        # Червона рамка
        self.canvas.create_rectangle(
            b, b, w - b, frame_h - b,
            outline=BORDER_COLOR, width=b, fill=""
        )

        # Куточки
        corners = {
            "nw": (0,     0,          s,   s),
            "ne": (w - s, 0,          w,   s),
            "sw": (0,     frame_h-s,  s,   frame_h),
            "se": (w - s, frame_h-s,  w,   frame_h),
        }
        cursors = {
            "nw": "size_nw_se", "ne": "size_ne_sw",
            "sw": "size_ne_sw", "se": "size_nw_se",
        }
        self._corners_coords = corners

        for corner, (x1, y1, x2, y2) in corners.items():
            tag = "handle_" + corner
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=BORDER_COLOR, fill=BORDER_COLOR, width=1, tags=tag
            )
            self.canvas.tag_bind(
                tag, "<Enter>",
                lambda e, cur=cursors[corner]: self.canvas.configure(cursor=cur)
            )
            self.canvas.tag_bind(
                tag, "<Leave>",
                lambda e: self.canvas.configure(cursor="fleur")
            )

        # Підпис з розміром
        self._label = self.canvas.create_text(
            w // 2, frame_h // 2,
            text="", fill="#ff6666",
            font=("Segoe UI", 10), anchor="center"
        )
        self._update_label()

    # ── ПАНЕЛЬ КЕРУВАННЯ (знизу рамки) ────────────────────────────────────────

    def _build_panel(self):
        """Панель з кнопками — звичайний tk.Frame, не прозорий."""
        self.panel = tk.Frame(self.root, bg="#111111", height=PANEL_H)
        self.panel.place(x=0, rely=1.0, anchor="sw", relwidth=1.0, height=PANEL_H)

        # Розмір (текстовий підпис)
        self.size_var = tk.StringVar(value="800 x 600")
        tk.Label(
            self.panel, textvariable=self.size_var,
            bg="#111111", fg="#888888", font=("Segoe UI", 10)
        ).pack(side=tk.LEFT, padx=12)

        # Кнопка Зупинити (прихована до старту)
        self.btn_stop = tk.Button(
            self.panel, text="⏹ Зупинити",
            bg="#552222", fg="#ffffff", activebackground="#772222",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=14, pady=4, cursor="hand2",
            command=self._stop_stream
        )
        # Кнопка Почати
        self.btn_start = tk.Button(
            self.panel, text="▶ Почати трансляцію",
            bg="#225522", fg="#ffffff", activebackground="#337733",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=14, pady=4, cursor="hand2",
            command=self._confirm_and_start
        )
        self.btn_start.pack(side=tk.RIGHT, padx=8, pady=6)

        # Кнопка Закрити
        tk.Button(
            self.panel, text="✕",
            bg="#111111", fg="#555555", activebackground="#222222",
            font=("Segoe UI", 11),
            relief=tk.FLAT, padx=8, pady=4, cursor="hand2",
            command=self._quit
        ).pack(side=tk.RIGHT, padx=0, pady=6)

    # ── ПІДПИС ────────────────────────────────────────────────────────────────

    def _update_label(self):
        try:
            w = self.root.winfo_width()
            h = self.root.winfo_height() - PANEL_H
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            text = str(w) + " x " + str(h) + "   (" + str(x) + ", " + str(y) + ")"
            self.canvas.itemconfig(self._label, text=text)
            self.canvas.coords(self._label, w // 2, h // 2)
            self.size_var.set(str(w) + " x " + str(h))
        except Exception as e:
            print("[_update_label] Помилка: " + str(e))

    # ── ПРИВ'ЯЗКА ПОДІЙ ───────────────────────────────────────────────────────

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<B1-Motion>",       self._on_motion)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Configure>",       self._on_configure)

    # ── ОБРОБНИКИ МИШІ ────────────────────────────────────────────────────────

    def _on_press(self, e):
        self._start_mx = e.x_root
        self._start_my = e.y_root
        self._start_wx = self.root.winfo_x()
        self._start_wy = self.root.winfo_y()
        self._start_ww = self.root.winfo_width()
        self._start_wh = self.root.winfo_height()

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
        h = self._start_wh   # включає PANEL_H

        if "e" in corner:
            w = max(MIN_WIDTH, w + dx)
        if "s" in corner:
            h = max(MIN_HEIGHT + PANEL_H, h + dy)
        if "w" in corner:
            new_w = max(MIN_WIDTH, w - dx)
            x = x + (w - new_w)
            w = new_w
        if "n" in corner:
            new_h = max(MIN_HEIGHT + PANEL_H, h - dy)
            y = y + (h - new_h)
            h = new_h

        self.root.geometry(str(w) + "x" + str(h) + "+" + str(x) + "+" + str(y))
        self._schedule_save()
        self._update_label()

    # ── HIT-TEST ──────────────────────────────────────────────────────────────

    def _hit_corner(self, cx, cy):
        for corner, (x1, y1, x2, y2) in self._corners_coords.items():
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return corner
        return None

    # ── ТРАНСЛЯЦІЯ ────────────────────────────────────────────────────────────

    def _confirm_and_start(self):
        """Показує підтвердження і запускає трансляцію."""
        w = self.root.winfo_width()
        h = self.root.winfo_height() - PANEL_H
        msg = (
            "Почати трансляцію цієї області?\n\n"
            "Розмір: " + str(w) + " x " + str(h) + "\n"
            "Позиція: (" + str(self.root.winfo_x()) + ", " + str(self.root.winfo_y()) + ")\n\n"
            "Зображення буде доступне всім у локальній мережі."
        )
        ok = messagebox.askyesno("Почати трансляцію", msg, parent=self.root)
        if ok:
            self._start_stream()

    def _start_stream(self):
        self._streaming = True
        self._save_config(active=True)
        # Оновлюємо кнопки
        self.btn_start.pack_forget()
        self.btn_stop.pack(side=tk.RIGHT, padx=8, pady=6)
        print("  Трансляція розпочата")

    def _stop_stream(self):
        self._streaming = False
        self._save_config(active=False)
        # Оновлюємо кнопки
        self.btn_stop.pack_forget()
        self.btn_start.pack(side=tk.RIGHT, padx=8, pady=6)
        print("  Трансляція зупинена")

    # ── ЗБЕРЕЖЕННЯ ────────────────────────────────────────────────────────────

    def _schedule_save(self):
        """Відкладене збереження — щоб не писати на диск при кожному пікселі."""
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, lambda: self._save_config(self._streaming))

    def _save_config(self, active: bool):
        try:
            cfg = {
                "x":      self.root.winfo_x(),
                "y":      self.root.winfo_y(),
                "width":  self.root.winfo_width(),
                "height": self.root.winfo_height() - PANEL_H,  # висота БЕЗ панелі
                "active": active,
            }
            with open(CAPTURE_CONFIG, "w") as f:
                json.dump(cfg, f)
        except Exception as e:
            print("[_save_config] Помилка: " + str(e))

    def _quit(self):
        if self._streaming:
            self._stop_stream()
        print("  Overlay закрито.")
        self.root.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДУ
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("  Capture Overlay")
    print("=" * 50)
    print("  1. Розташуй червону рамку на потрібній області")
    print("  2. Натисни 'Почати трансляцію'")
    print("  3. Для зупинки — кнопка 'Зупинити'")
    print("  Конфіг: " + str(CAPTURE_CONFIG.resolve()))
    print("=" * 50)
    print()

    root = tk.Tk()
    app  = CaptureOverlay(root)
    root.mainloop()