"""The installable desktop app: icon, launcher, lifetime, and the installers."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ icon
def test_the_icon_is_a_valid_png_and_ico(tmp_path):
    from ee_agent.ui.icon import SIZES, ico_bytes, png_bytes, write_icons

    png = png_bytes(64)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (64, 64)

    ico = ico_bytes()
    reserved, kind, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind, count) == (0, 1, len(SIZES))
    for k in range(count):
        dim, _dim2, _c, _r, _planes, _bpp, size, offset = struct.unpack("<BBBBHHII", ico[6 + 16 * k: 22 + 16 * k])
        assert ico[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + size <= len(ico)
    paths = write_icons(tmp_path)
    assert paths["ico"].stat().st_size > 1000 and paths["png"].exists()


# -------------------------------------------------------------- browsers
def test_app_windows_prefer_edge_then_chrome_on_windows(tmp_path):
    from ee_agent.desktop import browser_candidates, find_browser

    env = {"ProgramFiles(x86)": str(tmp_path / "x86"), "ProgramFiles": str(tmp_path / "pf"), "LOCALAPPDATA": str(tmp_path / "la")}
    candidates = browser_candidates("Windows", env)
    assert candidates[0].endswith("msedge.exe")
    assert find_browser("Windows", env) is None
    chrome = tmp_path / "pf" / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    assert find_browser("Windows", env) == str(chrome)


def test_the_window_is_an_app_window_with_its_own_profile(tmp_path):
    from ee_agent.desktop import window_command

    cmd = window_command("msedge.exe", "http://127.0.0.1:9/?token=t", tmp_path)
    assert "--app=http://127.0.0.1:9/?token=t" in cmd
    assert f"--user-data-dir={tmp_path}" in cmd


# ------------------------------------------------------- server lifetime
@pytest.fixture
def app_server():
    import threading

    from ee_agent.ui import server as ui

    httpd = ui.serve("127.0.0.1", 0, open_browser=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    ui.STATE.last_heartbeat = None
    ui.STATE.quit_requested = False
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    ui.STATE.last_heartbeat = None
    ui.STATE.quit_requested = False


def _call(port, path, token=None, method="GET", host=None):
    from ee_agent.ui import server as ui

    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"{}" if method == "POST" else None)
    req.add_header("X-EE-Token", ui.TOKEN if token is None else token)
    req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def test_ping_and_heartbeat_need_the_token(app_server):
    from ee_agent.ui import server as ui

    port = app_server.server_port
    assert _call(port, "/api/ping")[0] == 200
    assert _call(port, "/api/ping", token="wrong")[0] == 403
    assert _call(port, "/api/heartbeat", method="POST")[0] == 200
    assert ui.STATE.last_heartbeat is not None


def test_a_foreign_host_header_is_refused(app_server):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 must not
    reach the app, even before it would need the token."""
    port = app_server.server_port
    status, _ = _call(port, "/api/ping", host="evil.example")
    assert status == 403


def test_quit_stops_the_app(app_server):
    from ee_agent.desktop import Watchdog
    from ee_agent.ui import server as ui

    assert _call(app_server.server_port, "/api/quit", method="POST")[0] == 200
    assert ui.STATE.quit_requested
    assert Watchdog(app_server).should_stop()


def test_the_watchdog_waits_for_the_window_then_follows_heartbeats(app_server):
    from ee_agent.desktop import Watchdog
    from ee_agent.ui import server as ui

    now = [1000.0]
    dog = Watchdog(app_server, timeout=25, grace=90, clock=lambda: now[0])
    assert not dog.should_stop()          # still loading
    now[0] += 91
    assert dog.should_stop()              # the window never arrived
    ui.STATE.last_heartbeat = now[0]
    assert not dog.should_stop()          # the window is alive
    now[0] += 26
    assert dog.should_stop()              # the window closed


def test_a_second_launch_finds_the_running_app(app_server, tmp_path, monkeypatch):
    import ee_agent.desktop as desktop
    from ee_agent.ui import server as ui

    monkeypatch.setattr(desktop, "data_dir", lambda: tmp_path)
    assert desktop.already_running() is None
    (tmp_path / "app.json").write_text(
        json.dumps({"pid": 1, "port": app_server.server_port, "token": ui.TOKEN}), encoding="utf-8"
    )
    running = desktop.already_running()
    assert running is not None and running.port == app_server.server_port
    (tmp_path / "app.json").write_text(json.dumps({"pid": 1, "port": 9, "token": "x"}), encoding="utf-8")
    assert desktop.already_running() is None  # a stale file is not a running app


# -------------------------------------------------------------- data dir
@pytest.mark.parametrize(
    "system, env, expected",
    [
        ("Windows", {"USERPROFILE": "C:/Users/a"}, "C:/Users/a/EverEvolving"),
        ("Darwin", {}, "~/Library/Application Support/EverEvolving"),
        ("Linux", {"XDG_DATA_HOME": "/x/share"}, "/x/share/everevolving"),
        ("Linux", {}, "~/.local/share/everevolving"),
    ],
)
def test_the_data_folder_is_per_user(system, env, expected):
    from ee_agent.paths import user_data_dir

    got = user_data_dir(system=system, env=env)
    assert got == Path(expected).expanduser()


def test_the_repository_is_never_the_default_data_folder(monkeypatch):
    import ee_agent.paths as paths

    monkeypatch.delenv("EE_HOME", raising=False)
    assert paths.ee_home() != paths.REPO_ROOT


# ------------------------------------------------------------- installers
def test_the_windows_installer_does_what_it_says():
    ps1 = (REPO / "install/windows/install.ps1").read_text(encoding="utf-8")
    for needed in ("USERPROFILE", "-m\", \"venv", "WScript.Shell", "GetFolderPath(\"Desktop\")",
                   "ee_agent.ui.icon", "ee-agent-desktop.exe"):
        assert needed in ps1, needed
    for forbidden in ("Read-Host", "Get-Credential", "ConvertTo-SecureString", "RunAs"):
        assert forbidden.lower() not in ps1.lower(), forbidden
    bat = (REPO / "Install.bat").read_text(encoding="utf-8")
    assert "install\\windows\\install.ps1" in bat


def test_the_installers_are_plain_ascii():
    for rel in ("install/windows/install.ps1", "install/windows/uninstall.ps1",
                "install/windows/bootstrap.ps1", "Install.bat", "install.sh", "Install.command"):
        data = (REPO / rel).read_bytes()
        assert all(b < 128 for b in data), f"{rel} has non-ASCII bytes (Windows PowerShell 5.1 misreads them)"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_the_unix_installer_parses():
    result = subprocess.run(["bash", "-n", str(REPO / "install.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_desktop_launcher_is_a_windowed_entry_point():
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["gui-scripts"]["ee-agent-desktop"] == "ee_agent.desktop:main"


def test_claude_is_told_how_to_install_it():
    notes = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "install\\windows\\install.ps1" in notes and "bash install.sh" in notes
    assert "Never" in notes and "keys" in notes
