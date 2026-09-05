#!/usr/bin/env python3
"""
Klok — digital alarm clock app
Three modes: Alarm | Timer | Reminders
Background: Clockday.png / Clocknight.png
Size: 618x212
"""

import sys, os, json, time, subprocess
from datetime import datetime, date, timedelta
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QDialog, QLineEdit, QSpinBox,
    QComboBox, QFormLayout, QCheckBox, QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QTime, QObject

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except Exception:
    HAS_WATCHDOG = False

class _Sig(QObject):
    klok_changed  = pyqtSignal()
    timer_changed = pyqtSignal()
from PyQt6.QtGui import QPixmap, QColor, QPainter, QFont, QPen, QBrush, QLinearGradient

# ── paths ─────────────────────────────────────────────────────────────────────

ASSETS     = os.path.expanduser("~/.config/MASTER_VAULT/HOME_ASSIST/Klok")
DATA_FILE  = os.path.join(ASSETS, "klok.json")
TIMER_FILE = os.path.join(ASSETS, "timer.json")
SOUNDS_DIR = os.path.join(ASSETS, "sounds")

def bg_for_hour(h):
    if 6 <= h < 19:
        return os.path.join(ASSETS, "Clockday.png")
    return os.path.join(ASSETS, "Clocknight.png")

# ── data ──────────────────────────────────────────────────────────────────────

def load_data():
    if not os.path.exists(DATA_FILE): return {"alarms":[], "reminders":[]}
    with open(DATA_FILE) as f:
        try: return json.load(f)
        except: return {"alarms":[], "reminders":[]}

def save_data(data):
    os.makedirs(ASSETS, exist_ok=True)
    with open(DATA_FILE, "w") as f: json.dump(data, f, indent=2)

def load_timer():
    if not os.path.exists(TIMER_FILE): return {}
    try:
        with open(TIMER_FILE) as f: return json.load(f) or {}
    except Exception: return {}

def save_timer(d):
    os.makedirs(ASSETS, exist_ok=True)
    tmp = TIMER_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(d, f, indent=2)
    os.replace(tmp, TIMER_FILE)

def clear_timer():
    save_timer({})

# ── sound ─────────────────────────────────────────────────────────────────────

def play_alarm():
    path = os.path.join(ASSETS, "sounds", "alarm_sound.mp3")
    if not os.path.exists(path):
        sounds = [f for f in os.listdir(SOUNDS_DIR) if f.endswith((".mp3",".ogg",".wav"))] \
                 if os.path.isdir(SOUNDS_DIR) else []
        if sounds: path = os.path.join(SOUNDS_DIR, sounds[0])
        else: return
    subprocess.Popen(["mpv","--no-video","--loop=inf", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen("eww --config ~/.config/eww open btn-klok-stop",
                     shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── 7-segment digit display ───────────────────────────────────────────────────

# Segment map: which segments are on for each digit 0-9
# Segments: top, top-left, top-right, mid, bot-left, bot-right, bot
SEGS = {
    '0': [1,1,1,0,1,1,1],
    '1': [0,0,1,0,0,1,0],
    '2': [1,0,1,1,1,0,1],
    '3': [1,0,1,1,0,1,1],
    '4': [0,1,1,1,0,1,0],
    '5': [1,1,0,1,0,1,1],
    '6': [1,1,0,1,1,1,1],
    '7': [1,0,1,0,0,1,0],
    '8': [1,1,1,1,1,1,1],
    '9': [1,1,1,1,0,1,1],
    ':': None,
    ' ': [0,0,0,0,0,0,0],
}

class SevenSegDisplay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = "00:00"
        self._color = QColor(15, 40, 80)
        self.setMinimumSize(260, 70)

    def setText(self, t): self._text = t; self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        digit_w = 28
        digit_h = 56
        colon_w = 14
        gap     = 6
        # calc total width
        total = 0
        for ch in self._text:
            total += colon_w if ch == ':' else digit_w + gap
        start_x = (w - total) // 2
        start_y = (h - digit_h) // 2

        x = start_x
        for ch in self._text:
            if ch == ':':
                # two dots
                dot_r = 4
                cx = x + colon_w // 2
                p.setBrush(QBrush(self._color))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(cx - dot_r, start_y + digit_h//3 - dot_r, dot_r*2, dot_r*2)
                p.drawEllipse(cx - dot_r, start_y + 2*digit_h//3 - dot_r, dot_r*2, dot_r*2)
                x += colon_w
            else:
                segs = SEGS.get(ch, SEGS[' '])
                self._draw_digit(p, x, start_y, digit_w, digit_h, segs)
                x += digit_w + gap

    def _draw_digit(self, p, x, y, dw, dh, segs):
        c_on  = self._color
        c_off = QColor(80, 130, 180, 50)
        sw = 4   # segment width
        gap = 3  # gap from edges

        def seg(on, x1,y1, x2,y2, horizontal):
            color = c_on if on else c_off
            p.setBrush(QBrush(color)); p.setPen(Qt.PenStyle.NoPen)
            if horizontal:
                pts = [
                    (x1+gap, y1), (x2-gap, y1),
                    (x2-gap-sw//2, y1+sw), (x1+gap+sw//2, y1+sw)
                ]
            else:
                pts = [
                    (x1, y1+gap), (x1+sw, y1+gap+sw//2),
                    (x1+sw, y2-gap-sw//2), (x1, y2-gap)
                ]
            from PyQt6.QtGui import QPolygon
            from PyQt6.QtCore import QPoint
            poly = QPolygon([QPoint(int(px), int(py)) for px,py in pts])
            p.drawPolygon(poly)

        hw = dh // 2
        # top
        seg(segs[0], x, y,            x+dw, y,            True)
        # top-left
        seg(segs[1], x, y,            x,    y+hw,          False)
        # top-right
        seg(segs[2], x+dw-sw, y,      x+dw-sw, y+hw,      False)
        # mid
        seg(segs[3], x, y+hw,         x+dw, y+hw,         True)
        # bot-left
        seg(segs[4], x, y+hw,         x,    y+dh,          False)
        # bot-right
        seg(segs[5], x+dw-sw, y+hw,   x+dw-sw, y+dh,      False)
        # bot
        seg(segs[6], x, y+dh,         x+dw, y+dh,         True)

# ── background canvas ─────────────────────────────────────────────────────────

class BgCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._px = None; self.reload()
    def reload(self):
        h = int(time.strftime("%H"))
        p = bg_for_hour(h)
        self._px = QPixmap(p) if os.path.exists(p) else None
        self.update()
    def paintEvent(self, _):
        p = QPainter(self)
        r = self.rect()
        if self._px and not self._px.isNull():
            s = self._px.scaled(r.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            ox=(s.width()-r.width())//2; oy=(s.height()-r.height())//2
            p.drawPixmap(0,0,s,ox,oy,r.width(),r.height())
        else:
            p.fillRect(r, QColor(10,20,60))
        p.end()

# ── add dialog ────────────────────────────────────────────────────────────────

DLG_STYLE = """
QDialog{background:#060a18;color:#e0eeff;}
QLabel{color:#8090c0;font-family:monospace;font-size:11px;}
QLineEdit,QSpinBox,QComboBox{
    background:#0a1228;color:#c0d8ff;border:none;
    border-bottom:1px solid rgba(30,160,255,0.3);
    padding:4px;font-size:12px;font-family:monospace;}
QLineEdit:focus,QSpinBox:focus,QComboBox:focus{border-bottom:1px solid rgba(30,160,255,0.8);}
QComboBox QAbstractItemView{background:#0a1228;color:#c0d8ff;selection-background-color:#1a2a50;}
QCheckBox{color:#8090c0;font-family:monospace;}
QPushButton{background:transparent;color:#c0d8ff;border:1px solid rgba(30,160,255,0.3);
    border-radius:2px;padding:4px 12px;font-size:11px;font-family:monospace;}
QPushButton:hover{border-color:rgba(30,160,255,0.8);}
QPushButton#ok{background:rgba(0,60,140,0.7);border-color:rgba(30,160,255,0.6);font-weight:bold;}
"""

class AddAlarmDialog(QDialog):
    def __init__(self, mode, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle(f"Add {mode}")
        self.setMinimumWidth(300)
        self.setStyleSheet(DLG_STYLE)
        self._build()

    def _build(self):
        l = QVBoxLayout(self); l.setContentsMargins(18,18,18,18); l.setSpacing(12)
        hdr = QLabel(f"// {self.mode.upper()}")
        hdr.setStyleSheet("color:#1ea0ff;font-size:13px;font-family:monospace;letter-spacing:2px;")
        l.addWidget(hdr)
        form = QFormLayout(); form.setSpacing(10)

        if self.mode == "Timer":
            self._hrs  = QSpinBox(); self._hrs.setRange(0,23);  self._hrs.setValue(0)
            self._mins = QSpinBox(); self._mins.setRange(0,59); self._mins.setValue(5)
            self._secs = QSpinBox(); self._secs.setRange(0,59); self._secs.setValue(0)
            row = QHBoxLayout()
            for w,lbl in [(self._hrs,"h"),(self._mins,"m"),(self._secs,"s")]:
                row.addWidget(w); row.addWidget(QLabel(lbl))
            form.addRow("duration:", row)
            self._label = QLineEdit(); self._label.setPlaceholderText("label (optional)")
            form.addRow("label:", self._label)
        else:
            self._hour = QSpinBox(); self._hour.setRange(1,12); self._hour.setValue(8)
            self._min  = QSpinBox(); self._min.setRange(0,59);  self._min.setValue(0)
            self._ampm = QComboBox(); self._ampm.addItems(["AM","PM"])
            t_row = QHBoxLayout()
            t_row.addWidget(self._hour); t_row.addWidget(QLabel(":")); t_row.addWidget(self._min)
            t_row.addWidget(self._ampm)
            form.addRow("time:", t_row)
            self._label = QLineEdit(); self._label.setPlaceholderText("label")
            form.addRow("label:", self._label)
            if self.mode == "Alarm":
                days = ["Mo","Tu","We","Th","Fr","Sa","Su"]
                self._day_checks = []
                day_row = QHBoxLayout()
                for d in days:
                    cb = QCheckBox(d); cb.setChecked(True)
                    self._day_checks.append(cb); day_row.addWidget(cb)
                form.addRow("days:", day_row)

        l.addLayout(form)
        btns = QHBoxLayout(); btns.addStretch()
        cancel = QPushButton("cancel"); cancel.clicked.connect(self.reject)
        ok = QPushButton("add"); ok.setObjectName("ok"); ok.clicked.connect(self.accept)
        btns.addWidget(cancel); btns.addWidget(ok)
        l.addLayout(btns)

    def result_data(self):
        if self.mode == "Timer":
            secs = self._hrs.value()*3600 + self._mins.value()*60 + self._secs.value()
            return {"type":"timer","duration":secs,"remaining":secs,
                    "label":self._label.text().strip(),"active":False,"id":int(time.time()*1000)}
        elif self.mode == "Alarm":
            days = [i for i,cb in enumerate(self._day_checks) if cb.isChecked()]
            h = self._hour.value() % 12
            if self._ampm.currentText() == "PM": h += 12
            return {"type":"alarm","hour":h,"minute":self._min.value(),
                    "label":self._label.text().strip(),"days":days,
                    "active":True,"id":int(time.time()*1000)}
        else:  # Reminder
            h = self._hour.value() % 12
            if self._ampm.currentText() == "PM": h += 12
            return {"type":"reminder","hour":h,"minute":self._min.value(),
                    "label":self._label.text().strip(),"active":True,
                    "id":int(time.time()*1000)}


class _FadeLine(QWidget):
    """Vertical line that fades at top and bottom."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    def paintEvent(self, _):
        from PyQt6.QtGui import QLinearGradient
        p = QPainter(self)
        w, h = self.width(), self.height()
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(15,40,80,0))
        grad.setColorAt(0.25, QColor(15,40,80,160))
        grad.setColorAt(0.75, QColor(15,40,80,160))
        grad.setColorAt(1.0, QColor(15,40,80,0))
        p.fillRect(0, 0, w, h, grad)
        p.end()

# ── main window ───────────────────────────────────────────────────────────────

STYLE = """
QMainWindow,QWidget#root{background:transparent;}
QPushButton#tab{
    background:transparent;border:none;border-bottom:2px solid transparent;
    color:rgba(100,160,255,0.5);font-size:12px;font-family:monospace;
    font-weight:bold;padding:2px 4px;letter-spacing:1px;
}
QPushButton#tab[active=true]{
    color:rgba(30,160,255,1);border-bottom:2px solid rgba(30,160,255,0.8);font-size:14px;
}
QPushButton#add{
    background:rgba(0,40,120,0.6);border:1px solid rgba(30,160,255,0.4);
    border-radius:3px;color:rgba(30,160,255,0.9);font-size:11px;
    font-family:monospace;padding:2px 8px;
}
QPushButton#add:hover{border-color:rgba(30,160,255,0.9);}
QPushButton#del{
    background:transparent;border:none;color:rgba(180,20,20,0.9);font-size:12px;
}
QPushButton#del:hover{color:rgba(255,80,80,0.9);}
QPushButton#tog{
    background:transparent;border:none;color:rgba(10,40,120,0.85);font-size:12px;
}
QPushButton#tog:hover{color:rgba(30,160,255,0.9);}
QScrollArea{background:transparent;border:none;}
QScrollBar:vertical{background:transparent;width:4px;}
QScrollBar::handle:vertical{background:rgba(30,160,255,0.3);border-radius:2px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
QLabel#item{color:rgba(20,20,40,0.95);font-family:monospace;font-size:16px;font-weight:bold;}
QLabel#item_off{color:rgba(20,20,40,0.45);font-family:monospace;font-size:16px;}
QPushButton#arw{background:transparent;border:none;color:rgba(15,40,80,0.7);font-size:9px;padding:0;}
QPushButton#arw:hover{color:rgba(15,40,80,1);}
QPushButton#ampm{background:transparent;border:none;font-size:18px;font-family:monospace;padding:0;font-weight:bold;}
"""

class KlokWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Klok")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(618, 212)
        self.setStyleSheet(STYLE)

        self._tab      = "Alarm"
        self._drag_pos = None
        self._data     = load_data()
        self._timer_countdown = {}  # id -> remaining secs

        self._build()

        # main tick — check alarms, update clock
        self._tick_t = QTimer()
        self._tick_t.timeout.connect(self._tick)
        self._tick_t.start(1000)

        self._bg_t = QTimer()
        self._bg_t.timeout.connect(lambda: (self._bg.reload(), self._bg.update()))
        self._bg_t.start(60_000)

        # watch klok.json and timer.json so external (bridge / AI) changes appear live
        self._start_watcher()

    # ── build ──────────────────────────────────────────────────────────────────

    def _build(self):
        stack = QWidget(); self.setCentralWidget(stack)
        stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._bg = BgCanvas(stack)
        self._bg.setGeometry(0,0,self.width(),self.height())

        overlay = QWidget(stack)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        overlay.setGeometry(0,0,self.width(),self.height())
        overlay.raise_()

        # window: 618x212
        # BLACK SIDE buttons 
        btn_x   = 87
        btn_y   = 21
        btn_w   = 90
        btn_h   = 44
        btn_gap = 8
        self._tabs = {}
        self._add_btn = None
        for i, name in enumerate(("Alarm","Timer","Reminders")):
            btn = QPushButton(name, overlay)
            btn.setObjectName("tab")
            btn.setProperty("active", name == self._tab)
            btn.setGeometry(btn_x, btn_y + i*(btn_h+btn_gap), btn_w, btn_h)
            btn.clicked.connect(lambda c,n=name: self._switch_tab(n))
            self._tabs[name] = btn
            btn.raise_()



        # CLOCK — on blue screen: 20% from top=42, right edge at 60% of 618=370
        # x=30, y=42, width=300, height=100
        self._clock_area = QWidget(overlay)
        clock_area = self._clock_area
        clock_area.setGeometry(130, 42, 300, 100)
        clock_area.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        clock_area.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        cv = QVBoxLayout(clock_area); cv.setContentsMargins(0,0,0,0); cv.setSpacing(2)
        self._display = SevenSegDisplay()
        self._display.setMinimumSize(200, 80)
        cv.addWidget(self._display)
        self._sub_lbl = QLabel("Wednesday  03")
        self._sub_lbl.setStyleSheet(
            "color:rgba(15,40,80,0.85);font-family:monospace;font-size:18px;letter-spacing:1px;")
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cv.addWidget(self._sub_lbl)

        # ── ALARM PANEL — right of clock ──────────────────────────────────────
        # Fading vertical divider line (painted widget)
        self._divider = _FadeLine(overlay)
        divider = self._divider
        divider.setGeometry(385, 20, 3, 172)

        panel_x = 390
        panel_w = 120
        panel_h = 212

        # Mini time picker
        self._alarm_h = 0   # hour 0-23
        self._alarm_m = 0   # minute 0-59
        self._alarm_ampm = "AM"

        # Hours display + arrows
        def _lbl(text, px, py, pw, ph, style=""):
            l = QLabel(text, overlay)
            l.setGeometry(px, py, pw, ph)
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if style: l.setStyleSheet(style)
            return l

        def _abtn(text, px, py, cb):
            b = QPushButton(text, overlay)
            b.setObjectName("arw")
            b.setGeometry(px, py, 20, 14)
            b.clicked.connect(cb)
            return b

        dig_style = ("color:rgba(15,40,80,0.9);font-family:monospace;"
                     "font-size:20px;font-weight:bold;")

        # Hour
        _abtn("▲", panel_x+2,  33,  lambda: self._bump_alarm("h",  1))
        self._ah_lbl = _lbl("00", panel_x+2, 47, 24, 22, dig_style)
        _abtn("▼", panel_x+2,  69, lambda: self._bump_alarm("h", -1))

        _lbl(":", panel_x+26, 47, 10, 22, dig_style)

        # Minute
        _abtn("▲", panel_x+36, 33,  lambda: self._bump_alarm("m",  1))
        self._am_lbl = _lbl("00", panel_x+36, 47, 24, 22, dig_style)
        _abtn("▼", panel_x+36, 69, lambda: self._bump_alarm("m", -1))

        # AM / PM
        self._am_btn = QPushButton("AM", overlay)
        self._am_btn.setObjectName("ampm")
        self._am_btn.setGeometry(panel_x+62, 37, 26, 16)
        self._am_btn.clicked.connect(lambda: self._set_ampm("AM"))

        self._pm_btn = QPushButton("PM", overlay)
        self._pm_btn.setObjectName("ampm")
        self._pm_btn.setGeometry(panel_x+62, 55, 26, 16)
        self._pm_btn.clicked.connect(lambda: self._set_ampm("PM"))
        self._update_ampm_display()

        # Add alarm button
        add_al = QPushButton("+ alarm", overlay)
        add_al.setObjectName("add")
        add_al.setGeometry(panel_x+2, 90, 86, 18)
        add_al.clicked.connect(self._add_alarm_quick)

        # Alarm list scroll
        self._list_w = QWidget()
        self._list_w.setStyleSheet("background:rgba(0,0,0,0);")
        self._list_l = QVBoxLayout(self._list_w)
        self._list_l.setContentsMargins(0,0,0,0); self._list_l.setSpacing(1)
        self._list_l.addStretch()

        list_scroll = QScrollArea(overlay)
        list_scroll.setGeometry(390, 112, 120, 36)
        list_scroll.setWidgetResizable(True)
        list_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        list_scroll.setStyleSheet("background:rgba(0,0,0,0);border:none;QScrollBar:vertical{background:rgba(0,0,40,0.3);width:4px;}QScrollBar::handle:vertical{background:rgba(10,40,120,0.6);border-radius:2px;}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        list_scroll.setWidget(self._list_w)

        # ── TIMER PANEL ───────────────────────────────────────────────────────
        self._timer_panel = QWidget(overlay)
        self._timer_panel.setGeometry(185, 0, 618 - 185, 212)
        self._timer_panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._timer_panel.hide()

        # Timer state
        self._timer_h   = 0
        self._timer_m   = 5
        self._timer_s   = 0
        self._timer_running = False
        self._timer_remaining = 0
        self._timer_alarm_proc = None

        tp = self._timer_panel

        # digit style
        tdig = "color:rgba(15,40,80,0.9);font-family:monospace;font-size:28px;font-weight:bold;"
        tdig_red = "color:rgba(200,20,20,0.9);font-family:monospace;font-size:28px;font-weight:bold;"

        cx = 200  # center x of timer display

        ARW_W, ARW_H = 68, 56
        def _tbtn(text, cx, py, cb):
            # cx = center x of the digit this arrow belongs to
            b = QPushButton(text, tp)
            b.setObjectName("arw")
            b.setGeometry(cx - ARW_W//2, py, ARW_W, ARW_H)
            b.setStyleSheet("font-size:22px;")
            b.clicked.connect(cb)
            return b

        def _tlbl(text, px, py, pw, ph, style=""):
            l = QLabel(text, tp)
            l.setGeometry(px, py, pw, ph)
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if style: l.setStyleSheet(style)
            return l

        dw, dh = 32, 36
        gap = 8
        colon_w = 16
        btn_w2 = dw*2
        # panel is offset to x=185; lay out everything in panel-local coords.
        panel_w_local = 618 - 185
        digits_w = dw*2 + colon_w + dw*2 + colon_w + dw*2 + gap*5
        start_gap = 1                            # start button hugs the seconds
        block_w  = digits_w + start_gap + btn_w2
        sx = (panel_w_local - block_w) // 2 - 30  # nudge whole block left
        sy = 70
        arw_up_y = sy - ARW_H - 2
        arw_dn_y = sy + dh + 2

        # Hours
        _tbtn("▲", sx + dw,      arw_up_y, lambda: self._bump_timer("h",  1))
        self._th_lbl = _tlbl("00", sx, sy, dw*2, dh, tdig)
        _tbtn("▼", sx + dw,      arw_dn_y, lambda: self._bump_timer("h", -1))

        _tlbl(":", sx+dw*2+2, sy, colon_w, dh, tdig)

        # Minutes
        mx = sx+dw*2+colon_w+4
        _tbtn("▲", mx + dw,  arw_up_y, lambda: self._bump_timer("m",  1))
        self._tm_lbl = _tlbl("00", mx, sy, dw*2, dh, tdig)
        _tbtn("▼", mx + dw,  arw_dn_y, lambda: self._bump_timer("m", -1))

        _tlbl(":", mx+dw*2+2, sy, colon_w, dh, tdig)

        # Seconds
        ssx = mx+dw*2+colon_w+4
        _tbtn("▲", ssx + dw, arw_up_y, lambda: self._bump_timer("s",  1))
        self._ts_lbl = _tlbl("00", ssx, sy, dw*2, dh, tdig)
        _tbtn("▼", ssx + dw, arw_dn_y, lambda: self._bump_timer("s", -1))

        # Start/Pause button — sits right next to seconds
        self._tstart_btn = QPushButton("start", tp)
        self._tstart_btn.setObjectName("add")
        self._tstart_btn.setGeometry(ssx+dw*2+start_gap, sy, btn_w2, dh)
        self._tstart_btn.clicked.connect(self._toggle_timer_run)

        self._update_timer_display()

        # ── REMINDERS PANEL ───────────────────────────────────────────────────
        self._remind_panel = QWidget(overlay)
        self._remind_panel.setGeometry(185, 0, 618 - 185, 212)
        self._remind_panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._remind_panel.hide()
        rp = self._remind_panel
        rpw = 618 - 185

        # Big date label, top-center
        self._remind_date = QLabel("3 September", rp)
        self._remind_date.setGeometry(-50, 24, rpw, 34)
        self._remind_date.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._remind_date.setStyleSheet(
            "color:rgba(15,40,80,0.9);font-family:monospace;"
            "font-size:26px;font-weight:bold;letter-spacing:1px;")
        self._remind_date.setText(datetime.now().strftime("%-d %B"))

        # Long "add reminder" button below the date
        add_rem = QPushButton("add reminder", rp)
        add_rem.setObjectName("add")
        add_rem.setGeometry(0, 62, rpw - 90, 18)
        add_rem.clicked.connect(self._add_reminder)

        # Reminder list (4 rows visible before scroll). Row height ~34 -> 136px.
        self._rlist_w = QWidget()
        self._rlist_w.setStyleSheet("background:rgba(0,0,0,0);")
        self._rlist_l = QVBoxLayout(self._rlist_w)
        self._rlist_l.setContentsMargins(0,0,0,0); self._rlist_l.setSpacing(2)
        self._rlist_l.addStretch()

        rscroll = QScrollArea(rp)
        rscroll.setGeometry(0, 96, rpw - 90, 65)
        rscroll.setWidgetResizable(True)
        rscroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rscroll.setStyleSheet("background:rgba(0,0,0,0);border:none;QScrollBar:vertical{background:rgba(0,0,40,0.3);width:5px;}QScrollBar::handle:vertical{background:rgba(10,40,120,0.6);border-radius:2px;}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        rscroll.setWidget(self._rlist_w)

        self._refresh_list()
        self._refresh_reminders()

        # collect alarm-panel widgets for show/hide — done here so that
        # _timer_panel and _remind_panel already exist and can be excluded.
        excluded = set(self._tabs.values()) | {self._timer_panel, self._remind_panel, self._clock_area, self._divider}
        self._alarm_widgets = [
            child for child in overlay.children()
            if isinstance(child, QWidget) and child not in excluded
        ]

    def _refresh_reminders(self):
        while self._rlist_l.count() > 1:
            it = self._rlist_l.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        items = [x for x in self._data.get("reminders", [])
                 if x.get("type","reminder") == "reminder"]
        for i, item in enumerate(items):
            self._rlist_l.insertWidget(i, self._make_reminder_row(item))

    def _make_reminder_row(self, item):
        w = QWidget(); w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        w.setFixedHeight(32)
        rl = QHBoxLayout(w); rl.setContentsMargins(4,2,4,2); rl.setSpacing(6)
        h = item["hour"]; ampm = "PM" if h >= 12 else "AM"
        h12 = h % 12 or 12
        title = item.get("label","") or "(no title)"
        txt = f"{h12:02d}:{item['minute']:02d} {ampm}   {title}"
        lbl = QLabel(txt); lbl.setObjectName("item")
        lbl.setStyleSheet("color:rgba(15,40,80,0.9);font-family:monospace;font-size:15px;")
        rl.addWidget(lbl, 1)
        dl = QPushButton("✕"); dl.setObjectName("del"); dl.setFixedSize(22,22)
        dl.setStyleSheet("color:rgba(220,40,40,0.95);background:transparent;border:none;font-size:14px;")
        dl.clicked.connect(lambda c,it=item: self._delete_reminder(it))
        rl.addWidget(dl)
        return w

    def _add_reminder(self):
        dlg = AddAlarmDialog("Reminders", self)
        if dlg.exec():
            d = dlg.result_data()
            self._data.setdefault("reminders", []).append(d)
            save_data(self._data)
            self._refresh_reminders()

    def _delete_reminder(self, item):
        self._data["reminders"] = [x for x in self._data.get("reminders",[])
                                   if x.get("id") != item.get("id")]
        save_data(self._data)
        self._refresh_reminders()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self,"_bg"):
            self._bg.setGeometry(0,0,self.width(),self.height())
            for child in self.centralWidget().children():
                if isinstance(child,QWidget) and child is not self._bg:
                    child.setGeometry(0,0,self.width(),self.height())

    # ── tabs ──────────────────────────────────────────────────────────────────

    def _switch_tab(self, name):
        self._tab = name
        for n,btn in self._tabs.items():
            btn.setProperty("active", n==name)
            btn.setStyleSheet("")
        self._update_panel()

    def _update_panel(self):
        """Show/hide alarm panel vs timer panel vs reminders panel."""
        is_alarm = self._tab == "Alarm"
        is_timer = self._tab == "Timer"
        is_remind = self._tab == "Reminders"
        # clock + divider only on Alarm tab
        self._clock_area.setVisible(is_alarm)
        self._divider.setVisible(is_alarm)
        # alarm panel widgets (alarm tab only)
        for w in self._alarm_widgets:
            w.setVisible(is_alarm)
        # timer panel
        self._timer_panel.setVisible(is_timer)
        # reminders panel
        self._remind_panel.setVisible(is_remind)
        if is_alarm:
            self._refresh_list()
        elif is_remind:
            self._refresh_reminders()

    # ── list ──────────────────────────────────────────────────────────────────

    def _refresh_list(self):
        while self._list_l.count() > 1:
            item = self._list_l.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        key = "reminders" if self._tab == "Reminders" else "alarms"
        type_filter = {"Alarm":"alarm","Timer":"timer","Reminders":"reminder"}[self._tab]
        items = [x for x in self._data.get(key, [])
                 if x.get("type","alarm") == type_filter]

        for i, item in enumerate(items):
            row = self._make_row(item)
            self._list_l.insertWidget(i, row)

    def _make_row(self, item):
        w = QWidget(); w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        rl = QHBoxLayout(w); rl.setContentsMargins(1,1,1,1); rl.setSpacing(2)

        active = item.get("active", True)
        lbl_cls = "item" if active else "item_off"

        if item["type"] == "timer":
            rem = self._timer_countdown.get(item["id"], item.get("remaining", item["duration"]))
            h,r = divmod(rem,3600); m,s = divmod(r,60)
            txt = f"{h:02d}:{m:02d}:{s:02d}"
            if item.get("label"): txt += f"  {item['label']}"
        elif item["type"] == "reminder":
            h = item["hour"]; ampm = "PM" if h >= 12 else "AM"
            h12 = h % 12 or 12
            txt = f"{h12:02d}:{item['minute']:02d} {ampm}"
        else:
            h = item["hour"]; ampm = "PM" if h >= 12 else "AM"
            h12 = h % 12 or 12
            txt = f"{h12:02d}:{item['minute']:02d} {ampm}"

        lbl = QLabel(txt); lbl.setObjectName(lbl_cls)
        rl.addWidget(lbl, 1)

        # toggle (alarm/timer)
        if item["type"] != "reminder":
            tog = QPushButton("●" if active else "○")
            tog.setObjectName("tog"); tog.setFixedSize(18,18)
            tog.clicked.connect(lambda c,it=item: self._toggle(it))
            rl.addWidget(tog)

        # timer start/stop
        if item["type"] == "timer":
            running = item["id"] in self._timer_countdown and item.get("active")
            play = QPushButton("▶" if not running else "⏸")
            play.setObjectName("tog"); play.setFixedSize(18,18)
            play.clicked.connect(lambda c,it=item: self._toggle_timer(it))
            rl.addWidget(play)

        # delete
        dl = QPushButton("✕"); dl.setObjectName("del"); dl.setFixedSize(18,18)
        dl.clicked.connect(lambda c,it=item: self._delete(it))
        rl.addWidget(dl)
        return w

    # ── actions ───────────────────────────────────────────────────────────────

    def _add_item(self):
        dlg = AddAlarmDialog(self._tab, self)
        if dlg.exec():
            d = dlg.result_data()
            key = "reminders" if self._tab=="Reminders" else "alarms"
            self._data.setdefault(key,[]).append(d)
            save_data(self._data)
            self._refresh_list()

    def _toggle(self, item):
        item["active"] = not item.get("active", True)
        save_data(self._data)
        self._refresh_list()

    def _toggle_timer(self, item):
        if item["id"] in self._timer_countdown and item.get("active"):
            item["active"] = False
        else:
            item["active"] = True
            if item["id"] not in self._timer_countdown:
                self._timer_countdown[item["id"]] = item.get("remaining", item["duration"])
        save_data(self._data)
        self._refresh_list()

    def _delete(self, item):
        key = "reminders" if item["type"]=="reminder" else "alarms"
        self._data[key] = [x for x in self._data.get(key,[]) if x.get("id") != item["id"]]
        self._timer_countdown.pop(item["id"], None)
        save_data(self._data)
        self._refresh_list()

    # ── tick ──────────────────────────────────────────────────────────────────

    def _bump_alarm(self, field, delta):
        if field == "h":
            self._alarm_h = (self._alarm_h + delta) % 12
            self._ah_lbl.setText(f"{self._alarm_h:02d}" if self._alarm_h else "12")
        else:
            self._alarm_m = (self._alarm_m + delta) % 60
            self._am_lbl.setText(f"{self._alarm_m:02d}")

    def _set_ampm(self, val):
        self._alarm_ampm = val
        self._update_ampm_display()

    def _update_ampm_display(self):
        am_on = self._alarm_ampm == "AM"
        self._am_btn.setStyleSheet(
            "color:rgba(15,40,80,0.95);font-weight:bold;font-size:18px;font-family:monospace;"
            "background:transparent;border:none;" if am_on else
            "color:rgba(15,40,80,0.3);font-size:18px;font-family:monospace;"
            "background:transparent;border:none;")
        self._pm_btn.setStyleSheet(
            "color:rgba(15,40,80,0.95);font-weight:bold;font-size:18px;font-family:monospace;"
            "background:transparent;border:none;" if not am_on else
            "color:rgba(15,40,80,0.3);font-size:18px;font-family:monospace;"
            "background:transparent;border:none;")

    def _add_alarm_quick(self):
        h = self._alarm_h if self._alarm_h != 0 else 12
        if self._alarm_ampm == "PM" and h != 12: h += 12
        if self._alarm_ampm == "AM" and h == 12: h = 0
        item = {"type":"alarm","hour":h,"minute":self._alarm_m,
                "label":"","days":[0,1,2,3,4,5,6],"active":True,
                "id":int(time.time()*1000)}
        self._data.setdefault("alarms",[]).append(item)
        save_data(self._data)
        self._refresh_list()

    def _bump_timer(self, field, delta):
        if self._timer_running: return
        if field == "h":
            self._timer_h = (self._timer_h + delta) % 24
        elif field == "m":
            self._timer_m = (self._timer_m + delta) % 60
        else:
            self._timer_s = (self._timer_s + delta) % 60
        self._update_timer_display()

    def _update_timer_display(self, red=False):
        style = ("color:rgba(200,20,20,0.9);font-family:monospace;"
                 "font-size:28px;font-weight:bold;" if red else
                 "color:rgba(15,40,80,0.9);font-family:monospace;"
                 "font-size:28px;font-weight:bold;")
        self._th_lbl.setStyleSheet(style)
        self._tm_lbl.setStyleSheet(style)
        self._ts_lbl.setStyleSheet(style)
        if self._timer_running:
            rem = self._timer_remaining
            h,r = divmod(rem,3600); m,s = divmod(r,60)
            self._th_lbl.setText(f"{h:02d}")
            self._tm_lbl.setText(f"{m:02d}")
            self._ts_lbl.setText(f"{s:02d}")
        else:
            self._th_lbl.setText(f"{self._timer_h:02d}")
            self._tm_lbl.setText(f"{self._timer_m:02d}")
            self._ts_lbl.setText(f"{self._timer_s:02d}")

    def _toggle_timer_run(self):
        if self._timer_running:
            # pause
            self._timer_running = False
            self._tstart_btn.setText("start")
            clear_timer()
        else:
            if not self._timer_running and self._timer_remaining == 0:
                # fresh start
                self._timer_remaining = (self._timer_h*3600 +
                                         self._timer_m*60 +
                                         self._timer_s)
                if self._timer_remaining == 0: return
            self._timer_running = True
            self._tstart_btn.setText("pause")
            # mirror to timer.json so external readers (AI / bridge) see state
            try:
                from datetime import datetime as _dt, timedelta as _td
                end = _dt.now() + _td(seconds=self._timer_remaining)
                save_timer({"running": True, "end_at": end.isoformat(timespec="seconds")})
            except Exception: pass

    def _timer_tick(self):
        if not self._timer_running: return
        self._timer_remaining -= 1
        if self._timer_remaining <= 0:
            self._timer_remaining = 0
            self._timer_running = False
            self._tstart_btn.setText("start")
            self._update_timer_display(red=True)
            self._ring_timer()
            clear_timer()
        else:
            self._update_timer_display()

    def _ring_timer(self):
        sound = os.path.join(ASSETS, "sounds", "timer_finished.mp3")
        if os.path.exists(sound):
            self._timer_alarm_proc = subprocess.Popen(
                ["mpv","--no-video","--loop=inf", sound],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # open eww stop button on all monitors
        subprocess.Popen("eww --config ~/.config/eww open btn-klok-stop",
                         shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _stop_timer_ring(self):
        if self._timer_alarm_proc:
            self._timer_alarm_proc.terminate()
            self._timer_alarm_proc = None
        # reset timer display
        self._timer_remaining = 0
        self._update_timer_display(red=False)
        # close eww stop button
        subprocess.Popen("eww --config ~/.config/eww close btn-klok-stop",
                         shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _tick(self):
        now = datetime.now()
        # update main clock display
        self._display.setText(now.strftime("%H:%M"))
        self._sub_lbl.setText(now.strftime("%A  %d"))
        if hasattr(self, "_remind_date"):
            self._remind_date.setText(now.strftime("%-d %B"))
        # timer countdown
        self._timer_tick()

        # check alarms and reminders
        for item in self._data.get("alarms",[]):
            if not item.get("active"): continue
            if item["type"] == "alarm":
                if now.weekday() in item.get("days",[0,1,2,3,4,5,6]):
                    if now.hour==item["hour"] and now.minute==item["minute"] and now.second==0:
                        play_alarm()
            elif item["type"] == "timer":
                if item["id"] in self._timer_countdown and item.get("active"):
                    self._timer_countdown[item["id"]] -= 1
                    item["remaining"] = self._timer_countdown[item["id"]]
                    if self._timer_countdown[item["id"]] <= 0:
                        play_alarm()
                        del self._timer_countdown[item["id"]]
                        item["active"] = False
                        item["remaining"] = item["duration"]
                        save_data(self._data)
                    self._refresh_list()

        for item in list(self._data.get("reminders",[])):
            if now.hour==item["hour"] and now.minute==item["minute"] and now.second==0:
                play_alarm()
                # one-time — delete itself after firing
                self._data["reminders"] = [x for x in self._data["reminders"]
                                            if x.get("id") != item["id"]]
                save_data(self._data)
                if hasattr(self, "_remind_panel") and self._remind_panel.isVisible():
                    self._refresh_reminders()
                break

    # ── drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self,e):
        if e.button()==Qt.MouseButton.LeftButton:
            self._drag_pos=e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self,e):
        if self._drag_pos and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag_pos)
    def mouseReleaseEvent(self,e): self._drag_pos=None

    # ── live-reload watcher ───────────────────────────────────────────────────

    def _start_watcher(self):
        self._sig = _Sig()
        self._sig.klok_changed.connect(self._reload_klok)
        self._sig.timer_changed.connect(self._reload_timer)

        # polling fallback — always on (cheap insurance vs watchdog misses)
        self._poll_klok_m  = self._mtime(DATA_FILE)
        self._poll_timer_m = self._mtime(TIMER_FILE)
        self._poll_t = QTimer(self)
        self._poll_t.timeout.connect(self._poll_check)
        self._poll_t.start(1500)

        # initial sync from timer.json if it already exists
        self._reload_timer()

        if not HAS_WATCHDOG: return
        klok_name  = os.path.basename(DATA_FILE)
        timer_name = os.path.basename(TIMER_FILE)
        sig = self._sig
        class H(FileSystemEventHandler):
            def _emit(self, path):
                try:
                    n = os.path.basename(path)
                    if n == klok_name:  sig.klok_changed.emit()
                    elif n == timer_name: sig.timer_changed.emit()
                except Exception: pass
            def on_modified(self, e): self._emit(e.src_path)
            def on_created(self, e):  self._emit(e.src_path)
            def on_moved(self, e):
                self._emit(getattr(e, "dest_path", "") or "")
                self._emit(e.src_path)
        self._obs = Observer()
        os.makedirs(ASSETS, exist_ok=True)
        self._obs.schedule(H(), ASSETS, recursive=False)
        self._obs.start()

    def _mtime(self, p):
        try: return os.path.getmtime(p)
        except Exception: return 0

    def _poll_check(self):
        m = self._mtime(DATA_FILE)
        if m != self._poll_klok_m:
            self._poll_klok_m = m
            self._reload_klok()
        m = self._mtime(TIMER_FILE)
        if m != self._poll_timer_m:
            self._poll_timer_m = m
            self._reload_timer()

    def _reload_klok(self):
        self._data = load_data()
        try: self._refresh_list()
        except Exception: pass
        try: self._refresh_reminders()
        except Exception: pass

    def _reload_timer(self):
        """Apply timer.json to the running timer state.
        Shape: {"running":true,"end_at":"ISO8601","label":"optional"}
               or {} / missing keys to cancel."""
        t = load_timer()
        if not t or not t.get("running"):
            # external cancel
            if self._timer_running:
                self._timer_running = False
                self._timer_remaining = 0
                self._tstart_btn.setText("start")
                self._update_timer_display(red=False)
            return
        end_at = t.get("end_at")
        if not end_at: return
        try:
            from datetime import datetime as _dt
            end_dt = _dt.fromisoformat(end_at)
            rem = int((end_dt - _dt.now()).total_seconds())
        except Exception:
            return
        if rem <= 0:
            # already expired by the time we read it
            self._timer_running = False
            self._timer_remaining = 0
            self._update_timer_display(red=True)
            self._ring_timer()
            clear_timer()
            return
        self._timer_remaining = rem
        # split back into H/M/S for the display
        self._timer_h = rem // 3600
        self._timer_m = (rem % 3600) // 60
        self._timer_s = rem % 60
        self._timer_running = True
        self._tstart_btn.setText("pause")
        self._update_timer_display()

    def closeEvent(self, e):
        if HAS_WATCHDOG and hasattr(self, "_obs"):
            try: self._obs.stop(); self._obs.join()
            except Exception: pass
        super().closeEvent(e)

    def keyPressEvent(self,e):
        if e.key()==Qt.Key.Key_Escape:
            if self._timer_alarm_proc:
                self._stop_timer_ring()
            else:
                self.hide()
        super().keyPressEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = KlokWindow()
    win.show()
    sys.exit(app.exec())
