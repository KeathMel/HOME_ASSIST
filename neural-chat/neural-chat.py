#!/usr/bin/env python3
"""
Neural Chat — visual novel style
"""

import sys, os, json, time, base64, mimetypes, tempfile, subprocess, re
import urllib.request
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QScrollArea, QFrame, QFileDialog,
    QDialog, QLineEdit, QFormLayout, QDialogButtonBox, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize
from PyQt6.QtGui import (
    QPixmap, QColor, QPainter, QPainterPath, QKeyEvent,
    QLinearGradient, QBrush, QPen,
)

# ── constants ─────────────────────────────────────────────────────────────────

ASSETS      = os.path.expanduser("~/.config/MASTER_VAULT/HOME_ASSIST/neural-chat")
CONFIG_FILE = os.path.join(ASSETS, "config.json")
HISTORY_FILE= os.path.join(ASSETS, "history.json")

def bg_for_hour(h):
    if 4  <= h < 10: return os.path.join(ASSETS, "morning.png")
    if 10 <= h < 15: return os.path.join(ASSETS, "midday.png")
    if 15 <= h < 19: return os.path.join(ASSETS, "noon.png")
    return os.path.join(ASSETS, "evening.png")

DEFAULT_CFG = {
    "webhook_url":     "",
    "ai_name":         "Schwortz",
    "character_image": os.path.join(ASSETS, "him.png"),
    "window_w": 1000,
    "window_h": 680,
}

def load_config():
    if not os.path.exists(CONFIG_FILE): return dict(DEFAULT_CFG)
    with open(CONFIG_FILE) as f: return {**DEFAULT_CFG, **json.load(f)}

def save_config(cfg):
    os.makedirs(ASSETS, exist_ok=True)
    with open(CONFIG_FILE,"w") as f: json.dump(cfg, f, indent=2)

# ── tunnel base URL ───────────────────────────────────────────────────────────
# The webhook field stores only the path (e.g. "/hook/bruh"). The base comes
# from tunnel_url.py in HOME_ASSIST, the one place that knows the current
# tunnel, so a rotated URL is picked up everywhere without touching configs.

sys.path.insert(0, os.path.dirname(ASSETS))
try:
    from tunnel_url import webhook_url as _tunnel_webhook_url
except Exception:
    # helper missing -> stay on the local runner rather than refusing to
    # start, since on this machine localhost is the right answer anyway
    def _tunnel_webhook_url(path, fallback="http://127.0.0.1:5800"):
        if not path.startswith("/"): path = "/" + path
        return fallback + path

def build_full_url(field):
    """Combine the saved webhook field with the current tunnel base.

    A field that's already a full URL is used as-is, so you can point this
    at something else without fighting the tunnel. Anything else is treated
    as a path and resolved fresh EVERY call — quick tunnels hand out a new
    random URL on each restart, so a base cached at startup would keep
    posting to a dead address after a toggle.
    """
    f = (field or "").strip()
    if not f: return ""
    if f.startswith("http://") or f.startswith("https://"): return f
    return _tunnel_webhook_url(f)

# ── history ───────────────────────────────────────────────────────────────────

def load_history():
    if not os.path.exists(HISTORY_FILE): return []
    with open(HISTORY_FILE) as f:
        try: return json.load(f)
        except: return []

def save_history(msgs):
    os.makedirs(ASSETS, exist_ok=True)
    with open(HISTORY_FILE,"w") as f: json.dump(msgs[-200:], f, indent=2)

# ── mic source auto-detect ────────────────────────────────────────────────────

def detect_mic_source():
    """Return the first real mic input source — skips monitors and sink outputs."""
    try:
        out = subprocess.check_output(
            ["pactl","list","sources","short"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            low = line.lower()
            # skip anything that is a monitor (loopback of an output)
            # and anything that looks like a sink/output
            if "monitor" in low: continue
            if "output"  in low: continue
            if "sink"    in low: continue
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    except: pass
    return "default"

# ── worker threads ────────────────────────────────────────────────────────────

class SendThread(QThread):
    response_ready = pyqtSignal(str, str)
    def __init__(self, url, payload, ptype, parent=None):
        super().__init__(parent)
        self.url, self.payload, self.ptype = url, payload, ptype
    def run(self):
        try:
            if isinstance(self.payload, bytes):
                data, ct = self.payload, "audio/webm"
            elif self.ptype in ("file","image"):
                with open(self.payload["path"],"rb") as f: raw=f.read()
                data = json.dumps({"type":self.ptype,"filename":self.payload["name"],
                                   "data":base64.b64encode(raw).decode()}).encode()
                ct = "application/json"
            else:
                data = json.dumps({"message":self.payload,"type":self.ptype}).encode()
                ct   = "application/json"
            req = urllib.request.Request(self.url, data=data,
                                         headers={"Content-Type":ct}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r: body=r.read().decode()
            s = body.strip()
            if s.startswith("http") and any(s.endswith(x) for x in [".mp3",".ogg",".wav",".webm",".aac"]):
                self.response_ready.emit("audio", s)
            elif s.startswith("{"):
                p = json.loads(s)
                if "audio_url" in p: self.response_ready.emit("audio", p["audio_url"])
                elif "text"    in p: self.response_ready.emit("text",  p["text"])
                elif "message" in p: self.response_ready.emit("text",  p["message"])
                else:                self.response_ready.emit("text",  s)
            else:
                self.response_ready.emit("text", s)
        except Exception as e:
            self.response_ready.emit("text", f"[error: {e}]")

class AudioDLThread(QThread):
    done = pyqtSignal(str)
    def __init__(self, url, parent=None):
        super().__init__(parent); self.url=url
    def run(self):
        try:
            suffix = Path(self.url.split("?")[0]).suffix or ".mp3"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            urllib.request.urlretrieve(self.url, tmp.name)
            self.done.emit(tmp.name)
        except: self.done.emit("")

class VolMonitorThread(QThread):
    """Reads mic volume via pactl and emits 0-100 level."""
    level = pyqtSignal(int)
    def __init__(self, source, parent=None):
        super().__init__(parent); self.source=source; self._running=True
    def run(self):
        while self._running:
            try:
                out = subprocess.check_output(
                    ["pactl","get-source-volume", self.source],
                    text=True, stderr=subprocess.DEVNULL)
                nums = re.findall(r'(\d+)%', out)
                if nums: self.level.emit(int(nums[0]))
            except: pass
            self.msleep(80)
    def stop(self): self._running=False

# ── waveform widget ───────────────────────────────────────────────────────────

class WaveformWidget(QWidget):
    """Smooth animated sine-based waveform that reacts to mic volume."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0      # 0-100
        self._phase = 0.0
        self._anim_timer = QTimer()
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(30)  # ~33fps
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_level(self, level: int):
        self._level = level

    def _tick(self):
        self._phase += 0.15
        self.update()

    def paintEvent(self, _):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cy = h / 2
        amp = (self._level / 100.0) * (h * 0.40)

        # bottom border line
        pen = QPen(QColor(255,255,255,30), 1)
        p.setPen(pen)
        p.drawLine(0, h-1, w, h-1)

        if amp < 1:
            # flat line when silent
            pen = QPen(QColor(255,255,255,60), 1.5)
            p.setPen(pen)
            p.drawLine(0, int(cy), w, int(cy))
            p.end()
            return

        # draw smooth waveform
        path = QPainterPath()
        steps = w
        freq  = 3.0  # number of wave cycles across width

        for x in range(steps + 1):
            t   = (x / steps) * 2 * math.pi * freq + self._phase
            y   = cy - amp * math.sin(t)
            if x == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        # glow: thick translucent line underneath
        glow_pen = QPen(QColor(255,255,255,30), 6)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(glow_pen)
        p.drawPath(path)

        # main white line
        main_pen = QPen(QColor(255,255,255,200), 1.5)
        main_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(main_pen)
        p.drawPath(path)
        p.end()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._phase = 0.0; self._level = 0

# ── bubble widget ─────────────────────────────────────────────────────────────

class Bubble(QFrame):
    def __init__(self, role, text, msg_type="text", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        is_user = role == "user"
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0,2,0,2); outer.setSpacing(0)

        box = QFrame()
        box.setObjectName("ububble" if is_user else "abubble")
        il = QVBoxLayout(box); il.setContentsMargins(12,8,12,8); il.setSpacing(3)

        if msg_type == "image" and os.path.exists(text):
            lbl = QLabel()
            lbl.setPixmap(QPixmap(text).scaledToWidth(200, Qt.TransformationMode.SmoothTransformation))
            il.addWidget(lbl)
        elif msg_type == "audio":
            # green sharp-corner play button
            btn = QPushButton("▶  play audio")
            btn.setStyleSheet(
                "background:rgba(20,50,30,0.8);"
                "border:1px solid rgba(60,180,80,0.6);"
                "border-radius:2px;"
                "color:#a0ffb0;"
                "padding:5px 12px;"
                "font-size:14px;"
                "font-family:monospace;"
            )
            btn.clicked.connect(lambda: subprocess.Popen(
                ["mpv","--no-video",text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            il.addWidget(btn)
        else:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setStyleSheet("font-size:16px; color:#ffffff;")
            il.addWidget(lbl)

        ts = QLabel(time.strftime("%H:%M"))
        ts.setStyleSheet("color:rgba(255,255,255,0.30); font-size:10px; font-family:monospace;")
        ts.setAlignment(Qt.AlignmentFlag.AlignRight if is_user else Qt.AlignmentFlag.AlignLeft)
        il.addWidget(ts)

        box.setMaximumWidth(380)
        if is_user:
            outer.addStretch(); outer.addWidget(box); outer.addSpacing(6)
        else:
            outer.addSpacing(6); outer.addWidget(box); outer.addStretch()

class TypingBubble(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFrameShape(QFrame.Shape.NoFrame)
        o=QHBoxLayout(self); o.setContentsMargins(6,2,0,2)
        f=QFrame(); f.setObjectName("abubble")
        il=QVBoxLayout(f); il.setContentsMargins(12,8,12,8)
        self._l=QLabel("▪ ▪ ▪")
        self._l.setStyleSheet("color:rgba(255,255,255,0.3); font-size:14px; letter-spacing:4px;")
        il.addWidget(self._l); o.addWidget(f); o.addStretch()
        self._t=QTimer(); self._t.timeout.connect(self._tick); self._t.start(420); self._i=0
    def _tick(self):
        s=["▪ ▪ ▪","□ ▪ ▪","□ □ ▪","□ □ □"]; self._l.setText(s[self._i%4]); self._i+=1
    def stop(self): self._t.stop()

# ── settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Neural Chat — Settings"); self.setMinimumWidth(560)
        self.setStyleSheet("""
            QDialog { background: #1c1c22; color: #e8e8ec; }
            QLabel  { color: #c0c0c8; font-size: 16px; }
            QLabel#title    { color: #ffffff; font-size: 22px; font-weight: 600; }
            QLabel#subtle   { color: rgba(255,255,255,0.55); font-size: 13px; }
            QLabel#preview  { color: #b5e4c0; font-size: 13px; }
            QLineEdit {
                background: #26262e; color: #ffffff;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 6px;
                padding: 10px 12px;
                font-size: 16px;
            }
            QLineEdit:focus { border: 1px solid rgba(180,210,255,0.55); }
            QPushButton {
                background: #2c2c34; color: #ffffff;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 6px;
                padding: 9px 18px;
                font-size: 15px;
            }
            QPushButton:hover { background: #353541; }
            QPushButton#ok {
                background: #2e6a3e; color: #ffffff;
                border: 1px solid #3f8a52; font-weight: 600;
            }
            QPushButton#ok:hover { background: #38803c; }
        """)
        self.cfg = dict(cfg)
        l=QVBoxLayout(self); l.setContentsMargins(28,28,28,24); l.setSpacing(18)

        t=QLabel("Settings"); t.setObjectName("title")
        l.addWidget(t)

        # shows what the path will actually be prefixed with right now, so
        # a tunnel that's down is visible here instead of only at send time
        try:
            from tunnel_url import tunnel_base, is_tunnelled
            base = tunnel_base()
            base_lbl = QLabel(("Tunnel: " + base) if is_tunnelled()
                              else "Tunnel down — using " + base)
        except Exception:
            base_lbl = QLabel("Tunnel helper not found — using http://127.0.0.1:5800")
        base_lbl.setObjectName("subtle"); base_lbl.setWordWrap(True)
        l.addWidget(base_lbl)

        form=QFormLayout(); form.setSpacing(14); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.wh=QLineEdit(cfg.get("webhook_url",""))
        self.wh.setPlaceholderText("/hook/bruh")
        self.nm=QLineEdit(cfg.get("ai_name","Schwortz"))
        self.ch=QLineEdit(cfg.get("character_image",""))
        br=QPushButton("Browse"); br.clicked.connect(self._pick)
        row=QHBoxLayout(); row.addWidget(self.ch); row.addWidget(br)
        form.addRow("Webhook path", self.wh)
        form.addRow("AI name",      self.nm)
        form.addRow("Character",    row)
        l.addLayout(form)

        self._preview = QLabel(""); self._preview.setObjectName("preview")
        self._preview.setWordWrap(True)
        l.addWidget(self._preview)
        self.wh.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

        btns=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setObjectName("ok")
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        l.addWidget(btns)

    def _refresh_preview(self):
        u = build_full_url(self.wh.text())
        self._preview.setText(("→ " + u) if u else "→ (no URL — set a path above)")

    def _pick(self):
        p,_=QFileDialog.getOpenFileName(self,"Character","","Images (*.png *.jpg *.gif)")
        if p: self.ch.setText(p)

    def _ok(self):
        self.cfg["webhook_url"]=self.wh.text().strip()
        self.cfg["ai_name"]=self.nm.text().strip() or "Schwortz"
        self.cfg["character_image"]=self.ch.text().strip()
        self.accept()

# ── stylesheet ────────────────────────────────────────────────────────────────

STYLE = """
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 4px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.15); border-radius:2px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QFrame#abubble {
    background: rgba(10,6,22,0.72);
    border-radius: 2px;
    border-top-left-radius: 0px;
    border: 1px solid rgba(255,255,255,0.08);
}
QFrame#ububble {
    background: rgba(40,20,70,0.65);
    border-radius: 2px;
    border-bottom-right-radius: 0px;
    border: 1px solid rgba(255,255,255,0.10);
}

QTextEdit#inp {
    background: transparent;
    color: #ffffff;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.20);
    font-size: 16px;
    font-family: monospace;
    padding: 4px 6px;
}
QTextEdit#inp:focus {
    border-bottom: 1px solid rgba(255,255,255,0.60);
}

/* volume bar shown while recording */
QFrame#volbar {
    background: rgba(60,180,80,0.55);
    border-radius: 1px;
}

QPushButton#mic, QPushButton#att, QPushButton#snd {
    background: transparent;
    border: none;
    color: #ffffff;
    font-size: 20px;
    font-family: monospace;
    font-weight: bold;
}
QPushButton#mic:hover, QPushButton#att:hover, QPushButton#snd:hover {
    color: rgba(200,255,200,1);
}

QPushButton#gear {
    background: transparent;
    border: none;
    color: rgba(255,255,255,0.55);
    font-size: 22px;
}
QPushButton#gear:hover { color: #ffffff; }

QLabel#aname {
    color: rgba(255,255,255,0.55);
    font-size: 10px;
    font-family: monospace;
    letter-spacing: 3px;
}
QLabel#status {
    color: rgba(255,255,255,0.20);
    font-size: 10px;
    font-family: monospace;
}
"""

# ── main window ───────────────────────────────────────────────────────────────

class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.history = load_history()
        self._mic_source = detect_mic_source()

        self.setWindowTitle("Neural Chat")
        self.resize(self.cfg["window_w"], self.cfg["window_h"])
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)

        self._drag_pos     = None
        self._bg_px        = None
        self._char_px      = None
        self._send_thread  = None
        self._audio_thread = None
        self._typing_w     = None
        self._is_rec       = False
        self._hold_t       = None
        self._rec_proc     = None
        self._rec_tmp      = None
        self._vol_thread   = None
        self._vol_level    = 0

        self._load_assets()
        self._build()
        self._load_history_bubbles()

        # auto-focus input
        QTimer.singleShot(100, self._inp.setFocus)

        self._bg_timer = QTimer()
        self._bg_timer.timeout.connect(self._reload_bg)
        self._bg_timer.start(60_000)

    # ── assets ─────────────────────────────────────────────────────────────

    def _load_assets(self):
        h = int(time.strftime("%H"))
        p = bg_for_hour(h)
        self._bg_px = QPixmap(p) if os.path.exists(p) else None
        cp = self.cfg.get("character_image","")
        self._char_px = QPixmap(cp) if (cp and os.path.exists(cp)) else None

    def _reload_bg(self):
        h = int(time.strftime("%H"))
        p = bg_for_hour(h)
        if os.path.exists(p): self._bg_px = QPixmap(p); self.update()

    # ── background paint ───────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = self.rect()
        p.fillRect(r, QColor(8,4,20))
        if self._bg_px and not self._bg_px.isNull():
            s = self._bg_px.scaled(r.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            dx = (r.width()  - s.width() )//2
            dy = (r.height() - s.height())//2
            p.drawPixmap(dx, dy, s)
        p.fillRect(r, QColor(0,0,0,55))
        p.end()

    # ── drag ───────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag_pos)
    def mouseReleaseEvent(self, e): self._drag_pos=None

    # ── build ──────────────────────────────────────────────────────────────

    def _build(self):
        root = QWidget(); self.setCentralWidget(root)
        root.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # base layer: chat panel offset right
        base = QWidget(); base.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        bl = QHBoxLayout(base); bl.setContentsMargins(0,0,0,0); bl.setSpacing(0)
        char_col = int(self.cfg["window_w"] * 0.28)
        bl.addSpacing(char_col - 40)
        right = QWidget(); right.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        bl.addWidget(right, 1)

        # character overlay — top layer
        self._char_lbl = QLabel(root)
        self._char_lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._char_lbl.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft)
        self._char_lbl.setGeometry(0, 0, char_col+20, self.height())
        self._char_lbl.raise_()
        self._render_char()

        rl = QHBoxLayout(root); rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)
        rl.addWidget(base)

        # right panel layout
        rlay = QVBoxLayout(right); rlay.setContentsMargins(0,0,0,0); rlay.setSpacing(0)

        # top bar
        top = QWidget(); top.setFixedHeight(48)
        top.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        tl = QHBoxLayout(top); tl.setContentsMargins(14,0,12,0)
        self._name_lbl = QLabel(self.cfg["ai_name"].upper())
        self._name_lbl.setObjectName("aname")
        tl.addWidget(self._name_lbl)
        tl.addStretch()
        self._status_lbl = QLabel("ready")
        self._status_lbl.setObjectName("status")
        tl.addWidget(self._status_lbl)
        tl.addSpacing(6)
        gear = QPushButton("⚙"); gear.setObjectName("gear")
        gear.setFixedSize(34,34); gear.clicked.connect(self._open_settings)
        tl.addWidget(gear)
        rlay.addWidget(top)

        # messages scroll — bottom anchored
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background:transparent;")
        self._msg_w = QWidget(); self._msg_w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._msg_l = QVBoxLayout(self._msg_w)
        self._msg_l.setContentsMargins(8,10,8,10); self._msg_l.setSpacing(2)
        self._msg_l.addStretch()   # pushes messages to bottom
        self._scroll.setWidget(self._msg_w)
        rlay.addWidget(self._scroll, 1)

        # volume bar (shown while recording)
        self._vol_container = QWidget()
        self._vol_container.setFixedHeight(3)
        self._vol_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._vol_bar = QFrame(self._vol_container)
        self._vol_bar.setObjectName("volbar")
        self._vol_bar.setGeometry(0,0,0,3)
        rlay.addWidget(self._vol_container)

        # footer
        foot = QWidget(); foot.setFixedHeight(56)
        foot.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        fl = QHBoxLayout(foot); fl.setContentsMargins(12,8,12,8); fl.setSpacing(10)

        self._att_btn = QPushButton("⊕"); self._att_btn.setObjectName("att")
        self._att_btn.setFixedSize(34,34); self._att_btn.clicked.connect(self._attach)
        fl.addWidget(self._att_btn)

        self._mic_btn = QPushButton("\uf130"); self._mic_btn.setObjectName("mic")
        self._mic_btn.setFixedSize(34,34)
        self._mic_btn.pressed.connect(self._mic_press)
        self._mic_btn.released.connect(self._mic_release)
        fl.addWidget(self._mic_btn)

        self._inp = QTextEdit(); self._inp.setObjectName("inp")
        self._inp.setPlaceholderText("transmit…")
        self._inp.setFixedHeight(40)
        self._inp.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._inp.installEventFilter(self)

        # waveform widget shown in place of input while recording
        self._waveform = WaveformWidget()
        self._waveform.setFixedHeight(40)
        self._waveform.hide()

        fl.addWidget(self._inp)
        fl.addWidget(self._waveform)

        snd = QPushButton("→"); snd.setObjectName("snd")
        snd.setFixedSize(34,34); snd.clicked.connect(self._send_text)
        fl.addWidget(snd)

        rlay.addWidget(foot)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        char_col = int(self.width()*0.28)
        if hasattr(self,"_char_lbl"):
            self._char_lbl.setGeometry(0,0,char_col+20,self.height())
            self._char_lbl.raise_()
        self._render_char()

    def _render_char(self):
        if self._char_px and not self._char_px.isNull():
            h = int(self.height()*0.88)
            self._char_lbl.setPixmap(
                self._char_px.scaledToHeight(h, Qt.TransformationMode.SmoothTransformation))

    # ── key intercept ──────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._inp and isinstance(event, QKeyEvent):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self._send_text(); return True
        return super().eventFilter(obj, event)

    # ── history ────────────────────────────────────────────────────────────

    def _load_history_bubbles(self):
        for msg in self.history[-40:]:
            b = Bubble(msg["role"], msg["text"], msg.get("type","text"))
            n = self._msg_l.count()
            self._msg_l.insertWidget(n-1, b)
        QTimer.singleShot(60, self._scroll_bottom)

    # ── messages ───────────────────────────────────────────────────────────

    def _add_bubble(self, role, text, msg_type="text"):
        b = Bubble(role, text, msg_type)
        n = self._msg_l.count()
        self._msg_l.insertWidget(n-1, b)
        self.history.append({"role":role,"text":text,"type":msg_type,
                              "ts": int(time.time())})
        save_history(self.history)
        QTimer.singleShot(40, self._scroll_bottom)

    def _scroll_bottom(self):
        sb = self._scroll.verticalScrollBar(); sb.setValue(sb.maximum())

    def _show_typing(self):
        self._typing_w = TypingBubble()
        n = self._msg_l.count()
        self._msg_l.insertWidget(n-1, self._typing_w)
        QTimer.singleShot(40, self._scroll_bottom)

    def _hide_typing(self):
        if self._typing_w:
            self._typing_w.stop()
            self._msg_l.removeWidget(self._typing_w)
            self._typing_w.deleteLater()
            self._typing_w=None

    # ── send ───────────────────────────────────────────────────────────────

    def _send_text(self):
        txt = self._inp.toPlainText().strip()
        if not txt: return
        self._inp.clear()
        self._add_bubble("user", txt)
        self._dispatch(txt,"text")

    def _attach(self):
        p,_ = QFileDialog.getOpenFileName(self,"Attach","","All files (*)")
        if not p: return
        name = os.path.basename(p)
        mime,_ = mimetypes.guess_type(p)
        if mime and mime.startswith("image"):
            self._add_bubble("user", p, "image"); self._dispatch({"path":p,"name":name},"image")
        else:
            self._add_bubble("user", f"⊕ {name}"); self._dispatch({"path":p,"name":name},"file")

    def _dispatch(self, payload, ptype):
        url = build_full_url(self.cfg.get("webhook_url",""))
        if not url:
            self._add_bubble("ai","[ no webhook — open ⚙ settings ]"); return
        self._show_typing(); self._status_lbl.setText("sending…")
        self._send_thread = SendThread(url, payload, ptype)
        self._send_thread.response_ready.connect(self._on_response)
        self._send_thread.start()

    def _on_response(self, rtype, content):
        self._hide_typing(); self._status_lbl.setText("ready")
        if rtype == "audio":
            self._add_bubble("ai","[ fetching audio… ]")
            self._audio_thread = AudioDLThread(content)
            self._audio_thread.done.connect(self._on_audio)
            self._audio_thread.start()
        else:
            self._add_bubble("ai", content)

    def _on_audio(self, path):
        if not path: self._add_bubble("ai","[ audio failed ]"); return
        n = self._msg_l.count()
        item = self._msg_l.itemAt(n-2)
        if item and item.widget(): item.widget().deleteLater(); self._msg_l.removeItem(item)
        self._add_bubble("ai", path, "audio")
        subprocess.Popen(["mpv","--no-video",path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ── mic ────────────────────────────────────────────────────────────────

    def _mic_press(self):
        self._hold_t = QTimer(); self._hold_t.setSingleShot(True)
        self._hold_t.setInterval(400); self._hold_t.timeout.connect(self._start_rec)
        self._hold_t.start()

    def _mic_release(self):
        if self._hold_t: self._hold_t.stop()
        if self._is_rec: self._stop_rec()

    def _start_rec(self):
        self._is_rec = True
        self._mic_btn.setStyleSheet(
            "background:transparent;border:none;color:#ffffff;font-size:18px;font-weight:bold;")
        self._status_lbl.setText("● rec")
        self._inp.hide()
        self._waveform.show()

        self._rec_tmp = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
        self._rec_proc = subprocess.Popen(
            ["ffmpeg","-y","-f","pulse","-i", self._mic_source, self._rec_tmp.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self._vol_thread = VolMonitorThread(self._mic_source)
        self._vol_thread.level.connect(self._update_vol)
        self._vol_thread.start()

    def _stop_rec(self):
        self._is_rec = False
        self._mic_btn.setStyleSheet("")
        self._status_lbl.setText("sending…")
        self._waveform.hide()
        self._waveform.set_level(0)
        self._inp.show()
        self._inp.setFocus()

        if self._vol_thread:
            self._vol_thread.stop(); self._vol_thread.wait(); self._vol_thread=None

        if self._rec_proc: self._rec_proc.terminate(); self._rec_proc.wait()
        if self._rec_tmp and os.path.exists(self._rec_tmp.name):
            with open(self._rec_tmp.name,"rb") as f: audio=f.read()
            self._add_bubble("user", self._rec_tmp.name, "audio")
            self._dispatch(audio,"audio")

    def _update_vol(self, level):
        self._waveform.set_level(level)

    # ── settings ───────────────────────────────────────────────────────────

    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg = dlg.cfg; save_config(self.cfg)
            self._name_lbl.setText(self.cfg["ai_name"].upper())
            self._load_assets(); self._render_char(); self.update()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ChatWindow()
    win.show()
    sys.exit(app.exec())
