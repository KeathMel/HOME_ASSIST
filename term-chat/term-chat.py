#!/usr/bin/env python3
"""
Term Chat — terminal-agent chat.

Same visual-novel style as Neural Chat, but with a request/confirm mechanic:

  * A background watcher polls REQUEST_FILE (~/.config/term-chat/request.json).
  * When n8n drops a request into that file, the chat renders the request text
    with DENY / CONFIRM buttons below it.
  * On a choice the watched file is wiped empty ({}), the decision is POSTed to
    the webhook, and — if approved — the command is written to APPROVED_FILE
    and run-command.sh is launched to execute it in a terminal.

Request file shape (keys are flexible; these are what the app looks for):
  {
    "id":      "optional-id",
    "request": "human readable text shown in the bubble",
    "command": "the shell command to run on approve",
    "reply":   "optional extra AI chat text"
  }
"""

import sys, os, json, time, base64, mimetypes, tempfile, subprocess, re, math
import urllib.request
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QScrollArea, QFrame, QFileDialog,
    QDialog, QLineEdit, QFormLayout, QDialogButtonBox, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QColor, QPainter, QPen, QKeyEvent

# ── constants ─────────────────────────────────────────────────────────────────

ASSETS        = os.path.expanduser("~/.config/MASTER_VAULT/HOME_ASSIST/term-chat")
CONFIG_FILE   = os.path.join(ASSETS, "config.json")
HISTORY_FILE  = os.path.join(ASSETS, "history.json")
REQUEST_FILE  = os.path.join(ASSETS, "request.json")
APPROVED_FILE = os.path.join(ASSETS, "approved.json")
RUNNER        = os.path.join(ASSETS, "run-command.sh")

def bg_for_hour(h):
    if 5  <= h < 11: return os.path.join(ASSETS, "morning.png")
    if 11 <= h < 16: return os.path.join(ASSETS, "day.png")
    if 16 <= h < 20: return os.path.join(ASSETS, "noon.png")
    return os.path.join(ASSETS, "night.png")

DEFAULT_CFG = {
    "webhook_url":     "",
    "ai_name":         "Agent",
    "character_image": os.path.join(ASSETS, "him.png"),
    "window_w": 925,
    "window_h": 638,
}

def load_config():
    if not os.path.exists(CONFIG_FILE): return dict(DEFAULT_CFG)
    try:
        with open(CONFIG_FILE) as f: return {**DEFAULT_CFG, **json.load(f)}
    except: return dict(DEFAULT_CFG)

def save_config(cfg):
    os.makedirs(ASSETS, exist_ok=True)
    with open(CONFIG_FILE,"w") as f: json.dump(cfg, f, indent=2)

# ── tunnel base URL ───────────────────────────────────────────────────────────
# webhook_url stores only the path (/hook/bruh). The base comes from
# tunnel_url.py in HOME_ASSIST, the one place that knows the current tunnel,
# so a rotated URL is picked up here without touching this app's config.

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
    """A full URL is used as-is; anything else is a path resolved against
    the CURRENT tunnel. Resolved per call on purpose — a quick tunnel gets
    a new random URL every restart, so a base cached at startup would keep
    posting to a dead address after a toggle."""
    f = (field or "").strip()
    if not f: return ""
    if f.startswith("http://") or f.startswith("https://"): return f
    return _tunnel_webhook_url(f)

# ── history ───────────────────────────────────────────────────────────────────

def load_history():
    if not os.path.exists(HISTORY_FILE): return []
    try:
        with open(HISTORY_FILE) as f: return json.load(f)
    except: return []

def save_history(msgs):
    os.makedirs(ASSETS, exist_ok=True)
    with open(HISTORY_FILE,"w") as f: json.dump(msgs[-200:], f, indent=2)

# ── send worker ───────────────────────────────────────────────────────────────

class SendThread(QThread):
    response_ready = pyqtSignal(str)
    def __init__(self, url, payload, ptype="json", parent=None):
        super().__init__(parent)
        self.url, self.payload, self.ptype = url, payload, ptype
    def run(self):
        try:
            if self.ptype == "audio" and isinstance(self.payload, bytes):
                data, ct = self.payload, "audio/webm"
            elif self.ptype in ("file","image"):
                with open(self.payload["path"],"rb") as f: raw=f.read()
                data = json.dumps({"type":self.ptype,"filename":self.payload["name"],
                                   "data":base64.b64encode(raw).decode()}).encode()
                ct = "application/json"
            else:
                data = json.dumps(self.payload).encode()
                ct = "application/json"
            req = urllib.request.Request(
                self.url, data=data, headers={"Content-Type":ct}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode().strip()
            if body.startswith("http") and any(body.endswith(x) for x in [".mp3",".ogg",".wav",".webm",".aac"]):
                self.response_ready.emit("AUDIO::"+body); return
            if body.startswith("{"):
                p = json.loads(body)
                if "audio_url" in p: self.response_ready.emit("AUDIO::"+p["audio_url"]); return
                for k in ("text","message","reply"):
                    if k in p: self.response_ready.emit(str(p[k])); return
                self.response_ready.emit(body)
            elif body:
                self.response_ready.emit(body)
            else:
                self.response_ready.emit("")
        except Exception as e:
            self.response_ready.emit(f"[error: {e}]")

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

def detect_mic_source():
    try:
        out = subprocess.check_output(
            ["pactl","list","sources","short"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            low = line.lower()
            if "monitor" in low or "output" in low or "sink" in low: continue
            parts = line.split()
            if len(parts) >= 2: return parts[1]
    except: pass
    return "default"

class VolMonitorThread(QThread):
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

class WaveformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0; self._phase = 0.0
        self._t = QTimer(); self._t.timeout.connect(self._tick); self._t.start(30)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    def set_level(self, lv): self._level = lv
    def _tick(self): self._phase += 0.15; self.update()
    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height(); cy = h/2
        amp = (self._level/100.0)*(h*0.40)
        p.setPen(QPen(QColor(255,255,255,30),1)); p.drawLine(0,h-1,w,h-1)
        if amp < 1:
            p.setPen(QPen(QColor(255,255,255,60),1.5)); p.drawLine(0,int(cy),w,int(cy)); p.end(); return
        p.setPen(QPen(QColor(160,255,176,200),1.8))
        pts=[]
        for x in range(0,w,3):
            y = cy + amp*math.sin(x*0.05 + self._phase)*math.sin(x*0.013+self._phase*0.6)
            pts.append((x,y))
        for i in range(len(pts)-1):
            p.drawLine(int(pts[i][0]),int(pts[i][1]),int(pts[i+1][0]),int(pts[i+1][1]))
        p.end()

# ── bubble ────────────────────────────────────────────────────────────────────

class Bubble(QFrame):
    def __init__(self, role, text, msg_type="text", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        is_user = role == "user"
        outer = QHBoxLayout(self); outer.setContentsMargins(6,2,6,2)
        box = QFrame(); box.setObjectName("ububble" if is_user else "abubble")
        il = QVBoxLayout(box); il.setContentsMargins(14,10,14,10); il.setSpacing(3)

        if msg_type == "image" and os.path.exists(text):
            lbl = QLabel()
            lbl.setPixmap(QPixmap(text).scaledToWidth(220, Qt.TransformationMode.SmoothTransformation))
            il.addWidget(lbl)
        elif msg_type == "audio":
            btn = QPushButton("▶  play audio")
            btn.setStyleSheet(
                "background:rgba(20,50,30,0.8);border:1px solid rgba(60,180,80,0.6);"
                "border-radius:2px;color:#a0ffb0;padding:7px 14px;font-size:14px;font-family:monospace;")
            btn.clicked.connect(lambda: subprocess.Popen(
                ["mpv","--no-video",text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            il.addWidget(btn)
        else:
            lbl = QLabel(text); lbl.setWordWrap(True)
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

class RequestBubble(QFrame):
    """AI request with deny / confirm buttons. Locks after a choice."""
    def __init__(self, request_text, command, on_decision, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._on_decision = on_decision
        self._command = command
        outer = QHBoxLayout(self); outer.setContentsMargins(6,2,6,2)
        box = QFrame(); box.setObjectName("reqbubble")
        il = QVBoxLayout(box); il.setContentsMargins(12,10,12,10); il.setSpacing(8)

        tag = QLabel("// REQUEST")
        tag.setStyleSheet("color:#ffd27a;font-size:10px;font-family:monospace;letter-spacing:2px;")
        il.addWidget(tag)

        req = QLabel(request_text); req.setWordWrap(True)
        req.setStyleSheet("font-size:13px;color:#ffffff;")
        il.addWidget(req)

        if command:
            cmd = QLabel(command); cmd.setWordWrap(True)
            cmd.setStyleSheet(
                "font-size:12px;color:#a0ffb0;font-family:monospace;"
                "background:rgba(0,0,0,0.35);padding:6px 8px;border-radius:2px;")
            il.addWidget(cmd)

        self._btn_row = QHBoxLayout(); self._btn_row.setSpacing(8)
        self._deny = QPushButton("✕  deny"); self._deny.setObjectName("deny")
        self._conf = QPushButton("✓  confirm"); self._conf.setObjectName("conf")
        self._deny.clicked.connect(lambda: self._decide("denied"))
        self._conf.clicked.connect(lambda: self._decide("approved"))
        self._btn_row.addWidget(self._deny); self._btn_row.addWidget(self._conf)
        il.addLayout(self._btn_row)

        self._result = QLabel(""); self._result.setStyleSheet(
            "font-size:11px;font-family:monospace;color:rgba(255,255,255,0.6);")
        self._result.hide()
        il.addWidget(self._result)

        box.setMaximumWidth(380)
        outer.addSpacing(6); outer.addWidget(box); outer.addStretch()

    def _decide(self, decision):
        self._deny.setEnabled(False); self._conf.setEnabled(False)
        self._deny.hide(); self._conf.hide()
        if decision == "approved":
            self._result.setText("✓ approved · step done")
            self._result.setStyleSheet("font-size:11px;font-family:monospace;color:#a0ffb0;")
        else:
            self._result.setText("✕ denied")
            self._result.setStyleSheet("font-size:11px;font-family:monospace;color:#ff9a9a;")
        self._result.show()
        self._on_decision(decision, self._command)

# ── settings ──────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings"); self.setMinimumWidth(440)
        self.setStyleSheet("""
            QDialog{background:#080410;color:#e0e0e0;}
            QLabel{color:#909090;font-size:12px;font-family:monospace;}
            QLineEdit{background:#0f0820;color:#fff;border:none;
                border-bottom:1px solid rgba(255,255,255,0.2);
                padding:8px 4px;font-size:13px;font-family:monospace;}
            QLineEdit:focus{border-bottom:1px solid rgba(255,255,255,0.7);}
            QPushButton{background:transparent;color:#fff;
                border:1px solid rgba(255,255,255,0.2);
                border-radius:2px;padding:6px 14px;font-size:12px;font-family:monospace;}
            QPushButton:hover{border-color:rgba(255,255,255,0.6);}
            QPushButton#ok{background:rgba(20,50,30,0.8);
                border:1px solid rgba(60,180,80,0.5);color:#a0ffb0;font-weight:bold;}
        """)
        self.cfg = dict(cfg)
        l=QVBoxLayout(self); l.setContentsMargins(28,28,28,28); l.setSpacing(20)
        t=QLabel("// SETTINGS")
        t.setStyleSheet("color:#fff;font-size:15px;font-family:monospace;letter-spacing:4px;")
        l.addWidget(t)
        form=QFormLayout(); form.setSpacing(16)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.wh=QLineEdit(cfg.get("webhook_url","")); self.wh.setPlaceholderText("/hook/bruh")
        self.nm=QLineEdit(cfg.get("ai_name","Agent"))
        self.ch=QLineEdit(cfg.get("character_image",""))
        br=QPushButton("browse"); br.clicked.connect(self._pick)
        row=QHBoxLayout(); row.addWidget(self.ch); row.addWidget(br)
        form.addRow("webhook :", self.wh)
        form.addRow("ai name :", self.nm)
        form.addRow("character :", row)
        l.addLayout(form)
        btns=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setObjectName("ok")
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("confirm")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("cancel")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        l.addWidget(btns)
    def _pick(self):
        p,_=QFileDialog.getOpenFileName(self,"Character","","Images (*.png *.jpg *.gif)")
        if p: self.ch.setText(p)
    def _ok(self):
        self.cfg["webhook_url"]=self.wh.text().strip()
        self.cfg["ai_name"]=self.nm.text().strip() or "Agent"
        self.cfg["character_image"]=self.ch.text().strip()
        self.accept()

# ── stylesheet ────────────────────────────────────────────────────────────────

STYLE = """
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 4px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.15); border-radius:2px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QFrame#abubble {
    background: rgba(10,14,22,0.74);
    border-radius: 2px; border-top-left-radius: 0px;
    border: 1px solid rgba(255,255,255,0.08);
}
QFrame#ububble {
    background: rgba(20,40,70,0.65);
    border-radius: 2px; border-bottom-right-radius: 0px;
    border: 1px solid rgba(255,255,255,0.10);
}
QFrame#reqbubble {
    background: rgba(18,16,10,0.82);
    border-radius: 2px; border-top-left-radius: 0px;
    border: 1px solid rgba(255,210,122,0.35);
}

QTextEdit#inp {
    background: transparent; color: #ffffff; border: none;
    border-bottom: 1px solid rgba(255,255,255,0.20);
    font-size: 16px; font-family: monospace; padding: 4px 6px;
}
QTextEdit#inp:focus { border-bottom: 1px solid rgba(255,255,255,0.60); }

QPushButton#snd, QPushButton#mic { background: transparent; border: none; color: #ffffff;
    font-size: 20px; font-family: monospace; font-weight: bold; }
QPushButton#snd:hover, QPushButton#mic:hover { color: rgba(200,255,200,1); }

QPushButton#gear { background: transparent; border: none;
    color: rgba(255,255,255,0.55); font-size: 22px; }
QPushButton#gear:hover { color: #ffffff; }

QPushButton#deny, QPushButton#conf {
    background: transparent; border: 1px solid rgba(255,255,255,0.25);
    border-radius: 2px; padding: 5px 14px; font-size: 13px;
    font-family: monospace; color: #ffffff;
}
QPushButton#deny:hover { border-color: rgba(255,120,120,0.9); color:#ff9a9a; }
QPushButton#conf:hover { border-color: rgba(80,200,110,0.9); color:#a0ffb0; }
QPushButton#deny:disabled, QPushButton#conf:disabled { color: rgba(255,255,255,0.3); }

QLabel#aname { color: rgba(255,255,255,0.70); font-size: 14px;
    font-family: monospace; letter-spacing: 3px; font-weight: bold; }
QLabel#status { color: rgba(255,255,255,0.20); font-size: 11px; font-family: monospace; }
"""

# ── main window ───────────────────────────────────────────────────────────────

class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.history = load_history()
        self._seen_request_sig = None

        self.setWindowTitle("Term Chat")
        self.resize(self.cfg["window_w"], self.cfg["window_h"])
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)

        self._drag_pos = None
        self._bg_px = None
        self._char_px = None
        self._send_thread = None
        self._audio_thread = None
        self._mic_source = detect_mic_source()
        self._is_rec = False
        self._hold_t = None
        self._rec_proc = None
        self._rec_tmp = None
        self._vol_thread = None

        self._load_assets()
        self._build()
        self._load_history_bubbles()
        QTimer.singleShot(100, self._inp.setFocus)

        self._bg_timer = QTimer(); self._bg_timer.timeout.connect(self._reload_bg)
        self._bg_timer.start(60_000)

        # watch the request file
        self._req_timer = QTimer(); self._req_timer.timeout.connect(self._check_request)
        self._req_timer.start(1000)

    # ── assets ──
    def _load_assets(self):
        p = bg_for_hour(int(time.strftime("%H")))
        self._bg_px = QPixmap(p) if os.path.exists(p) else None
        cp = self.cfg.get("character_image","")
        self._char_px = QPixmap(cp) if (cp and os.path.exists(cp)) else None

    def _reload_bg(self):
        p = bg_for_hour(int(time.strftime("%H")))
        if os.path.exists(p): self._bg_px = QPixmap(p); self.update()

    # ── background paint: top-aligned to window top ──
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = self.rect()
        if self._bg_px and not self._bg_px.isNull():
            s = self._bg_px.scaled(r.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
            ox = (s.width()-r.width())//2
            oy = 0   # top of image aligns with top of window
            p.drawPixmap(0,0,s,ox,oy,r.width(),r.height())
        else:
            p.fillRect(r, QColor(8,10,18))
        p.fillRect(r, QColor(0,0,0,55))
        p.end()

    # ── drag ──
    def mousePressEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag_pos)
    def mouseReleaseEvent(self, e): self._drag_pos=None

    # ── build ──
    def _build(self):
        root = QWidget(); self.setCentralWidget(root)
        root.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        base = QWidget(); base.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        bl = QHBoxLayout(base); bl.setContentsMargins(0,0,0,0); bl.setSpacing(0)
        char_col = int(self.cfg["window_w"] * 0.34)
        bl.addSpacing(char_col - 60)
        right = QWidget(); right.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        bl.addWidget(right, 1)

        self._char_lbl = QLabel(root)
        self._char_lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._char_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._char_lbl.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft)
        self._char_lbl.setGeometry(-20, 40, char_col+40, self.height())
        self._char_lbl.raise_()
        self._render_char()

        rl = QHBoxLayout(root); rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)
        rl.addWidget(base)

        rlay = QVBoxLayout(right); rlay.setContentsMargins(0,0,0,0); rlay.setSpacing(0)

        top = QWidget(); top.setFixedHeight(48)
        top.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        tl = QHBoxLayout(top); tl.setContentsMargins(14,0,12,0)
        self._name_lbl = QLabel(self.cfg["ai_name"].upper()); self._name_lbl.setObjectName("aname")
        tl.addWidget(self._name_lbl); tl.addStretch()
        self._status_lbl = QLabel("ready"); self._status_lbl.setObjectName("status")
        tl.addWidget(self._status_lbl); tl.addSpacing(6)
        gear = QPushButton("⚙"); gear.setObjectName("gear")
        gear.setFixedSize(34,34); gear.clicked.connect(self._open_settings)
        tl.addWidget(gear)
        rlay.addWidget(top)

        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background:transparent;")
        self._msg_w = QWidget(); self._msg_w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._msg_l = QVBoxLayout(self._msg_w)
        self._msg_l.setContentsMargins(8,10,8,10); self._msg_l.setSpacing(2)
        self._msg_l.addStretch()
        self._scroll.setWidget(self._msg_w)
        rlay.addWidget(self._scroll, 1)

        foot = QWidget(); foot.setFixedHeight(56)
        foot.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        fl = QHBoxLayout(foot); fl.setContentsMargins(12,8,12,8); fl.setSpacing(10)

        self._mic_btn = QPushButton("\uf130"); self._mic_btn.setObjectName("mic")
        self._mic_btn.setFixedSize(34,34)
        self._mic_btn.pressed.connect(self._mic_press)
        self._mic_btn.released.connect(self._mic_release)
        fl.addWidget(self._mic_btn)

        self._inp = QTextEdit(); self._inp.setObjectName("inp")
        self._inp.setPlaceholderText("transmit…"); self._inp.setFixedHeight(40)
        self._inp.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._inp.installEventFilter(self)
        fl.addWidget(self._inp)

        self._waveform = WaveformWidget(); self._waveform.setFixedHeight(40); self._waveform.hide()
        fl.addWidget(self._waveform)

        snd = QPushButton("→"); snd.setObjectName("snd")
        snd.setFixedSize(34,34); snd.clicked.connect(self._send_text)
        fl.addWidget(snd)
        rlay.addWidget(foot)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        char_col = int(self.width()*0.34)
        if hasattr(self,"_char_lbl"):
            self._char_lbl.setGeometry(-20, 40, char_col+40, self.height())
            self._char_lbl.raise_()
        self._render_char()

    def _render_char(self):
        if self._char_px and not self._char_px.isNull():
            h = int(self.height()*1.01)   # ~10% smaller than before, still close/cropped
            self._char_lbl.setPixmap(
                self._char_px.scaledToHeight(h, Qt.TransformationMode.SmoothTransformation))

    # ── key intercept ──
    def eventFilter(self, obj, event):
        if obj is self._inp and isinstance(event, QKeyEvent):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self._send_text(); return True
        return super().eventFilter(obj, event)

    # ── history ──
    def _load_history_bubbles(self):
        for msg in self.history[-40:]:
            b = Bubble(msg["role"], msg["text"], msg.get("type","text"))
            self._msg_l.insertWidget(self._msg_l.count()-1, b)
        QTimer.singleShot(60, self._scroll_bottom)

    def _add_bubble(self, role, text, store=True):
        b = Bubble(role, text)
        self._msg_l.insertWidget(self._msg_l.count()-1, b)
        if store:
            self.history.append({"role":role,"text":text,"type":"text","ts":int(time.time())})
            save_history(self.history)
        QTimer.singleShot(40, self._scroll_bottom)

    def _scroll_bottom(self):
        sb = self._scroll.verticalScrollBar(); sb.setValue(sb.maximum())

    # ── send text ──
    def _send_text(self):
        txt = self._inp.toPlainText().strip()
        if not txt: return
        self._inp.clear()
        self._add_bubble("user", txt)
        url = build_full_url(self.cfg.get("webhook_url",""))
        if not url:
            self._add_bubble("ai", "[ no webhook — open ⚙ settings ]"); return
        self._status_lbl.setText("sending…")
        self._send_thread = SendThread(url, {"message":txt,"type":"text"}, self)
        self._send_thread.response_ready.connect(self._on_reply)
        self._send_thread.start()

    def _on_reply(self, text):
        self._status_lbl.setText("ready")
        if not text: return
        if text.startswith("AUDIO::"):
            url = text[len("AUDIO::"):]
            self._add_bubble("ai", "[ fetching audio… ]", store=False)
            self._audio_thread = AudioDLThread(url, self)
            self._audio_thread.done.connect(self._on_audio)
            self._audio_thread.start()
            return

        # Permission request: "Permission (command) reason text..."
        # Detected by literal prefix. The command is whatever's inside the
        # first matching pair of parentheses. Everything after the closing
        # paren is the reason shown to the user.
        stripped = text.lstrip()
        if stripped.startswith("Permission") and "(" in stripped and ")" in stripped:
            try:
                start = stripped.index("(")
                end   = stripped.index(")", start+1)
                cmd   = stripped[start+1:end].strip()
                reason = stripped[end+1:].strip() or "(no reason given)"
                rb = RequestBubble(reason, cmd,
                                   lambda dec, c: self._on_request_decision("", dec, c))
                self._msg_l.insertWidget(self._msg_l.count()-1, rb)
                self.history.append({"role":"ai","text":f"[request] {reason}",
                                     "type":"text","ts":int(time.time())})
                save_history(self.history)
                QTimer.singleShot(40, self._scroll_bottom)
                return
            except Exception:
                pass   # fall through to plain bubble

        self._add_bubble("ai", text)

    def _on_audio(self, path):
        # remove the "fetching" placeholder
        n = self._msg_l.count()
        item = self._msg_l.itemAt(n-2)
        if item and item.widget(): item.widget().deleteLater(); self._msg_l.removeItem(item)
        if not path:
            self._add_bubble("ai","[ audio failed ]"); return
        b = Bubble("ai", path, "audio")
        self._msg_l.insertWidget(self._msg_l.count()-1, b)
        self.history.append({"role":"ai","text":path,"type":"audio","ts":int(time.time())})
        save_history(self.history)
        subprocess.Popen(["mpv","--no-video",path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        QTimer.singleShot(40, self._scroll_bottom)

    # ── mic ──
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
            "background:transparent;border:none;color:#ffffff;font-size:20px;font-weight:bold;")
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
            b = Bubble("user", self._rec_tmp.name, "audio")
            self._msg_l.insertWidget(self._msg_l.count()-1, b)
            self.history.append({"role":"user","text":self._rec_tmp.name,"type":"audio","ts":int(time.time())})
            save_history(self.history)
            QTimer.singleShot(40, self._scroll_bottom)
            url = build_full_url(self.cfg.get("webhook_url",""))
            if not url:
                self._status_lbl.setText("ready")
                self._add_bubble("ai","[ no webhook — open ⚙ settings ]"); return
            self._send_thread = SendThread(url, audio, "audio", self)
            self._send_thread.response_ready.connect(self._on_reply)
            self._send_thread.start()

    def _update_vol(self, level):
        self._waveform.set_level(level)

    # ── request file watcher ──
    def _check_request(self):
        if not os.path.exists(REQUEST_FILE): return
        try:
            raw = open(REQUEST_FILE).read().strip()
        except: return
        if not raw or raw in ("{}", "[]", "null"): return
        try:
            data = json.loads(raw)
        except: return
        if not isinstance(data, dict) or not data: return

        sig = json.dumps(data, sort_keys=True)
        if sig == self._seen_request_sig: return
        self._seen_request_sig = sig

        req_text = data.get("request") or data.get("text") or "(request)"
        command  = data.get("command", "")
        reply    = data.get("reply", "")
        req_id   = data.get("id", "")

        if reply: self._add_bubble("ai", reply)

        rb = RequestBubble(req_text, command,
                           lambda dec, cmd: self._on_request_decision(req_id, dec, cmd))
        self._msg_l.insertWidget(self._msg_l.count()-1, rb)
        self.history.append({"role":"ai","text":f"[request] {req_text}",
                             "type":"text","ts":int(time.time())})
        save_history(self.history)
        QTimer.singleShot(40, self._scroll_bottom)

    def _on_request_decision(self, req_id, decision, command):
        # 1) wipe the watched file empty
        try:
            with open(REQUEST_FILE,"w") as f: f.write("{}")
        except: pass
        self._seen_request_sig = None

        # 2) POST decision back to the webhook
        url = build_full_url(self.cfg.get("webhook_url",""))
        if url:
            payload = {"id":req_id, "decision":decision, "command":command}
            self._send_thread = SendThread(url, payload, self)
            self._send_thread.response_ready.connect(self._on_reply)
            self._send_thread.start()

        # 3) if approved, hand the command to the runner script
        if decision == "approved" and command:
            try:
                with open(APPROVED_FILE,"w") as f:
                    json.dump({"command":command,"id":req_id,"ts":int(time.time())}, f, indent=2)
            except: pass
            if os.path.exists(RUNNER):
                subprocess.Popen(["bash", RUNNER],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ── settings ──
    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg = dlg.cfg; save_config(self.cfg)
            self._name_lbl.setText(self.cfg["ai_name"].upper())
            self._load_assets(); self._render_char(); self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape: self.close()


def main():
    os.makedirs(ASSETS, exist_ok=True)
    app = QApplication(sys.argv)
    w = ChatWindow(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
