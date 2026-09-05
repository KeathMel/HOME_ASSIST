#!/usr/bin/env python3
"""
Chat Room — a small multi-AI group chat.

Three AIs (Schwortz, Eira, Lyren) each have their own webhook, bubble colour,
and character layer in the room scene. Above the input are three name buttons;
tap to select one or several. Your message is POSTed to every selected AI's
webhook. Selected AIs light up in the scene; unselected ones are dimmed.

App -> n8n  : {"message": "...", "ai": "Eira", "type": "text"}   (one POST per selected AI)
n8n -> App  : {"name": "Eira", "text": "..."}  or  {"name":"Eira","audio_url":"..."}
              (if "name" missing, the app labels the reply by the webhook it sent to)
"""

import sys, os, json, time, base64, tempfile, subprocess, re, math
import urllib.request
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QScrollArea, QFrame, QFileDialog,
    QDialog, QLineEdit, QFormLayout, QDialogButtonBox, QSizePolicy,
    QGraphicsOpacityEffect,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRect
from PyQt6.QtGui import QPixmap, QColor, QPainter, QPen, QKeyEvent, QImage

ASSETS       = os.path.expanduser("~/.config/MASTER_VAULT/HOME_ASSIST/chat-room")
CONFIG_FILE  = os.path.join(ASSETS, "config.json")
HISTORY_FILE = os.path.join(ASSETS, "history.json")

# tunnel_url lives one level up, in HOME_ASSIST, shared by every app
sys.path.insert(0, os.path.dirname(ASSETS))
try:
    from tunnel_url import webhook_url
except Exception:
    # no helper found -> fall back to the local runner, so the app still
    # runs on this machine even if the tunnel file is missing entirely
    def webhook_url(path, fallback="http://127.0.0.1:5800"):
        if not path.startswith("/"):
            path = "/" + path
        return fallback + path

# ── AI roster ─────────────────────────────────────────────────────────────────
# colour = bubble colour; default character layout in the room scene.
# pos is fractional (x,y) of the window, scale is fraction of window height,
# anchor "bottom" means y is the bottom edge of the sprite.
AIS = [
    {"key":"schwortz", "name":"Schwortz", "img":"schwortz.png",
     "color":"rgba(30,70,45,0.85)",  "border":"rgba(90,200,130,0.6)",
     "pos":(0.82,0.85), "scale":0.62},   # standing further back, mid-room
    {"key":"eira",     "name":"Eira",     "img":"Eira.png",
     "color":"rgba(90,65,20,0.85)",  "border":"rgba(230,180,80,0.6)",
     "pos":(0.60,0.75), "scale":0.22},   # behind the couch table — smallest
    {"key":"lyren",    "name":"Lyren",    "img":"Lyren.png",
     "color":"rgba(95,85,20,0.88)",  "border":"rgba(245,225,70,0.7)",
     "pos":(0.26,0.90), "scale":0.54},   # on the couch (left) — bigger
]
AI_BY_NAME = {a["name"].lower(): a for a in AIS}

DEFAULT_CFG = {
    "webhooks": {"Schwortz":"", "Eira":"", "Lyren":""},
    "window_w": 367, "window_h": 394,
    "dim_day": 30, "dim_night": 80,
}

def load_config():
    if not os.path.exists(CONFIG_FILE): return dict(DEFAULT_CFG)
    try:
        with open(CONFIG_FILE) as f:
            c = json.load(f)
        return {**DEFAULT_CFG, **c, "webhooks":{**DEFAULT_CFG["webhooks"], **c.get("webhooks",{})}}
    except: return dict(DEFAULT_CFG)

def save_config(cfg):
    os.makedirs(ASSETS, exist_ok=True)
    with open(CONFIG_FILE,"w") as f: json.dump(cfg, f, indent=2)

def load_history():
    if not os.path.exists(HISTORY_FILE): return []
    try:
        with open(HISTORY_FILE) as f: return json.load(f)
    except: return []

def save_history(msgs):
    os.makedirs(ASSETS, exist_ok=True)
    with open(HISTORY_FILE,"w") as f: json.dump(msgs[-200:], f, indent=2)

def is_night():
    h = int(time.strftime("%H"))
    return h < 6 or h >= 18

# ── send worker ───────────────────────────────────────────────────────────────

class SendThread(QThread):
    response_ready = pyqtSignal(str, str)   # (ai_name, text)
    def __init__(self, url, ai_name, payload, ptype="json", parent=None):
        super().__init__(parent)
        self.url, self.ai_name, self.payload, self.ptype = url, ai_name, payload, ptype
    def run(self):
        try:
            import gzip, secrets
            if self.ptype == "audio" and isinstance(self.payload, bytes):
                raw, ct = self.payload, "audio/webm"
            else:
                raw = json.dumps(self.payload).encode(); ct = "application/json"

            # gzip the body and tag with a custom header so n8n can route
            body = gzip.compress(raw)
            headers = {
                "Content-Type": ct,
                "Content-Encoding": "gzip",
                "X-Chatroom-Source": "chat-room",
                "X-Chatroom-Token":  secrets.token_hex(8),
            }
            req = urllib.request.Request(
                self.url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode().strip()
            name = self.ai_name
            if body.startswith("{"):
                p = json.loads(body)
                name = p.get("name", name)
                if "audio_url" in p:
                    self.response_ready.emit(name, "AUDIO::"+p["audio_url"]); return
                for k in ("text","message","reply"):
                    if k in p: self.response_ready.emit(name, str(p[k])); return
                self.response_ready.emit(name, body)
            elif body:
                self.response_ready.emit(name, body)
            else:
                self.response_ready.emit(name, "")
        except Exception as e:
            self.response_ready.emit(self.ai_name, f"[error: {e}]")

class AudioDLThread(QThread):
    done = pyqtSignal(str, str)
    def __init__(self, name, url, parent=None):
        super().__init__(parent); self.name=name; self.url=url
    def run(self):
        try:
            suffix = Path(self.url.split("?")[0]).suffix or ".mp3"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            urllib.request.urlretrieve(self.url, tmp.name)
            self.done.emit(self.name, tmp.name)
        except: self.done.emit(self.name, "")

def detect_mic_source():
    try:
        out = subprocess.check_output(["pactl","list","sources","short"],
                                      text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            low=line.lower()
            if "monitor" in low or "output" in low or "sink" in low: continue
            parts=line.split()
            if len(parts)>=2: return parts[1]
    except: pass
    return "default"

class VolMonitorThread(QThread):
    level = pyqtSignal(int)
    def __init__(self, source, parent=None):
        super().__init__(parent); self.source=source; self._running=True
    def run(self):
        while self._running:
            try:
                out=subprocess.check_output(["pactl","get-source-volume",self.source],
                                            text=True, stderr=subprocess.DEVNULL)
                nums=re.findall(r'(\d+)%', out)
                if nums: self.level.emit(int(nums[0]))
            except: pass
            self.msleep(80)
    def stop(self): self._running=False

class WaveformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._level=0; self._phase=0.0
        self._t=QTimer(); self._t.timeout.connect(self._tick); self._t.start(30)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    def set_level(self, lv): self._level=lv
    def _tick(self): self._phase+=0.15; self.update()
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w,h=self.width(),self.height(); cy=h/2
        amp=(self._level/100.0)*(h*0.40)
        if amp<1:
            p.setPen(QPen(QColor(255,255,255,60),1.5)); p.drawLine(0,int(cy),w,int(cy)); p.end(); return
        p.setPen(QPen(QColor(245,225,120,200),1.8))
        pts=[(x, cy+amp*math.sin(x*0.05+self._phase)*math.sin(x*0.013+self._phase*0.6)) for x in range(0,w,3)]
        for i in range(len(pts)-1):
            p.drawLine(int(pts[i][0]),int(pts[i][1]),int(pts[i+1][0]),int(pts[i+1][1]))
        p.end()

# ── bubble ────────────────────────────────────────────────────────────────────

class Bubble(QFrame):
    def __init__(self, role, text, ai=None, msg_type="text", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        is_user = role=="user"
        outer=QHBoxLayout(self); outer.setContentsMargins(5,2,5,2)
        box=QFrame()
        if is_user:
            box.setStyleSheet("background:rgba(60,60,68,0.78);border-radius:9px;"
                              "border-bottom-right-radius:2px;")
        else:
            a=ai or {}
            box.setStyleSheet(f"background:{a.get('color','rgba(40,40,48,0.8)')};"
                              f"border-radius:9px;border-top-left-radius:2px;"
                              f"border:1px solid {a.get('border','rgba(255,255,255,0.12)')};")
        il=QVBoxLayout(box); il.setContentsMargins(10,6,10,6); il.setSpacing(2)

        if not is_user and ai:
            nm=QLabel(ai["name"])
            nm.setStyleSheet(f"color:{ai['border']};font-size:10px;font-weight:bold;"
                             f"font-family:monospace;letter-spacing:1px;")
            il.addWidget(nm)

        if msg_type=="audio":
            btn=QPushButton("▶  audio")
            btn.setStyleSheet("background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.25);"
                              "border-radius:2px;color:#fff;padding:5px 10px;font-size:12px;font-family:monospace;")
            btn.clicked.connect(lambda: subprocess.Popen(["mpv","--no-video",text],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            il.addWidget(btn)
        else:
            lbl=QLabel(text); lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setStyleSheet("font-size:13px;color:#f0f0f0;")
            il.addWidget(lbl)

        box.setMaximumWidth(250)
        if is_user: outer.addStretch(); outer.addWidget(box)
        else: outer.addWidget(box); outer.addStretch()

# ── settings ──────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chat Room — Settings"); self.setMinimumWidth(460)
        self.setStyleSheet("""
            QDialog{background:#0a0a10;color:#e0e0e0;}
            QLabel{color:#a0a0a0;font-size:12px;font-family:monospace;}
            QLineEdit{background:#14141c;color:#fff;border:none;
                border-bottom:1px solid rgba(255,255,255,0.2);
                padding:7px 4px;font-size:12px;font-family:monospace;}
            QLineEdit:focus{border-bottom:1px solid rgba(255,255,255,0.7);}
            QPushButton{background:transparent;color:#fff;border:1px solid rgba(255,255,255,0.2);
                border-radius:2px;padding:6px 14px;font-size:12px;font-family:monospace;}
            QPushButton#ok{background:rgba(20,50,30,0.8);border:1px solid rgba(60,180,80,0.5);
                color:#a0ffb0;font-weight:bold;}
        """)
        self.cfg=dict(cfg); self.cfg["webhooks"]=dict(cfg.get("webhooks",{}))
        l=QVBoxLayout(self); l.setContentsMargins(26,26,26,26); l.setSpacing(18)
        t=QLabel("// CHAT ROOM"); t.setStyleSheet("color:#fff;font-size:15px;font-family:monospace;letter-spacing:4px;")
        l.addWidget(t)
        sub=QLabel("webhook path for each AI  (the tunnel supplies the rest):")
        l.addWidget(sub)
        form=QFormLayout(); form.setSpacing(14); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.fields={}
        for a in AIS:
            nm=a["name"]
            fld=QLineEdit(self.cfg["webhooks"].get(nm,"")); fld.setPlaceholderText("/hook/bruh")
            self.fields[nm]=fld
            form.addRow(f"{nm} :", fld)
        l.addLayout(form)
        btns=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setObjectName("ok")
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("save")
        btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        l.addWidget(btns)
    def _ok(self):
        for nm,fld in self.fields.items():
            self.cfg["webhooks"][nm]=fld.text().strip()
        self.accept()

# ── stylesheet ────────────────────────────────────────────────────────────────

STYLE = """
QScrollArea{background:transparent;border:none;}
QScrollBar:vertical{background:transparent;width:4px;}
QScrollBar::handle:vertical{background:rgba(255,255,255,0.15);border-radius:2px;min-height:18px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}

QTextEdit#inp{background:transparent;color:#ffffff;border:none;
    border-bottom:1px solid rgba(255,255,255,0.20);
    font-size:13px;font-family:monospace;padding:3px 5px;}
QTextEdit#inp:focus{border-bottom:1px solid rgba(255,255,255,0.55);}

QPushButton#snd,QPushButton#mic{background:transparent;border:none;color:#fff;
    font-size:17px;font-family:monospace;font-weight:bold;}
QPushButton#snd:hover,QPushButton#mic:hover{color:rgba(245,225,120,1);}
QPushButton#gear{background:transparent;border:none;color:rgba(255,255,255,0.5);font-size:18px;}
QPushButton#gear:hover{color:#fff;}
"""

# ── main window ───────────────────────────────────────────────────────────────

class ChatRoom(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg=load_config()
        self.history=load_history()
        self.setWindowTitle("Chat Room")
        self.resize(self.cfg["window_w"], self.cfg["window_h"])
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)

        self._drag_pos=None
        self._bg_px=None
        self._selected=set()             # set of ai keys
        self._char_lbls={}               # key -> QLabel
        self._char_px={}                 # key -> keyed cut-out pixmap
        self._char_raw={}                # key -> original solid pixmap
        self._name_btns={}               # key -> QPushButton
        self._mic_source=detect_mic_source()
        self._is_rec=False; self._hold_t=None; self._rec_proc=None
        self._rec_tmp=None; self._vol_thread=None
        self._threads=[]

        self._load_assets()
        self._build()
        self._load_history_bubbles()
        QTimer.singleShot(100, self._inp.setFocus)
        self._bg_timer=QTimer(); self._bg_timer.timeout.connect(self._reload_bg); self._bg_timer.start(60_000)

    def _load_assets(self):
        p = os.path.join(ASSETS, "room_night.png" if is_night() else "room_day.png")
        self._bg_px = QPixmap(p) if os.path.exists(p) else None
        for a in AIS:
            ip=os.path.join(ASSETS, a["img"])
            px = QPixmap(ip) if os.path.exists(ip) else None
            if px and not px.isNull():
                keyed = self._key_black(px)
                box   = self._bbox(keyed)
                self._char_px[a["key"]]  = keyed.copy(box) if box else keyed
                self._char_raw[a["key"]] = px.copy(box) if box else px
            else:
                self._char_px[a["key"]]=px; self._char_raw[a["key"]]=px

    def _bbox(self, px):
        try:
            import numpy as np
            img=px.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            w,h=img.width(),img.height(); ptr=img.bits(); ptr.setsize(h*w*4)
            arr=np.frombuffer(ptr,np.uint8).reshape((h,w,4))
            alpha=arr[:,:,3]>25
            ys=np.any(alpha,axis=1); xs=np.any(alpha,axis=0)
            if not ys.any() or not xs.any(): return None
            miny,maxy=np.where(ys)[0][[0,-1]]; minx,maxx=np.where(xs)[0][[0,-1]]
            pad=4
            minx=max(0,int(minx)-pad); miny=max(0,int(miny)-pad)
            maxx=min(w,int(maxx)+pad); maxy=min(h,int(maxy)+pad)
            return QRect(minx,miny,maxx-minx,maxy-miny)
        except Exception:
            return None

    def _key_black(self, px):
        """Make near-black pixels transparent; force the rest fully opaque."""
        try:
            import numpy as np
            img = px.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            w,h = img.width(), img.height()
            ptr = img.bits(); ptr.setsize(h*w*4)
            arr = np.frombuffer(ptr, np.uint8).reshape((h,w,4)).copy()
            rgb = arr[:,:,:3].astype(np.int16)
            black = (rgb[:,:,0]<16)&(rgb[:,:,1]<16)&(rgb[:,:,2]<16)
            arr[:,:,3] = 255          # everything solid by default
            arr[black,3] = 0          # only true-black becomes transparent
            out = QImage(arr.data, w, h, QImage.Format.Format_RGBA8888).copy()
            return QPixmap.fromImage(out)
        except Exception:
            return px

    def _autocrop(self, px):
        """Trim fully-transparent margins so positioning uses the real character."""
        try:
            import numpy as np
            img = px.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            w,h = img.width(), img.height()
            ptr = img.bits(); ptr.setsize(h*w*4)
            arr = np.frombuffer(ptr, np.uint8).reshape((h,w,4))
            alpha = arr[:,:,3] > 25
            ys = np.any(alpha, axis=1); xs = np.any(alpha, axis=0)
            if not ys.any() or not xs.any(): return px
            miny,maxy = np.where(ys)[0][[0,-1]]
            minx,maxx = np.where(xs)[0][[0,-1]]
            pad=4
            minx=max(0,int(minx)-pad); miny=max(0,int(miny)-pad)
            maxx=min(w,int(maxx)+pad); maxy=min(h,int(maxy)+pad)
            return px.copy(QRect(minx,miny,maxx-minx,maxy-miny))
        except Exception:
            return px

    def _reload_bg(self):
        p=os.path.join(ASSETS,"room_night.png" if is_night() else "room_day.png")
        if os.path.exists(p): self._bg_px=QPixmap(p); self.update()

    # background top-aligned with dim overlay
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r=self.rect()
        if self._bg_px and not self._bg_px.isNull():
            s=self._bg_px.scaled(r.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
            ox=(s.width()-r.width())//2
            p.drawPixmap(0,0,s,ox,0,r.width(),r.height())
        else:
            p.fillRect(r, QColor(10,8,14))
        dim = self.cfg["dim_night"] if is_night() else self.cfg["dim_day"]
        p.fillRect(r, QColor(0,0,0,dim))
        p.end()

    def mousePressEvent(self,e):
        if e.button()==Qt.MouseButton.LeftButton:
            self._drag_pos=e.globalPosition().toPoint()-self.frameGeometry().topLeft()
    def mouseMoveEvent(self,e):
        if self._drag_pos and e.buttons()==Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self._drag_pos)
    def mouseReleaseEvent(self,e): self._drag_pos=None

    def _build(self):
        root=QWidget(); self.setCentralWidget(root)
        root.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # character layers (under the chat UI)
        for a in AIS:
            lbl=QLabel(root)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.setAlignment(Qt.AlignmentFlag.AlignBottom|Qt.AlignmentFlag.AlignHCenter)
            self._char_lbls[a["key"]]=lbl
        self._place_chars()

        col=QVBoxLayout(root); col.setContentsMargins(0,0,0,0); col.setSpacing(0)

        # top bar
        top=QWidget(); top.setFixedHeight(34); top.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        tl=QHBoxLayout(top); tl.setContentsMargins(10,0,8,0)
        title=QLabel("CHAT ROOM"); title.setStyleSheet("color:rgba(255,255,255,0.55);font-size:10px;font-family:monospace;letter-spacing:3px;")
        tl.addWidget(title); tl.addStretch()
        gear=QPushButton("⚙"); gear.setObjectName("gear"); gear.setFixedSize(28,28)
        gear.clicked.connect(self._open_settings); tl.addWidget(gear)
        col.addWidget(top)

        # messages
        self._scroll=QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background:transparent;border:none;")
        self._scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._scroll.viewport().setStyleSheet("background:transparent;")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._msg_w=QWidget(); self._msg_w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._msg_w.setStyleSheet("background:transparent;")
        self._msg_l=QVBoxLayout(self._msg_w); self._msg_l.setContentsMargins(4,6,4,6); self._msg_l.setSpacing(1)
        self._msg_l.addStretch()
        self._scroll.setWidget(self._msg_w)
        col.addWidget(self._scroll,1)

        # name selector row
        sel=QWidget(); sel.setFixedHeight(32); sel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        sl=QHBoxLayout(sel); sl.setContentsMargins(6,2,6,2); sl.setSpacing(5)
        for a in AIS:
            b=QPushButton(a["name"])
            b.setCheckable(True); b.setFixedHeight(26)
            b.clicked.connect(lambda chk,k=a["key"]: self._toggle_ai(k))
            self._name_btns[a["key"]]=b
            self._style_name_btn(a["key"])
            sl.addWidget(b)
        col.addWidget(sel)

        # input row
        foot=QWidget(); foot.setFixedHeight(46); foot.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        fl=QHBoxLayout(foot); fl.setContentsMargins(8,5,8,7); fl.setSpacing(7)
        self._mic_btn=QPushButton("\uf130"); self._mic_btn.setObjectName("mic"); self._mic_btn.setFixedSize(28,28)
        self._mic_btn.pressed.connect(self._mic_press); self._mic_btn.released.connect(self._mic_release)
        fl.addWidget(self._mic_btn)
        self._inp=QTextEdit(); self._inp.setObjectName("inp"); self._inp.setPlaceholderText("message…")
        self._inp.setFixedHeight(34); self._inp.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._inp.installEventFilter(self)
        fl.addWidget(self._inp)
        self._waveform=WaveformWidget(); self._waveform.setFixedHeight(34); self._waveform.hide()
        fl.addWidget(self._waveform)
        snd=QPushButton("→"); snd.setObjectName("snd"); snd.setFixedSize(28,28); snd.clicked.connect(self._send_text)
        fl.addWidget(snd)
        col.addWidget(foot)

    def _place_chars(self):
        W,H=self.width(),self.height()
        for a in AIS:
            lbl=self._char_lbls[a["key"]]
            selected = a["key"] in self._selected
            px = self._char_raw.get(a["key"]) if selected else self._char_px.get(a["key"])
            if px and not px.isNull():
                hh=int(H*a["scale"])
                scaled=px.scaledToHeight(hh, Qt.TransformationMode.SmoothTransformation)
                lbl.setPixmap(scaled)
                fx,fy=a["pos"]
                x=int(W*fx - scaled.width()/2); y=int(H*fy - scaled.height())
                lbl.setGeometry(x,y,scaled.width(),scaled.height())
            self._apply_char_opacity(a["key"])
            lbl.lower()

    def _apply_char_opacity(self, key):
        lbl=self._char_lbls[key]
        eff=lbl.graphicsEffect()
        if eff is None:
            eff=QGraphicsOpacityEffect(lbl)
            lbl.setGraphicsEffect(eff)
        eff.setOpacity(1.0 if key in self._selected else 0.5)
        lbl.update()

    def _style_name_btn(self, key):
        a=AI_BY_NAME[key.lower()] if key.lower() in AI_BY_NAME else next(x for x in AIS if x["key"]==key)
        on = key in self._selected
        if on:
            self._name_btns[key].setStyleSheet(
                f"background:{a['color']};border:1px solid {a['border']};border-radius:3px;"
                f"color:#fff;font-size:11px;font-family:monospace;font-weight:bold;padding:2px 6px;")
        else:
            self._name_btns[key].setStyleSheet(
                "background:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.15);border-radius:3px;"
                "color:rgba(255,255,255,0.6);font-size:11px;font-family:monospace;padding:2px 6px;")

    def _toggle_ai(self, key):
        if key in self._selected: self._selected.discard(key)
        else: self._selected.add(key)
        self._style_name_btn(key)
        self._place_chars()

    def resizeEvent(self,e):
        super().resizeEvent(e)
        if self._char_lbls: self._place_chars()

    def eventFilter(self,obj,event):
        if obj is self._inp and isinstance(event,QKeyEvent):
            if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
                if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self._send_text(); return True
        return super().eventFilter(obj,event)

    # history
    def _load_history_bubbles(self):
        for m in self.history[-40:]:
            ai = AI_BY_NAME.get((m.get("ai") or "").lower())
            self._msg_l.insertWidget(self._msg_l.count()-1,
                Bubble(m["role"], m["text"], ai, m.get("type","text")))
        QTimer.singleShot(60, self._scroll_bottom)

    def _add_bubble(self, role, text, ai=None, msg_type="text", store=True):
        self._msg_l.insertWidget(self._msg_l.count()-1, Bubble(role,text,ai,msg_type))
        if store:
            self.history.append({"role":role,"text":text,"type":msg_type,
                                 "ai":(ai["name"] if ai else ""),"ts":int(time.time())})
            save_history(self.history)
        QTimer.singleShot(40, self._scroll_bottom)

    def _scroll_bottom(self):
        sb=self._scroll.verticalScrollBar(); sb.setValue(sb.maximum())

    # send
    def _targets(self):
        return [a for a in AIS if a["key"] in self._selected]

    def _send_text(self):
        txt=self._inp.toPlainText().strip()
        if not txt: return
        targets=self._targets()
        if not targets:
            self._add_bubble("ai","[ select at least one AI above ]", None); return
        self._inp.clear()
        self._add_bubble("user", txt)
        self._dispatch(targets, {"message":txt,"type":"text"}, "json", txt)

    def _dispatch(self, targets, payload_base, ptype, audio_bytes=None):
        for a in targets:
            path=self.cfg["webhooks"].get(a["name"],"").strip()
            if not path:
                self._add_bubble("ai", f"[ no webhook path set for {a['name']} ]", a); continue
            # resolved here, per send, on purpose: a quick tunnel gets a new
            # random URL every restart, so anything cached at startup would
            # keep posting to a dead address after the tunnel is toggled
            url = path if path.startswith("http") else webhook_url(path)
            if ptype=="audio":
                payload=audio_bytes
            else:
                payload=dict(payload_base); payload["ai"]=a["name"]
            t=SendThread(url, a["name"], payload, ptype, self)
            t.response_ready.connect(self._on_reply)
            self._threads.append(t); t.start()

    def _on_reply(self, ai_name, text):
        if not text: return
        ai=AI_BY_NAME.get(ai_name.lower())
        if text.startswith("AUDIO::"):
            url=text[len("AUDIO::"):]
            dl=AudioDLThread(ai_name, url, self); dl.done.connect(self._on_audio)
            self._threads.append(dl); dl.start(); return
        self._add_bubble("ai", text, ai)

    def _on_audio(self, ai_name, path):
        ai=AI_BY_NAME.get(ai_name.lower())
        if not path: self._add_bubble("ai","[ audio failed ]", ai); return
        self._add_bubble("ai", path, ai, "audio")
        subprocess.Popen(["mpv","--no-video",path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # mic
    def _mic_press(self):
        self._hold_t=QTimer(); self._hold_t.setSingleShot(True)
        self._hold_t.setInterval(400); self._hold_t.timeout.connect(self._start_rec); self._hold_t.start()
    def _mic_release(self):
        if self._hold_t: self._hold_t.stop()
        if self._is_rec: self._stop_rec()
    def _start_rec(self):
        if not self._targets(): self._add_bubble("ai","[ select an AI first ]",None); return
        self._is_rec=True
        self._inp.hide(); self._waveform.show()
        self._rec_tmp=tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
        self._rec_proc=subprocess.Popen(
            ["ffmpeg","-y","-f","pulse","-i",self._mic_source,self._rec_tmp.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._vol_thread=VolMonitorThread(self._mic_source); self._vol_thread.level.connect(self._waveform.set_level); self._vol_thread.start()
    def _stop_rec(self):
        self._is_rec=False
        self._waveform.hide(); self._waveform.set_level(0); self._inp.show(); self._inp.setFocus()
        if self._vol_thread: self._vol_thread.stop(); self._vol_thread.wait(); self._vol_thread=None
        if self._rec_proc: self._rec_proc.terminate(); self._rec_proc.wait()
        if self._rec_tmp and os.path.exists(self._rec_tmp.name):
            with open(self._rec_tmp.name,"rb") as f: audio=f.read()
            self._add_bubble("user", self._rec_tmp.name, None, "audio")
            self._dispatch(self._targets(), None, "audio", audio)

    def _open_settings(self):
        dlg=SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg=dlg.cfg; save_config(self.cfg)

    def keyPressEvent(self,e):
        if e.key()==Qt.Key.Key_Escape: self.close()

def main():
    os.makedirs(ASSETS, exist_ok=True)
    app=QApplication(sys.argv)
    w=ChatRoom(); w.show()
    sys.exit(app.exec())

if __name__=="__main__":
    main()