"""Open the agent as a desktop app: its own window, its own icon, no terminal.

The installer's desktop shortcut runs this (``ee-agent-desktop``, a windowed
executable, so no console appears). It:

1. keeps one copy running -- a second double-click opens another window on the
   app that is already running instead of starting a second one;
2. starts the local server on a free port (localhost only, session token);
3. opens it in an app window -- Edge or Chrome in ``--app`` mode with its own
   profile, so it has no address bar or tabs and looks like any other program.
   If neither browser exists it falls back to the default browser;
4. stops by itself when the window closes: the page sends a heartbeat every few
   seconds, and when the heartbeats stop the server shuts down.

Everything the app writes goes to the per-user data folder
(:func:`ee_agent.paths.user_data_dir`), never into the program's own folder.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "EverEvolving Trading Agent"
HEARTBEAT_TIMEOUT = 25.0   # seconds without a heartbeat before the app stops
STARTUP_GRACE = 90.0       # how long the first window gets to load


def data_dir() -> Path:
    from ee_agent.paths import ee_home

    return ee_home()


def _state_file() -> Path:
    return data_dir() / "app.json"


# ------------------------------------------------------------------ browsers
def browser_candidates(system: str | None = None, env: dict | None = None) -> list[str]:
    """Chromium-family browsers that support ``--app`` windows, best first."""
    system = system or platform.system()
    env = env if env is not None else dict(os.environ)
    if system == "Windows":
        roots = [env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 env.get("ProgramFiles", r"C:\Program Files"),
                 env.get("LOCALAPPDATA", "")]
        rel = [r"Microsoft\Edge\Application\msedge.exe",
               r"Google\Chrome\Application\chrome.exe",
               r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        return [str(Path(root) / r) for r in rel for root in roots if root]
    if system == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "microsoft-edge", "microsoft-edge-stable", "brave-browser"]
    return [p for p in (shutil.which(n) for n in names) if p]


def find_browser(system: str | None = None, env: dict | None = None) -> str | None:
    for path in browser_candidates(system, env):
        if Path(path).exists():
            return path
    return None


def window_command(browser: str, url: str, profile: Path) -> list[str]:
    """The command that opens ``url`` as an app window with its own profile."""
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--window-size=1280,860",
        "--class=EverEvolving",
    ]


def open_window(url: str) -> subprocess.Popen | None:
    browser = find_browser()
    if browser is None:
        import webbrowser

        webbrowser.open(url)
        return None
    profile = data_dir() / "window-profile"
    profile.mkdir(parents=True, exist_ok=True)
    flags = 0
    if platform.system() == "Windows":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(window_command(browser, url, profile), creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------ single instance
@dataclass
class Running:
    pid: int
    port: int
    token: str

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"


def already_running() -> Running | None:
    """The app this user already has open, if it is really alive."""
    path = _state_file()
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        running = Running(int(info["pid"]), int(info["port"]), str(info["token"]))
        req = urllib.request.Request(
            f"http://127.0.0.1:{running.port}/api/ping", headers={"X-EE-Token": running.token}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return running
    except Exception:
        pass
    return None


def _free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free local port")


# ------------------------------------------------------------------ lifetime
class Watchdog(threading.Thread):
    """Stops the server when the window has gone away."""

    def __init__(self, server, timeout: float = HEARTBEAT_TIMEOUT, grace: float = STARTUP_GRACE,
                 clock=time.monotonic, interval: float = 1.0):
        super().__init__(daemon=True)
        self.server = server
        self.timeout = timeout
        self.grace = grace
        self.clock = clock
        self.interval = interval
        self.started_at = clock()
        self.stopped = threading.Event()

    def should_stop(self) -> bool:
        from ee_agent.ui import server as ui

        last = ui.STATE.last_heartbeat
        now = self.clock()
        if ui.STATE.quit_requested:
            return True
        if last is None:
            return now - self.started_at > self.grace
        return now - last > self.timeout

    def run(self) -> None:
        while not self.stopped.is_set():
            if self.should_stop():
                self.stopped.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            time.sleep(self.interval)


def _quiet_streams() -> None:
    """A windowed executable has no console: send output to a log file instead
    of letting the first print() fail."""
    if sys.stdout is None or sys.stderr is None:
        log = open(data_dir() / "app.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def main(argv: list[str] | None = None) -> int:
    from ee_agent.ui import server as ui

    data_dir().mkdir(parents=True, exist_ok=True)
    _quiet_streams()

    running = already_running()
    if running is not None:
        open_window(running.url)
        return 0

    port = _free_port()
    httpd = ui.serve("127.0.0.1", port, open_browser=False)
    url = f"http://127.0.0.1:{httpd.server_port}/?token={ui.TOKEN}"
    _state_file().write_text(
        json.dumps({"pid": os.getpid(), "port": httpd.server_port, "token": ui.TOKEN}), encoding="utf-8"
    )
    watchdog = Watchdog(httpd)
    watchdog.start()
    open_window(url)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        try:
            _state_file().unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
