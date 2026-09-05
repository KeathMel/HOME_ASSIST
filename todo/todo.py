#!/usr/bin/env python3
"""
Todo — standing notepad list
Background: day/night notepad image
Tasks from ~/.config/todo/tasks.json
"""

import sys, os, json, time, subprocess
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QDialog,
    QLineEdit, QComboBox, QTextEdit, QFormLayout, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QColor, QPainter, QFont, QPen, QBrush, QLinearGradient

# ── paths ─────────────────────────────────────────────────────────────────────

ASSETS     = os.path.expanduser("~/.config/MASTER_VAULT/HOME_ASSIST/todo")
TASKS_FILE = os.path.join(ASSETS, "tasks.json")

def bg_for_hour(h):
    if 6 <= h < 19:
        return os.path.join(ASSETS, "todo_day.png")
    return os.path.join(ASSETS, "todo_night.png")

CAT_COLORS = {
    "important":  QColor(200,  55,  55),
    "today":      QColor(200, 160,  30),
    "coming up":  QColor( 50, 160,  80),
    "goals":      QColor(120,  55, 190),
}
CAT_NAMES = ["Important", "Today", "Coming up", "Goals"]

def cat_color(cat):
    return CAT_COLORS.get(cat.lower().strip(), QColor(140,120,90))

# ── tasks ─────────────────────────────────────────────────────────────────────

def load_tasks():
    if not os.path.exists(TASKS_FILE): return []
    with open(TASKS_FILE) as f:
        try: return json.load(f)
        except: return []

def save_tasks(tasks):
    os.makedirs(ASSETS, exist_ok=True)
    with open(TASKS_FILE,"w") as f: json.dump(tasks, f, indent=2)

def update_task(tid, **kwargs):
    tasks = load_tasks()
    for t in tasks:
        if t.get("id") == tid:
            t.update(kwargs)
    save_tasks(tasks)

def add_task(category, title, notes=""):
    tasks = load_tasks()
    tasks.append({
        "command": "save",
        "category": category,
        "title": title,
        "notes": notes,
        "status": "active",
        "id": int(time.time()*1000 + len(tasks)),
    })
    save_tasks(tasks)

def set_status(tid, status):
    tasks = load_tasks()
    for t in tasks:
        if t.get("id") == tid:
            t["status"] = status
            if status == "active":
                t["command"] = "save"
    save_tasks(tasks)

def remove_task(tid):
    save_tasks([t for t in load_tasks() if t.get("id") != tid])

# ── file watcher ──────────────────────────────────────────────────────────────

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

class _Sig(QObject):
    changed = pyqtSignal()

# ── add task dialog ───────────────────────────────────────────────────────────

DLG_STYLE = """
QDialog{background:#d8c9a0;color:#281a0a;}
QLabel{color:rgba(60,40,15,0.8);font-size:11px;font-family:monospace;}
QLineEdit,QTextEdit,QComboBox{
    background:rgba(255,250,235,0.55);color:#281a0a;
    border:none;border-bottom:1px solid rgba(100,70,30,0.45);
    padding:6px 4px;font-size:12px;font-family:monospace;}
QLineEdit:focus,QTextEdit:focus,QComboBox:focus{border-bottom:1px solid rgba(100,70,30,0.85);}
QComboBox QAbstractItemView{background:#d8c9a0;color:#281a0a;selection-background-color:rgba(200,160,30,0.45);}
QPushButton{background:rgba(40,25,10,0.45);color:rgba(245,235,200,0.92);
    border:1.5px solid rgba(100,70,30,0.5);
    border-radius:4px;padding:6px 16px;font-size:11px;font-family:monospace;font-weight:bold;}
QPushButton:hover{background:rgba(60,40,15,0.7);border-color:rgba(100,70,30,0.85);}
QPushButton#ok{background:rgba(40,90,40,0.7);border:1.5px solid rgba(60,160,80,0.6);
    color:#e8ffe0;font-weight:bold;}
"""

class TaskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("new task")
        self.setMinimumWidth(210)
        self.setStyleSheet(DLG_STYLE)
        self._build()

    def _build(self):
        l = QVBoxLayout(self); l.setContentsMargins(16,16,16,16); l.setSpacing(10)
        form = QFormLayout(); form.setSpacing(10)
        self._cat   = QComboBox()
        for c in CAT_NAMES: self._cat.addItem(c)
        self._title = QLineEdit(); self._title.setPlaceholderText("task title…")
        self._notes = QTextEdit(); self._notes.setFixedHeight(48)
        self._notes.setPlaceholderText("notes (optional)…")
        form.addRow("category", self._cat)
        form.addRow("title", self._title)
        form.addRow("notes", self._notes)
        l.addLayout(form)
        row = QHBoxLayout(); row.addStretch()
        cancel = QPushButton("cancel"); cancel.clicked.connect(self.reject)
        ok = QPushButton("confirm"); ok.setObjectName("ok"); ok.clicked.connect(self._ok)
        row.addWidget(cancel); row.addWidget(ok)
        l.addLayout(row)

    def _ok(self):
        if not self._title.text().strip(): return
        self.accept()

    def data(self):
        return self._cat.currentText(), self._title.text().strip(), self._notes.toPlainText().strip()

# ── task row widget ───────────────────────────────────────────────────────────

ROW_STYLE = """
QFrame#row {
    background: rgba(245,235,200,0.10);
    border-bottom: 1px solid rgba(100,70,30,0.25);
}
QFrame#row:hover { background: rgba(245,235,200,0.17); }
QLabel#title { color: rgba(40,25,10,0.92); font-size: 11px; font-family: monospace; font-weight: bold; }
QLabel#notes { color: rgba(60,40,15,0.65); font-size: 11px; font-family: monospace; }
QLabel#cat   { font-size: 10px; font-family: monospace; font-weight: bold; }
QPushButton#act {
    background: transparent; border: none;
    color: rgba(40,25,10,0.55); font-size: 16px;
}
QPushButton#act:hover { color: rgba(40,25,10,0.9); }
"""

class TaskRow(QWidget):
    def __init__(self, task, on_done, on_delete, on_restore, view, parent=None):
        super().__init__(parent)
        self._task      = task
        self._on_done   = on_done
        self._on_delete = on_delete
        self._on_restore= on_restore
        self._view      = view
        self._expanded  = False
        self.setStyleSheet(ROW_STYLE)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)

        # main row
        row = QFrame(); row.setObjectName("row")
        rl  = QHBoxLayout(row); rl.setContentsMargins(10,7,8,7); rl.setSpacing(6)
        rl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # category dot
        dot = QLabel("●")
        c   = cat_color(self._task.get("category",""))
        dot.setStyleSheet(f"color:rgb({c.red()},{c.green()},{c.blue()});font-size:17px;")
        dot.setFixedWidth(18)
        rl.addWidget(dot)

        # title + notes
        txt_col = QVBoxLayout(); txt_col.setSpacing(1)
        title_lbl = QLabel(self._task.get("title",""))
        title_lbl.setObjectName("title")
        title_lbl.setWordWrap(True)
        title_lbl.setMaximumWidth(140)
        if self._task.get("status") == "done":
            title_lbl.setStyleSheet("color:rgba(40,25,10,0.45);font-size:13px;font-family:monospace;"
                                    "font-weight:bold;text-decoration:line-through;")
        txt_col.addWidget(title_lbl)
        rl.addLayout(txt_col, 1)

        # expand arrow with step count
        steps = self._task.get("steps", [])
        done_steps = sum(1 for s in steps if isinstance(s, dict) and s.get("done"))
        step_label = f" {done_steps}/{len(steps)}" if steps else ""
        self._arrow = QPushButton(f"▾{step_label}")
        self._arrow.setObjectName("act")
        self._arrow.setFixedSize(44,24)
        self._arrow.clicked.connect(self._toggle_expand)
        rl.addWidget(self._arrow)

        # checkbox — action depends on status
        status = self._task.get("status","active")
        self._check = QPushButton("○")
        self._check.setObjectName("act")
        self._check.setFixedSize(24,24)
        if status == "active":
            self._check.clicked.connect(self._quick_done)
        else:
            self._check.clicked.connect(self._quick_restore)
        rl.addWidget(self._check)

        outer.addWidget(row)

        # expandable detail panel
        self._detail = QFrame()
        self._detail.setStyleSheet("background:rgba(245,235,200,0.15);border-top:1px solid rgba(100,70,30,0.2);")
        dv = QVBoxLayout(self._detail); dv.setContentsMargins(8,6,8,6); dv.setSpacing(4)
        self._detail.hide()

        # notes (small print, above the action row)
        if self._task.get("notes"):
            notes_lbl = QLabel(self._task["notes"])
            notes_lbl.setObjectName("notes")
            notes_lbl.setWordWrap(True)
            notes_lbl.setStyleSheet("color:rgba(60,40,15,0.7);font-size:10px;"
                                    "font-family:monospace;font-style:italic;")
            dv.addWidget(notes_lbl)

        # top row: + add step (left) + trash (right)
        top_row = QHBoxLayout(); top_row.setSpacing(4)
        add_step_btn = QPushButton("+ step")
        add_step_btn.setStyleSheet(
            "background:rgba(40,25,10,0.5);border:none;border-radius:2px;"
            "color:rgba(245,235,200,0.85);font-size:10px;padding:2px 6px;font-family:monospace;")
        top_row.addWidget(add_step_btn)
        top_row.addStretch()
        trash_btn = QPushButton("🗑")
        trash_btn.setStyleSheet(
            "background:rgba(180,40,40,0.6);border:none;border-radius:2px;"
            "color:#fff;font-size:12px;padding:2px 6px;")
        trash_btn.clicked.connect(self._trash_task)
        top_row.addWidget(trash_btn)
        dv.addLayout(top_row)

        # inline step input (hidden until + step clicked)
        self._step_inp_row = QHBoxLayout(); self._step_inp_row.setSpacing(3)
        self._step_inp = QLineEdit()
        self._step_inp.setPlaceholderText("step…")
        self._step_inp.setStyleSheet(
            "background:rgba(40,25,10,0.4);color:#fff;border:none;"
            "border-bottom:1px solid rgba(200,170,100,0.5);font-size:10px;"
            "font-family:monospace;padding:2px 4px;")
        self._step_inp.returnPressed.connect(self._confirm_step)
        self._step_inp_row_widget = QWidget()
        self._step_inp_row_widget.setLayout(self._step_inp_row)
        self._step_inp_row.addWidget(self._step_inp)
        confirm_btn = QPushButton("✓")
        confirm_btn.setFixedSize(20,20)
        confirm_btn.setStyleSheet("background:rgba(50,160,60,0.7);border:none;color:#fff;font-size:10px;border-radius:2px;")
        confirm_btn.clicked.connect(self._confirm_step)
        self._step_inp_row.addWidget(confirm_btn)
        self._step_inp_row_widget.hide()
        dv.addWidget(self._step_inp_row_widget)
        add_step_btn.clicked.connect(self._show_step_inp)

        # steps list container
        self._steps_container = QVBoxLayout(); self._steps_container.setSpacing(2)
        dv.addLayout(self._steps_container)
        self._rebuild_steps()

        outer.addWidget(self._detail)

    def _toggle_expand(self):
        self._expanded = not self._expanded
        self._detail.setVisible(self._expanded)
        self._arrow.setText("▴" if self._expanded else "▾")

    def _trash_task(self):
        sound = "/home/k/.config/todo/sounds/windows-10-hardware-remove-disconnect.mp3"
        if os.path.exists(sound):
            subprocess.Popen(["mpv","--no-video","--volume=70", sound],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._on_delete(self._task)

    def _quick_done(self):
        sound = "/home/k/.config/todo/sounds/Task_done.mp3"
        if os.path.exists(sound):
            subprocess.Popen(["mpv","--no-video","--volume=70", sound],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._on_done(self._task)

    def _quick_restore(self):
        sound = "/home/k/.config/todo/sounds/respawn-anchor-1.mp3"
        if os.path.exists(sound):
            subprocess.Popen(["mpv","--no-video","--volume=70", sound],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._on_restore(self._task)

    def _rebuild_steps(self):
        """Clear and rebuild steps in the detail panel."""
        while self._steps_container.count():
            item = self._steps_container.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        steps = self._task.get("steps", [])
        for i, step in enumerate(steps):
            txt  = step.get("text", step) if isinstance(step, dict) else str(step)
            done = isinstance(step, dict) and step.get("done", False)
            row  = QHBoxLayout(); row.setSpacing(4)
            cb   = QPushButton("✓" if done else "○")
            cb.setFixedSize(20,20)
            cb.setStyleSheet(
                ("background:rgba(50,160,60,0.7);" if done else "background:rgba(40,25,10,0.3);") +
                "border:none;border-radius:2px;color:#fff;font-size:10px;")
            cb.clicked.connect(lambda checked, idx=i: self._toggle_step(idx))
            lbl = QLabel(txt)
            lbl.setStyleSheet(
                "color:rgba(40,25,10,0.4);font-size:11px;font-family:monospace;text-decoration:line-through;"
                if done else
                "color:rgba(40,25,10,0.85);font-size:11px;font-family:monospace;")
            row.addWidget(cb); row.addWidget(lbl, 1)
            wrapper = QWidget(); wrapper.setLayout(row)
            self._steps_container.addWidget(wrapper)
        # update arrow label
        done_c = sum(1 for s in steps if isinstance(s, dict) and s.get("done"))
        step_label = f" {done_c}/{len(steps)}" if steps else ""
        self._arrow.setText(f"{'▴' if self._expanded else '▾'}{step_label}")

    def _toggle_step(self, idx):
        steps = self._task.setdefault("steps", [])
        if idx >= len(steps): return
        s = steps[idx]
        if isinstance(s, str): s = {"text": s, "done": False}
        s["done"] = not s.get("done", False)
        steps[idx] = s
        update_task(self._task["id"], steps=steps)
        sound = "/home/k/.config/todo/sounds/universfield-level-passed-142971.mp3"
        if os.path.exists(sound):
            subprocess.Popen(["mpv","--no-video","--volume=70", sound],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._rebuild_steps()

    def _show_step_inp(self):
        try:
            self._step_inp_row_widget.show()
            self._step_inp.setFocus()
        except RuntimeError:
            pass

    def _confirm_step(self):
        try:
            txt = self._step_inp.text().strip()
            if not txt:
                return
            steps = self._task.setdefault("steps", [])
            steps.append({"text": txt, "done": False})
            # write to file
            tasks = load_tasks()
            for t in tasks:
                if t.get("id") == self._task["id"]:
                    t["steps"] = steps
            save_tasks(tasks)
            self._step_inp.clear()
            self._step_inp_row_widget.hide()
            self._rebuild_steps()
        except Exception as e:
            print("add step error:", e)

# ── background canvas ─────────────────────────────────────────────────────────

class BgCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._px = None
        self.reload()

    def reload(self):
        h = int(time.strftime("%H"))
        p = bg_for_hour(h)
        self._px = QPixmap(p) if os.path.exists(p) else None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.rect()
        if self._px and not self._px.isNull():
            s  = self._px.scaled(r.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
            ox = (s.width()-r.width())//2; oy=(s.height()-r.height())//2
            p.drawPixmap(0,0,s,ox,oy,r.width(),r.height())
        else:
            p.fillRect(r, QColor(30,20,10))
        p.end()

# ── main window ───────────────────────────────────────────────────────────────

MAIN_STYLE = """
QMainWindow,QWidget#root { background: transparent; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; }
QScrollBar::handle:vertical { background: rgba(100,70,30,0.4); border-radius:3px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QPushButton#add_btn {
    background: rgba(40,25,10,0.72);
    border: 1.5px solid rgba(100,70,30,0.8);
    border-radius: 4px;
    color: rgba(245,235,200,0.92);
    font-size: 12px;
    font-family: monospace;
    font-weight: bold;
    padding: 8px;
}
QPushButton#add_btn:hover { background: rgba(60,40,15,0.85); }
QPushButton#tab_btn {
    background: rgba(40,25,10,0.45);
    border: 1.5px solid rgba(100,70,30,0.5);
    border-radius: 4px;
    color: rgba(40,25,10,0.55);
    font-size: 11px;
    font-family: monospace;
    font-weight: bold;
    padding: 8px 0;
}
QPushButton#tab_btn[active=true] {
    background: rgba(40,25,10,0.72);
    border: 1.5px solid rgba(100,70,30,0.85);
    color: rgba(245,235,200,0.95);
}
"""

class TodoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Todo")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(160, 320)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(MAIN_STYLE)

        self._tab = "active"  # active | done | deleted
        self._drag_pos = None
        self._scroll_sound_timer = None
        self._build()
        self._refresh()
        self._start_watcher()

        self._bg_timer = QTimer()
        self._bg_timer.timeout.connect(lambda: (self._bg2.reload(), self._bg2.update()))
        self._bg_timer.start(60_000)

    def _build(self):
        stack = QWidget()
        self.setCentralWidget(stack)
        stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # background fills everything
        self._bg2 = BgCanvas(stack)
        self._bg2.setGeometry(0, 0, self.width(), self.height())

        # overlay on top
        overlay = QWidget(stack)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        overlay.setGeometry(0, 0, self.width(), self.height())
        overlay.raise_()

        # add button — absolute position (tuned to sit on notepad page)
        add_btn = QPushButton("＋  add task", overlay)
        add_btn.setObjectName("add_btn")
        add_btn.setGeometry(110, 185, 200, 40)
        add_btn.clicked.connect(self._add_task)

        # tab row — directly below add button
        btn_x   = 110   # same x as add_btn
        btn_y   = 230   # add_btn bottom (185+40) + 5px gap
        btn_w   = 64    # each tab button width
        btn_gap = 4
        self._tabs = {}
        for i, name in enumerate(("active", "done", "deleted")):
            btn = QPushButton(name, overlay)
            btn.setObjectName("tab_btn")
            btn.setProperty("active", name == self._tab)
            btn.setGeometry(btn_x + i*(btn_w+btn_gap), btn_y, btn_w, 30)
            btn.clicked.connect(lambda checked, n=name: self._switch_tab(n))
            self._tabs[name] = btn

        # scrollable task list — below tab row
        self._scroll = QScrollArea(overlay)
        scroll = self._scroll
        scroll.setGeometry(110, 265, 200, 192)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background:transparent;")
        self._list_widget = QWidget()
        self._list_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch()
        scroll.setWidget(self._list_widget)
        scroll.installEventFilter(self)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_bg2"):
            self._bg2.setGeometry(0, 0, self.width(), self.height())
        for child in self.centralWidget().children():
            if isinstance(child, QWidget) and child is not self._bg2:
                child.setGeometry(0, 0, self.width(), self.height())

    # ── tabs ──────────────────────────────────────────────────────────────────

    def _switch_tab(self, name):
        self._tab = name
        for n, btn in self._tabs.items():
            btn.setProperty("active", n == name)
            btn.setStyleSheet("")  # force refresh
        self._refresh()

    # ── task actions ──────────────────────────────────────────────────────────

    def _add_task(self):
        dlg = TaskDialog(self)
        if dlg.exec():
            cat, title, notes = dlg.data()
            add_task(cat, title, notes)
            self._refresh()

    def _done(self, task):
        set_status(task["id"], "done")
        self._refresh()

    def _delete(self, task):
        set_status(task["id"], "deleted")
        self._refresh()

    def _restore(self, task):
        set_status(task["id"], "active")
        self._refresh()

    # ── refresh ───────────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self._scroll and event.type() == QEvent.Type.Wheel:
            if self._scroll_sound_timer is None or not self._scroll_sound_timer.isActive():
                sound = "/home/k/.config/todo/sounds/makigai_maimai-paper-245786.mp3"
                subprocess.Popen(["mpv","--no-video","--volume=50", sound],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._scroll_sound_timer = QTimer()
                self._scroll_sound_timer.setSingleShot(True)
                self._scroll_sound_timer.start(600)
        return super().eventFilter(obj, event)

    def _refresh(self):
        # remember which task had dropdown open
        expanded_id = None
        for i in range(self._list_layout.count()-1):
            item = self._list_layout.itemAt(i)
            if item and item.widget() and hasattr(item.widget(), "_expanded"):
                if item.widget()._expanded:
                    expanded_id = item.widget()._task.get("id")
                    break

        # clear list
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        tasks = [t for t in load_tasks() if t.get("status","active") == self._tab]

        if not tasks:
            empty = QLabel("nothing here")
            empty.setStyleSheet("color:rgba(40,25,10,0.35);font-size:12px;font-family:monospace;"
                                "padding:16px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_layout.insertWidget(0, empty)
            return

        for i, t in enumerate(tasks):
            row = TaskRow(t, self._done, self._delete, self._restore, self._tab)
            self._list_layout.insertWidget(i, row)
            # restore expanded state
            if t.get("id") == expanded_id:
                row._toggle_expand()

    # ── watcher ───────────────────────────────────────────────────────────────

    def _start_watcher(self):
        self._sig = _Sig()
        self._sig.changed.connect(self._refresh)

        # polling fallback always on (cheap insurance; bridge writes are infrequent)
        self._poll_mtime = self._file_mtime()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_check)
        self._poll_timer.start(1500)

        # watchdog (instant) on top of polling, if available
        if not HAS_WATCHDOG: return
        target_name = os.path.basename(TASKS_FILE)
        class H(FileSystemEventHandler):
            def __init__(self, sig): super().__init__(); self._s=sig
            def _maybe(self, path):
                try:
                    if os.path.basename(path) == target_name:
                        self._s.changed.emit()
                except Exception: pass
            def on_modified(self, e): self._maybe(e.src_path)
            def on_created(self, e):  self._maybe(e.src_path)
            def on_moved(self, e):
                self._maybe(getattr(e, "dest_path", "") or "")
                self._maybe(e.src_path)
        self._obs = Observer()
        os.makedirs(ASSETS, exist_ok=True)
        self._obs.schedule(H(self._sig), ASSETS, recursive=False)
        self._obs.start()

    def _file_mtime(self):
        try: return os.path.getmtime(TASKS_FILE)
        except Exception: return 0

    def _poll_check(self):
        m = self._file_mtime()
        if m != self._poll_mtime:
            self._poll_mtime = m
            self._refresh()

    def closeEvent(self, e):
        if HAS_WATCHDOG and hasattr(self,"_obs"):
            self._obs.stop(); self._obs.join()
        super().closeEvent(e)

    # ── drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag_pos)
    def mouseReleaseEvent(self, e): self._drag_pos=None

    def keyPressEvent(self, e):
        if e.key()==Qt.Key.Key_Escape: self.hide()
        elif e.key()==Qt.Key.Key_Tab: self._add_task()
        super().keyPressEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = TodoWindow()
    win.show()
    sys.exit(app.exec())
