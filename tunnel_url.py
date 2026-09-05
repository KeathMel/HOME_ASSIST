"""
tunnel_url.py — the tunnel and its popup, in one file.

  RUN IT   → opens the popup: set the port, start or stop the tunnel,
             copy the URL. Recent ports are one click away.
                 python tunnel_url.py            the popup
                 python tunnel_url.py toggle     no UI, just flip it
                 python tunnel_url.py start|stop|status

  IMPORT IT → the assistant apps get the current base URL from it
                 from tunnel_url import webhook_url
                 url = webhook_url("/hook/bruh")

tunnel.json, tunnel.log and settings.json all live NEXT TO THIS FILE, so
the whole thing moves as one unit and nothing points outside its folder.

PyQt6 is imported inside the popup, not at the top, so an app importing
this purely for webhook_url() never pays for Qt or breaks without it.

The tunnel holds the BASE only (https://something.trycloudflare.com); each
app keeps its own path, so only the half that actually changes lives here.
Quick tunnels get a new random URL on every restart, so call webhook_url()
at SEND time rather than caching it at startup, or the app keeps posting
to a dead address after a toggle.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TUNNEL_FILE = os.path.join(HERE, "tunnel.json")
LOG_FILE = os.path.join(HERE, "tunnel.log")
SETTINGS_FILE = os.path.join(HERE, "settings.json")

DEFAULT_PORT = 5800
KEEP_RECENT = 3
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


# ── settings (port + recent ports) ───────────────────────────────────────

def _settings():
    """Never raises: a missing or broken file just means defaults, which
    shouldn't be enough to stop an app starting."""
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        return {"port": int(s.get("port", DEFAULT_PORT)),
                "recent": [int(p) for p in s.get("recent", [])][:KEEP_RECENT]}
    except Exception:
        return {"port": DEFAULT_PORT, "recent": []}


def current_port():
    return _settings()["port"]


def recent_ports():
    return _settings()["recent"]


def set_port(port):
    """Save the port and push it to the front of the recent list."""
    port = int(port)
    recent = [p for p in recent_ports() if p != port]
    recent.insert(0, port)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"port": port, "recent": recent[:KEEP_RECENT]}, f)
    except Exception:
        pass


def _local_base(port=None):
    return f"http://127.0.0.1:{port or current_port()}"


# ── reading (what the apps use) ──────────────────────────────────────────

def tunnel_base(fallback=None):
    """The live tunnel base URL, or the localhost fallback when it's down —
    which is the right answer anyway for an app on the same machine."""
    if fallback is None:
        fallback = _local_base()
    try:
        with open(TUNNEL_FILE) as f:
            url = (json.load(f).get("url") or "").strip()
        return url.rstrip("/") if url else fallback
    except Exception:
        return fallback


def webhook_url(path, fallback=None):
    """Full URL for a hook path, e.g. webhook_url('/hook/bruh')."""
    if not path.startswith("/"):
        path = "/" + path
    return tunnel_base(fallback) + path


def is_tunnelled():
    return tunnel_base(fallback="") != ""


# ── the tunnel ───────────────────────────────────────────────────────────

def _match(port):
    return f"cloudflared tunnel --url http://127.0.0.1:{port}"


def _pid(port=None):
    """PID of our cloudflared, or None.

    Matched on the running command rather than a pid file: a pid file goes
    stale the moment anything unexpected happens (crash, manual kill) and
    then the toggle starts lying about which state it's in.
    """
    port = port or current_port()
    try:
        out = subprocess.run(["pgrep", "-f", _match(port)],
                             capture_output=True, text=True).stdout.strip()
        return int(out.split("\n")[0]) if out else None
    except Exception:
        return None


def _any_pid():
    """Any cloudflared quick tunnel we started, whatever port. Lets the UI
    notice a tunnel left running on a port you've since changed away from."""
    try:
        out = subprocess.run(["pgrep", "-f", "cloudflared tunnel --url"],
                             capture_output=True, text=True).stdout.strip()
        return int(out.split("\n")[0]) if out else None
    except Exception:
        return None


def _which(prog):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, prog)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _notify(title, body=""):
    try:
        subprocess.run(["notify-send", title, body],
                       capture_output=True, timeout=3)
    except Exception:
        pass


def _url_from_log():
    try:
        with open(LOG_FILE) as f:
            found = URL_RE.findall(f.read())
        return found[-1] if found else None
    except Exception:
        return None


def _write(url):
    with open(TUNNEL_FILE, "w") as f:
        json.dump({"url": url}, f)
        f.write("\n")


def stop(quiet=False):
    pid = _pid() or _any_pid()
    if pid:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    _write("")
    if not quiet:
        print("tunnel stopped")
        _notify("Tunnel down")
    return None


def start(port=None, timeout=30, quiet=False):
    port = int(port or current_port())
    set_port(port)

    cf = _which("cloudflared")
    if not cf:
        if not quiet:
            print("cloudflared not found on PATH", file=sys.stderr)
            _notify("Tunnel failed", "cloudflared not found")
        return None

    with open(LOG_FILE, "w") as log:
        subprocess.Popen([cf, "tunnel", "--url", f"http://127.0.0.1:{port}"],
                         stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL,
                         start_new_session=True)   # outlives its parent

    deadline = time.time() + timeout
    while time.time() < deadline:
        url = _url_from_log()
        if url:
            _write(url)
            if not quiet:
                print(url)
                try:
                    subprocess.run(["wl-copy"], input=url.encode(), timeout=3)
                except Exception:
                    pass
                _notify("Tunnel up", url)
            return url
        time.sleep(0.5)

    stop(quiet=True)
    if not quiet:
        print(f"no URL after {timeout}s — see {LOG_FILE}", file=sys.stderr)
        _notify("Tunnel failed", f"no URL after {timeout}s")
    return None


def toggle(port=None):
    pid = _pid(port)
    saved = tunnel_base(fallback="")

    if pid and not saved:
        # alive but the file lost its URL — repair rather than toggle, so a
        # half-broken state costs one press instead of two
        url = _url_from_log()
        if url:
            _write(url)
            _notify("Tunnel URL restored", url)
            return url
        stop()          # alive with no URL anywhere: it's useless
        return None
    if pid:
        return stop()
    return start(port)


# ── the popup ────────────────────────────────────────────────────────────

STYLE = """
QWidget#panel{background:rgba(10,10,12,0.96);
    border:1px solid rgba(160,255,176,0.25);}
QLabel#title{color:#fff;font-size:15px;font-family:monospace;
    letter-spacing:4px;}
QLabel#lbl{color:rgba(255,255,255,0.45);font-size:10px;
    font-family:monospace;letter-spacing:2px;}
QLabel#url{font-size:11px;font-family:monospace;}
QLineEdit#port{background:transparent;color:#a0ffb0;border:none;
    border-bottom:1px solid rgba(160,255,176,0.35);
    font-size:24px;font-family:monospace;letter-spacing:3px;padding:4px 2px;}
QLineEdit#port:focus{border-bottom:1px solid rgba(160,255,176,0.9);}
QPushButton#recent{background:transparent;color:rgba(255,255,255,0.45);
    border:1px solid rgba(255,255,255,0.15);font-size:12px;
    font-family:monospace;letter-spacing:2px;padding:5px 12px;}
QPushButton#recent:hover{color:#a0ffb0;
    border:1px solid rgba(160,255,176,0.5);}
QPushButton#go{background:transparent;border:1px solid rgba(160,255,176,0.4);
    color:#a0ffb0;font-weight:bold;font-size:12px;font-family:monospace;
    letter-spacing:3px;padding:8px 20px;}
QPushButton#go:hover{background:rgba(160,255,176,0.10);}
QPushButton#kill{background:transparent;border:1px solid rgba(255,120,120,0.35);
    color:#ff8888;font-weight:bold;font-size:12px;font-family:monospace;
    letter-spacing:3px;padding:8px 20px;}
QPushButton#kill:hover{background:rgba(255,120,120,0.10);}
QPushButton#copy,QPushButton#close{background:transparent;border:none;
    color:rgba(255,255,255,0.35);font-size:12px;font-family:monospace;
    letter-spacing:2px;padding:8px 10px;}
QPushButton#copy:hover,QPushButton#close:hover{color:#fff;}
"""


def popup():
    """The whole thing in one window: port, recents, status, start/stop."""
    from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QPushButton)
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtGui import QIntValidator

    class Starter(QThread):
        """cloudflared takes a few seconds to hand back a URL — off the UI
        thread so the window doesn't freeze while it waits."""
        done = pyqtSignal(str)

        def __init__(self, port):
            super().__init__()
            self.port = port

        def run(self):
            self.done.emit(start(self.port, quiet=True) or "")

    class Panel(QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("panel")
            self.setWindowTitle("Tunnel")
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                                Qt.WindowType.Window)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setStyleSheet(STYLE)
            self.setFixedWidth(340)
            self._thread = None
            self._drag = None
            self._close_after = False

            lay = QVBoxLayout(self)
            lay.setContentsMargins(26, 22, 26, 20)
            lay.setSpacing(14)

            top = QHBoxLayout()
            t = QLabel("// TUNNEL"); t.setObjectName("title")
            top.addWidget(t); top.addStretch(1)
            x = QPushButton("✕"); x.setObjectName("close")
            x.setCursor(Qt.CursorShape.PointingHandCursor)
            x.clicked.connect(self.close)
            top.addWidget(x)
            lay.addLayout(top)

            pl = QLabel("PORT"); pl.setObjectName("lbl"); lay.addWidget(pl)
            self.field = QLineEdit(str(current_port()))
            self.field.setObjectName("port")
            self.field.setValidator(QIntValidator(1, 65535, self))
            self.field.returnPressed.connect(self._enter)
            lay.addWidget(self.field)

            rec = recent_ports()
            if rec:
                rl = QLabel("RECENT"); rl.setObjectName("lbl"); lay.addWidget(rl)
                row = QHBoxLayout(); row.setSpacing(8)
                for p in rec:
                    b = QPushButton(str(p)); b.setObjectName("recent")
                    b.setCursor(Qt.CursorShape.PointingHandCursor)
                    # fills the field rather than starting outright, so a
                    # misclick on a recent port costs nothing
                    b.clicked.connect(lambda _, v=p: self._fill(v))
                    row.addWidget(b)
                row.addStretch(1)
                lay.addLayout(row)

            self.status = QLabel(); self.status.setObjectName("url")
            self.status.setWordWrap(True)
            self.status.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(self.status)

            btns = QHBoxLayout(); btns.setSpacing(6)
            self.copy = QPushButton("copy"); self.copy.setObjectName("copy")
            self.copy.setCursor(Qt.CursorShape.PointingHandCursor)
            self.copy.clicked.connect(self._copy)
            btns.addWidget(self.copy); btns.addStretch(1)
            self.go = QPushButton("START"); self.go.setObjectName("go")
            self.go.setCursor(Qt.CursorShape.PointingHandCursor)
            self.go.clicked.connect(self._go)
            btns.addWidget(self.go)
            lay.addLayout(btns)

            self._refresh()
            self.field.setFocus(); self.field.selectAll()

        # dragging, since the window is frameless
        def mousePressEvent(self, e):
            if e.button() == Qt.MouseButton.LeftButton:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, e):
            if self._drag and e.buttons() & Qt.MouseButton.LeftButton:
                self.move(e.globalPosition().toPoint() - self._drag)

        def mouseReleaseEvent(self, e):
            self._drag = None

        def keyPressEvent(self, e):
            if e.key() == Qt.Key.Key_Escape:
                self.close()

        def _fill(self, port):
            self.field.setText(str(port))
            self.field.setFocus(); self.field.selectAll()

        def _port(self):
            try:
                p = int(self.field.text().strip())
            except ValueError:
                return None
            return p if 1 <= p <= 65535 else None

        def _refresh(self):
            up = _pid() is not None
            url = tunnel_base(fallback="")
            if up and url:
                self.status.setText(url)
                self.status.setStyleSheet("color:#a0ffb0;")
                self.go.setText("STOP"); self.go.setObjectName("kill")
                self.copy.setVisible(True)
            elif up:
                self.status.setText("running, no URL yet")
                self.status.setStyleSheet("color:rgba(255,255,255,0.45);")
                self.go.setText("STOP"); self.go.setObjectName("kill")
                self.copy.setVisible(False)
            else:
                self.status.setText("offline")
                self.status.setStyleSheet("color:rgba(255,255,255,0.35);")
                self.go.setText("START"); self.go.setObjectName("go")
                self.copy.setVisible(False)
            self.go.setStyleSheet("")      # force the new objectName to apply
            self.setStyleSheet(STYLE)

        def _copy(self):
            url = tunnel_base(fallback="")
            if url:
                try:
                    subprocess.run(["wl-copy"], input=url.encode(), timeout=3)
                except Exception:
                    pass

        def _enter(self):
            """Enter means 'do it and get out of my way'.

            The window is HIDDEN rather than closed, because starting runs
            on a worker thread and the URL is only written once cloudflared
            answers — closing outright would kill the app before that and
            leave tunnel.json empty. It quits itself in _started instead.
            """
            if _pid() is not None:
                self._go()            # already up: Enter just stops it
                return
            if self._port() is None:
                self.field.setFocus(); self.field.selectAll()
                return
            self._close_after = True
            self._go()
            self.hide()

        def _go(self):
            if _pid() is not None:
                stop(quiet=True)
                _notify("Tunnel down")
                self._refresh()
                return

            port = self._port()
            if port is None:
                # nothing valid typed — flash the field rather than starting
                # on a blank value and silently using the old port
                self.field.setFocus(); self.field.selectAll()
                return

            self.go.setEnabled(False)
            self.status.setText("starting…")
            self.status.setStyleSheet("color:rgba(255,255,255,0.45);")

            self._thread = Starter(port)
            self._thread.done.connect(self._started)
            self._thread.start()

        def _started(self, url):
            self.go.setEnabled(True)
            if url:
                try:
                    subprocess.run(["wl-copy"], input=url.encode(), timeout=3)
                except Exception:
                    pass
                _notify("Tunnel up", url)
            else:
                _notify("Tunnel failed", "no URL — see tunnel.log")
            if self._close_after:
                # started via Enter: the notification is the result, so
                # there's nothing left to show
                QApplication.instance().quit()
                return
            self._refresh()

    app = QApplication.instance() or QApplication(sys.argv)
    w = Panel()
    scr = app.primaryScreen().geometry()
    w.adjustSize()
    w.move(scr.center().x() - w.width() // 2,
           scr.center().y() - w.height() // 2)
    w.show()
    app.exec()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "popup"
    if arg == "start":
        start(sys.argv[2] if len(sys.argv) > 2 else None)
    elif arg == "stop":
        stop()
    elif arg == "toggle":
        toggle()
    elif arg == "status":
        url = tunnel_base(fallback="")
        print(url)
        sys.exit(0 if _pid() else 1)
    else:
        popup()
