#!/usr/bin/env python3
"""
Neural Calendar — cyberpunk wall calendar
Reads events from ~/.config/chrono/events.json
Background changes by time of day (day/night)
"""

import sys, os, json, time, math, calendar, subprocess, threading
from datetime import date, datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QRect, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import (
    QPixmap, QColor, QPainter, QPainterPath, QPen, QBrush,
    QFont, QFontMetrics, QLinearGradient,
)

# ── paths ─────────────────────────────────────────────────────────────────────

ASSETS      = os.path.expanduser("~/.config/MASTER_VAULT/HOME_ASSIST/Chrono")
EVENTS_FILE = os.path.join(ASSETS, "events.json")
# events whose date has passed get moved here, so events.json only holds
# today and what's still ahead. Both files are read back on load, so nothing
# vanishes from the calendar -- it's purely a split on disk.
PAST_EVENTS_FILE = os.path.join(ASSETS, "past_events.json")

def bg_for_hour(h):
    if 6 <= h < 19:
        return os.path.join(ASSETS, "Calendar_day.png")
    return os.path.join(ASSETS, "Calendar_night_png.png")

# ── category colours ──────────────────────────────────────────────────────────

CATEGORIES = {
    "obligations":           QColor(220,  60,  60),
    "social engagements":    QColor( 60, 120, 220),
    "administrative tasks":  QColor(220, 180,  40),
    "personal development":  QColor( 50, 190, 100),
    "logistics":             QColor(150,  80, 220),
}

def cat_color(name: str) -> QColor:
    return CATEGORIES.get(name.lower().strip(), QColor(180, 180, 180))

# ── events store ──────────────────────────────────────────────────────────────

def _read_events(path) -> list:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        try:
            data = json.load(f)
            return data if isinstance(data, list) else []
        except:
            return []

def _archive_past(events_list) -> list:
    """Move anything dated before today into past_events.json. Returns the
    events that are still current."""
    today = date.today().isoformat()
    current, past = [], _read_events(PAST_EVENTS_FILE)
    moved = False
    for ev in events_list:
        d = ev.get("date", "")
        if d and d < today:
            past.append(ev); moved = True
        else:
            current.append(ev)
    if moved:
        os.makedirs(ASSETS, exist_ok=True)
        with open(PAST_EVENTS_FILE, "w") as f:
            json.dump(past, f, indent=2)
        with open(EVENTS_FILE, "w") as f:
            json.dump(current, f, indent=2)
    return current

def load_events() -> dict:
    """Return dict keyed by 'YYYY-MM-DD' -> list of {appointment, category}"""
    raw = _archive_past(_read_events(EVENTS_FILE)) + _read_events(PAST_EVENTS_FILE)
    events = {}
    for ev in raw:
        d = ev.get("date","")
        if d:
            events.setdefault(d, []).append(ev)
    return events

def write_event(cmd: str, category: str, appointment: str, date_str: str):
    """Add or remove an event from the JSON file."""
    os.makedirs(ASSETS, exist_ok=True)
    events_list = _read_events(EVENTS_FILE)

    if cmd == "create":
        events_list.append({
            "command": "create",
            "category": category,
            "appointment": appointment,
            "date": date_str,
        })
    elif cmd == "delete":
        def _keep(e):
            return not (e.get("date") == date_str and
                        e.get("appointment","").lower() == appointment.lower())
        events_list = [e for e in events_list if _keep(e)]
        # a deleted event may already have been archived, so clear it there too
        past = _read_events(PAST_EVENTS_FILE)
        kept_past = [e for e in past if _keep(e)]
        if len(kept_past) != len(past):
            with open(PAST_EVENTS_FILE, "w") as f:
                json.dump(kept_past, f, indent=2)

    with open(EVENTS_FILE, "w") as f:
        json.dump(events_list, f, indent=2)

# ── file watcher ──────────────────────────────────────────────────────────────

class _Sig(QObject):
    changed = pyqtSignal()

class EventsWatcher(FileSystemEventHandler):
    def __init__(self, signal):
        super().__init__()
        self._sig = signal
        self._target = os.path.basename(EVENTS_FILE)

    def _maybe(self, path):
        try:
            if path and os.path.basename(path) == self._target:
                self._sig.changed.emit()
        except Exception:
            pass

    def on_modified(self, event): self._maybe(event.src_path)
    def on_created(self, event):  self._maybe(event.src_path)
    def on_moved(self, event):
        self._maybe(getattr(event, "dest_path", "") or "")
        self._maybe(event.src_path)

# ── paper sound ───────────────────────────────────────────────────────────────

PAPER_SOUND = os.path.join(ASSETS, "paper.ogg")

def play_paper():
    if os.path.exists(PAPER_SOUND):
        subprocess.Popen(["mpv","--no-video","--volume=60", PAPER_SOUND],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── calendar page widget ──────────────────────────────────────────────────────

# The calendar grid is drawn on top of the background image.
# These offsets position the grid onto the dark notebook page in the image.
# Tune these after first run.
GRID_LEFT   = 150  # shifted right to sit on notebook
GRID_TOP    = 130  # px from window top
GRID_RIGHT  = 570  # px from window left
GRID_BOTTOM = 690  # px from window top

# Background zoom factor — >1 zooms in
BG_ZOOM = 1.35

DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

FONT_SMALL  = QFont("monospace", 8)
FONT_MED    = QFont("monospace", 11, QFont.Weight.Bold)
FONT_LARGE  = QFont("monospace", 13, QFont.Weight.Bold)
FONT_HEADER = QFont("VT323",     22, QFont.Weight.Bold)

class CalendarPage(QWidget):
    date_clicked = pyqtSignal(str)  # emits YYYY-MM-DD

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg_px     = None
        self._events    = {}
        self._year      = date.today().year
        self._month     = date.today().month
        self._today     = date.today()
        self._load_bg()
        self._load_events()
        self.setMinimumSize(550, 780)

    def _load_bg(self):
        h  = int(time.strftime("%H"))
        p  = bg_for_hour(h)
        if os.path.exists(p):
            self._bg_px = QPixmap(p)

    def refresh_events(self):
        self._load_events()
        self.update()

    def _load_events(self):
        self._events = load_events()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        mx, my = e.position().x(), e.position().y()
        gl  = GRID_LEFT
        gr  = GRID_RIGHT
        gt  = GRID_TOP        # top of entire grid area
        gb  = GRID_BOTTOM

        grid_w   = gr - gl
        grid_h   = gb - gt
        cell_w   = grid_w / 7
        # match paint: row_h uses same formula as _draw_calendar
        row_h    = (grid_h - 30) / 7

        # date grid starts BELOW the day-name row (28px)
        date_top = gt + 28

        cal = calendar.monthcalendar(self._year, self._month)

        if not (gl <= mx <= gr and date_top <= my <= gb):
            return

        col = int((mx - gl) / cell_w)
        row = int((my - date_top) / row_h)
        if 0 <= row < len(cal) and 0 <= col < 7:
            day = cal[row][col]
            if day != 0:
                date_str = date(self._year, self._month, day).strftime("%Y-%m-%d")
                self.date_clicked.emit(date_str)

    def navigate(self, delta: int):
        m = self._month + delta
        y = self._year
        while m > 12: m -= 12; y += 1
        while m < 1:  m += 12; y -= 1
        self._month = m
        self._year  = y
        self.update()

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()

        # background — zoomed in
        if self._bg_px and not self._bg_px.isNull():
            target_w = int(w * BG_ZOOM)
            target_h = int(h * BG_ZOOM)
            s = self._bg_px.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            ox = (s.width()-w)//2; oy = (s.height()-h)//2
            p.drawPixmap(0,0,s,ox,oy,w,h)
        else:
            p.fillRect(self.rect(), QColor(15,12,25))

        self._draw_calendar(p)
        p.end()

    def _draw_calendar(self, p: QPainter):
        gl = GRID_LEFT
        gt = GRID_TOP
        gr = GRID_RIGHT
        gb = GRID_BOTTOM

        grid_w = gr - gl
        grid_h = gb - gt

        # header: month name + year
        p.setFont(FONT_HEADER)
        month_name = datetime(self._year, self._month, 1).strftime("%B %Y").upper()
        p.setPen(QColor(220, 220, 200, 220))
        header_rect = QRect(gl, gt - 50, grid_w, 46)
        p.drawText(header_rect, Qt.AlignmentFlag.AlignCenter, month_name)

        # day name row
        cell_w = grid_w / 7
        row_h  = (grid_h - 30) / 7   # 6 week rows + 1 day-name row

        p.setFont(FONT_MED)
        for i, name in enumerate(DAY_NAMES):
            x = gl + i * cell_w
            r = QRect(int(x), gt, int(cell_w), 28)
            color = QColor(180,120,80,200) if name in ("Sa","Su") else QColor(180,180,160,180)
            p.setPen(color)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, name)

        # get month grid — needed to know how many weeks to draw
        cal        = calendar.monthcalendar(self._year, self._month)
        today      = self._today
        is_current = (self._year == today.year and self._month == today.month)

        # grid lines — thicker and more visible
        p.setPen(QPen(QColor(255,255,255,55), 1.2))
        for i in range(8):
            x = gl + i * cell_w
            p.drawLine(int(x), gt+28, int(x), gb)
        # only draw as many rows as the month actually needs
        weeks_in_month = len(cal)
        for i in range(weeks_in_month + 1):
            y = gt + 28 + i * row_h
            p.drawLine(gl, int(y), gr, int(y))
        # update grid bottom to match actual content height
        gb = int(gt + 28 + weeks_in_month * row_h)

        # draw cells

        for row_i, week in enumerate(cal):
            for col_i, day in enumerate(week):
                if day == 0:
                    continue
                x  = gl + col_i * cell_w
                y  = gt + 28 + (row_i + 0.08) * row_h
                cw = int(cell_w)
                ch = int(row_h * 0.92)

                cell_date = date(self._year, self._month, day)
                date_str  = cell_date.strftime("%Y-%m-%d")
                is_today  = (cell_date == today)
                is_past   = (cell_date < today)

                # day number
                p.setFont(FONT_MED)
                if is_past:
                    p.setPen(QColor(180, 180, 160, 120))
                elif is_today:
                    p.setPen(QColor(255, 255, 255, 240))
                else:
                    p.setPen(QColor(210, 210, 190, 200))

                num_rect = QRect(int(x), int(y), cw, 22)
                p.drawText(num_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                           str(day))

                # today: circle
                if is_today:
                    cx  = int(x + 12)
                    cy  = int(y + 11)
                    rad = 11
                    pen = QPen(QColor(220, 80, 80, 220), 1.5)
                    p.setPen(pen)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawEllipse(cx - rad, cy - rad, rad*2, rad*2)

                # past: red X over number
                if is_past:
                    pen = QPen(QColor(200, 50, 50, 140), 1.2)
                    p.setPen(pen)
                    nx1 = int(x + 2);  ny1 = int(y + 2)
                    nx2 = int(x + 20); ny2 = int(y + 20)
                    p.drawLine(nx1, ny1, nx2, ny2)
                    p.drawLine(nx2, ny1, nx1, ny2)

                # events: colored dots in up to 2 rows of 4
                day_events = self._events.get(date_str, [])
                dot_r   = 3
                spacing = dot_r * 2 + 3
                max_per_row = 4
                dot_start_y = int(y + row_h * 0.45)

                for ei, ev in enumerate(day_events[:8]):
                    row = ei // max_per_row
                    col = ei %  max_per_row
                    dots_in_row = min(max_per_row, len(day_events) - row * max_per_row)
                    row_start_x = int(x + cw/2 - (dots_in_row * spacing)/2 + dot_r)
                    dx = row_start_x + col * spacing - dot_r
                    dy = dot_start_y + row * (dot_r*2 + 3)
                    color = cat_color(ev.get("category",""))
                    color.setAlpha(220)
                    p.setBrush(QBrush(color))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawEllipse(dx, dy - dot_r, dot_r*2, dot_r*2)

# ── date detail dialog ────────────────────────────────────────────────────────

from PyQt6.QtWidgets import (
    QDialog, QComboBox, QFormLayout, QLineEdit, QDialogButtonBox,
    QTextEdit, QScrollArea, QSplitter,
)

NOTES_FILE = os.path.join(ASSETS, "notes.json")

def load_notes() -> dict:
    if not os.path.exists(NOTES_FILE): return {}
    with open(NOTES_FILE) as f:
        try: return json.load(f)
        except: return {}

def save_note(date_str: str, text: str):
    notes = load_notes()
    notes[date_str] = text
    with open(NOTES_FILE, "w") as f: json.dump(notes, f, indent=2)

DIALOG_STYLE = """
    QDialog{
        background:#2e2b24;
        color:#d6cdb8;
        border:1px solid #5a5040;
    }
    QLabel{
        color:#b8a98a;
        font-family:Georgia,serif;
        font-size:12px;
    }
    QTextEdit{
        background:#262318;
        color:#d6cdb8;
        border:none;
        border-bottom:1px solid rgba(180,160,120,0.3);
        font-family:Georgia,serif;
        font-size:12px;
        padding:6px;
        selection-background-color:#5a4a30;
    }
    QTextEdit:focus{
        border-bottom:1px solid rgba(180,160,120,0.7);
    }
    QPushButton{
        background:rgba(60,52,38,0.85);
        color:#c8b898;
        border:1px solid rgba(140,120,80,0.45);
        border-radius:2px;
        padding:5px 14px;
        font-size:11px;
        font-family:Georgia,serif;
    }
    QPushButton:hover{
        background:rgba(80,68,48,0.9);
        border-color:rgba(180,160,100,0.7);
        color:#e0d0b0;
    }
    QPushButton#save{
        background:rgba(45,55,38,0.9);
        border:1px solid rgba(100,140,70,0.55);
        color:#b8d49a;
        font-weight:bold;
    }
    QPushButton#save:hover{
        background:rgba(55,68,45,0.95);
        border-color:rgba(130,175,90,0.75);
    }
"""

class DateDetailDialog(QDialog):
    def __init__(self, date_str: str, events: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(date_str)
        self.setMinimumSize(560, 360)
        self.setStyleSheet(DIALOG_STYLE)
        self._date_str = date_str
        self._events   = events
        self._build()

    def _build(self):
        from PyQt6.QtWidgets import QFrame
        l = QVBoxLayout(self)
        l.setContentsMargins(20,20,20,20); l.setSpacing(14)

        # header
        dt = datetime.strptime(self._date_str, "%Y-%m-%d")
        hdr = QLabel(dt.strftime("%A, %d %B %Y"))
        hdr.setStyleSheet("color:#e0d0b0;font-size:15px;font-family:Georgia,serif;font-weight:bold;")
        l.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:rgba(255,255,255,0.1);")
        l.addWidget(sep)

        # split: events left, notes right
        split = QHBoxLayout(); split.setSpacing(16)

        # events list
        self._ev_col = QVBoxLayout(); self._ev_col.setSpacing(6)
        ev_col = self._ev_col
        ev_hdr = QLabel("Events")
        ev_hdr.setStyleSheet("color:#a09070;font-size:10px;letter-spacing:2px;font-family:Georgia,serif;font-weight:bold;")
        ev_col.addWidget(ev_hdr)

        if self._events:
            for ev in self._events:
                color = cat_color(ev.get("category",""))
                dot   = "●"
                row   = QHBoxLayout()
                dot_lbl = QLabel(dot)
                dot_lbl.setStyleSheet(
                    f"color:rgb({color.red()},{color.green()},{color.blue()});"
                    "font-size:14px;font-family:Georgia,serif;")
                dot_lbl.setFixedWidth(20)
                appt_lbl = QLabel(ev.get("appointment",""))
                appt_lbl.setStyleSheet("color:#d0c4a8;font-size:12px;font-family:Georgia,serif;")
                row.addWidget(dot_lbl); row.addWidget(appt_lbl); row.addStretch()
                del_btn = QPushButton("✕")
                del_btn.setFixedSize(20, 20)
                del_btn.setStyleSheet(
                    "background:transparent;border:none;color:rgba(220,80,80,0.7);"
                    "font-size:12px;font-weight:bold;padding:0;")
                _ev = ev
                del_btn.clicked.connect(lambda checked, e=_ev: self._delete_event(e))
                row.addWidget(del_btn)
                ev_col.addLayout(row)
        else:
            empty = QLabel("no events")
            empty.setStyleSheet("color:rgba(160,140,100,0.6);font-family:Georgia,serif;font-size:11px;font-style:italic;")
            ev_col.addWidget(empty)

        ev_col.addStretch()
        split.addLayout(ev_col, 1)

        # vertical divider
        vline = QFrame(); vline.setFrameShape(QFrame.Shape.VLine)
        vline.setStyleSheet("color:rgba(255,255,255,0.1);"); split.addWidget(vline)

        # notes
        note_col = QVBoxLayout(); note_col.setSpacing(6)
        note_hdr = QLabel("Notes")
        note_hdr.setStyleSheet("color:#a09070;font-size:10px;letter-spacing:2px;font-family:Georgia,serif;font-weight:bold;")
        note_col.addWidget(note_hdr)

        self._note_edit = QTextEdit()
        self._note_edit.setPlaceholderText("add notes…")
        notes = load_notes()
        self._note_edit.setText(notes.get(self._date_str,""))
        note_col.addWidget(self._note_edit, 1)

        save_btn = QPushButton("save note"); save_btn.setObjectName("save")
        save_btn.clicked.connect(self._save_note)
        note_col.addWidget(save_btn)
        split.addLayout(note_col, 1)

        l.addLayout(split, 1)

        close_btn = QPushButton("close")
        close_btn.setStyleSheet(
            "background:rgba(60,52,38,0.85);color:#c8b898;border:1px solid rgba(140,120,80,0.45);"
            "border-radius:2px;padding:5px 14px;font-family:Georgia,serif;font-size:11px;")
        close_btn.setIcon(close_btn.style().standardIcon(
            close_btn.style().StandardPixmap.SP_DialogCloseButton))
        from PyQt6.QtGui import QIcon
        close_btn.setIcon(QIcon())  # remove icon entirely
        close_btn.clicked.connect(self.reject)
        brow = QHBoxLayout(); brow.addStretch(); brow.addWidget(close_btn)
        l.addLayout(brow)

    def _delete_event(self, ev):
        write_event("delete", ev.get("category",""), ev.get("appointment",""), self._date_str)
        self._events = [e for e in self._events if e is not ev]
        self._rebuild_events()

    def _rebuild_events(self):
        """Clear and rebuild the events section without closing the dialog."""
        # clear old event widgets
        while self._ev_col.count():
            item = self._ev_col.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget(): sub.widget().deleteLater()

        ev_hdr = QLabel("Events")
        ev_hdr.setStyleSheet("color:#a09070;font-size:10px;letter-spacing:2px;font-family:Georgia,serif;font-weight:bold;")
        self._ev_col.addWidget(ev_hdr)

        if self._events:
            for ev in self._events:
                color = cat_color(ev.get("category",""))
                row = QHBoxLayout()
                dot_lbl = QLabel("●")
                dot_lbl.setStyleSheet(
                    f"color:rgb({color.red()},{color.green()},{color.blue()});"
                    "font-size:14px;font-family:Georgia,serif;")
                dot_lbl.setFixedWidth(20)
                appt_lbl = QLabel(ev.get("appointment",""))
                appt_lbl.setStyleSheet("color:#d0c4a8;font-size:12px;font-family:Georgia,serif;")
                del_btn = QPushButton("✕")
                del_btn.setFixedSize(20,20)
                del_btn.setStyleSheet(
                    "background:transparent;border:none;color:rgba(220,80,80,0.7);"
                    "font-size:12px;font-weight:bold;padding:0;")
                del_btn.clicked.connect(lambda checked, e=ev: self._delete_event(e))
                row.addWidget(dot_lbl); row.addWidget(appt_lbl); row.addStretch(); row.addWidget(del_btn)
                self._ev_col.addLayout(row)
        else:
            empty = QLabel("no events")
            empty.setStyleSheet("color:rgba(160,140,100,0.6);font-family:Georgia,serif;font-size:11px;font-style:italic;")
            self._ev_col.addWidget(empty)

        self._ev_col.addStretch()

    def _save_note(self):
        save_note(self._date_str, self._note_edit.toPlainText())

# ── add event dialog ──────────────────────────────────────────────────────────

class AddEventDialog(QDialog):
    CATS = [
        "Obligations",
        "Social Engagements",
        "Administrative Tasks",
        "Personal Development",
        "Logistics",
    ]

    def __init__(self, year, month, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Event")
        self.setMinimumWidth(360)
        self.setStyleSheet("""
            QDialog{background:#2e2b24;color:#d6cdb8;border:1px solid #5a5040;}
            QLabel{color:#b8a98a;font-size:12px;font-family:Georgia,serif;}
            QLineEdit,QComboBox{
                background:#262318;color:#d6cdb8;
                border:none;border-bottom:1px solid rgba(180,160,120,0.35);
                padding:6px 4px;font-size:13px;font-family:Georgia,serif;
            }
            QLineEdit:focus,QComboBox:focus{border-bottom:1px solid rgba(180,160,100,0.75);}
            QComboBox QAbstractItemView{background:#262318;color:#d6cdb8;selection-background-color:#4a4030;}
            QPushButton{background:rgba(60,52,38,0.85);color:#c8b898;
                border:1px solid rgba(140,120,80,0.45);border-radius:2px;
                padding:6px 14px;font-size:12px;font-family:Georgia,serif;}
            QPushButton:hover{background:rgba(80,68,48,0.9);border-color:rgba(180,160,100,0.7);color:#e0d0b0;}
            QPushButton#ok{background:rgba(45,55,38,0.9);border:1px solid rgba(100,140,70,0.55);color:#b8d49a;font-weight:bold;}
            QPushButton#ok:hover{background:rgba(55,68,45,0.95);border-color:rgba(130,175,90,0.75);}
        """)
        self.category    = ""
        self.appointment = ""
        self.date_str    = ""
        self._year  = year
        self._month = month
        self._build()

    def _build(self):
        today = date.today()
        l = QVBoxLayout(self)
        l.setContentsMargins(24,24,24,24); l.setSpacing(16)

        t = QLabel("Add Event")
        t.setStyleSheet("color:#e0d0b0;font-size:15px;font-family:Georgia,serif;font-weight:bold;")
        l.addWidget(t)

        form = QFormLayout(); form.setSpacing(14)

        self._date_edit = QLineEdit(
            f"{self._year}-{self._month:02d}-{today.day:02d}")
        self._date_edit.setPlaceholderText("YYYY-MM-DD")
        form.addRow("date :", self._date_edit)

        self._cat_combo = QComboBox()
        for c in self.CATS:
            self._cat_combo.addItem(c)
        form.addRow("category :", self._cat_combo)

        self._appt_edit = QLineEdit()
        self._appt_edit.setPlaceholderText("event name…")
        form.addRow("event :", self._appt_edit)

        l.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setObjectName("ok")
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("confirm")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("cancel")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        l.addWidget(btns)

    def _ok(self):
        self.category    = self._cat_combo.currentText()
        self.appointment = self._appt_edit.text().strip()
        self.date_str    = self._date_edit.text().strip()
        if not self.appointment or not self.date_str:
            return
        self.accept()


# ── main window ───────────────────────────────────────────────────────────────

class CalendarWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Neural Calendar")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(550, 780)

        self._drag_pos = None
        self._build()
        self._start_watcher()

        # refresh bg every minute
        self._bg_t = QTimer()
        self._bg_t.timeout.connect(self._refresh_bg)
        self._bg_t.start(60_000)

    def _build(self):
        root = QWidget(); self.setCentralWidget(root)
        root.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        rl = QVBoxLayout(root); rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)

        self._page = CalendarPage()
        self._page.date_clicked.connect(self._show_date_detail)
        rl.addWidget(self._page, 1)

        # floating arrow buttons as overlay on top of page
        self._prev_btn = QPushButton("◀", self._page)
        self._prev_btn.setFixedSize(34,34)
        self._prev_btn.setStyleSheet(self._btn_style())
        self._prev_btn.clicked.connect(self._prev_month)
        self._prev_btn.move(GRID_LEFT, GRID_TOP - 48)

        self._next_btn = QPushButton("▶", self._page)
        self._next_btn.setFixedSize(34,34)
        self._next_btn.setStyleSheet(self._btn_style())
        self._next_btn.clicked.connect(self._next_month)
        self._next_btn.move(GRID_RIGHT - 38, GRID_TOP - 48)

        # plus button bottom right of calendar page
        self._plus_btn = QPushButton("+", self._page)
        self._plus_btn.setFixedSize(38,38)
        self._plus_btn.setStyleSheet(
            "background:rgba(60,52,38,0.88);border:1px solid rgba(140,120,80,0.55);"
            "border-radius:4px;color:#d0c4a8;font-size:20px;font-weight:bold;")
        self._plus_btn.clicked.connect(self._add_event)
        self._plus_btn.move(GRID_RIGHT - 230, GRID_BOTTOM - 142)

    def _show_date_detail(self, date_str: str):
        events = self._page._events.get(date_str, [])
        dlg = DateDetailDialog(date_str, events, self)
        dlg.exec()

    def _add_event(self):
        dlg = AddEventDialog(self._page._year, self._page._month, self)
        if dlg.exec():
            write_event("create", dlg.category, dlg.appointment, dlg.date_str)
            self._page.refresh_events()

    def _btn_style(self):
        return ("background:rgba(60,52,38,0.75);border:1px solid rgba(140,120,80,0.4);"
                "border-radius:4px;color:rgba(210,190,150,0.85);font-size:14px;font-weight:bold;")

    def _prev_month(self):
        play_paper()
        self._page.navigate(-1)

    def _next_month(self):
        play_paper()
        self._page.navigate(+1)

    def _refresh_bg(self):
        self._page._load_bg()
        self._page.update()

    # ── file watcher ──────────────────────────────────────────────────────────

    def _start_watcher(self):
        self._sig = _Sig()
        self._sig.changed.connect(self._page.refresh_events)
        handler = EventsWatcher(self._sig)
        self._observer = Observer()
        os.makedirs(ASSETS, exist_ok=True)
        self._observer.schedule(handler, ASSETS, recursive=False)
        self._observer.start()

        # polling fallback (cheap insurance if watchdog misses an event)
        self._poll_mtime = self._file_mtime()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_check)
        self._poll_timer.start(1500)

    def _file_mtime(self):
        try: return os.path.getmtime(EVENTS_FILE)
        except Exception: return 0

    def _poll_check(self):
        m = self._file_mtime()
        if m != self._poll_mtime:
            self._poll_mtime = m
            self._page.refresh_events()

    def closeEvent(self, e):
        self._observer.stop()
        self._observer.join()
        super().closeEvent(e)

    # ── drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag_pos)
    def mouseReleaseEvent(self, e): self._drag_pos=None


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = CalendarWindow()
    win.show()
    sys.exit(app.exec())
