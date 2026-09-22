"""The desktop app.

Runs the real server on a loopback port and drives it over HTTP, so what is
tested is the thing the client actually uses -- not a mocked handler.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ee_agent.conversation import tools as toolbox
from ee_agent.ui import server as ui

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    httpd = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    yield base
    httpd.shutdown()
    httpd.server_close()


def call(base: str, path: str, payload: dict | None = None, token: str | None = ui.TOKEN):
    url = f"{base}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-EE-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body.startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        return exc.code, (json.loads(body) if body.startswith("{") else body)


@pytest.fixture(autouse=True)
def clean():
    toolbox.reset_workspace()
    ui.STATE.conversation = None
    ui.STATE.activity.clear()
    yield
    toolbox.reset_workspace()


# =================================================================== security
def test_api_requires_the_session_token(app):
    status, body = call(app, "/api/status", token=None)
    assert status == 403
    assert "token" in body["error"]


def test_a_wrong_token_is_refused(app):
    status, body = call(app, "/api/status", token="not-the-token")
    assert status == 403


def test_posting_without_a_token_is_refused(app):
    status, _body = call(app, "/api/chat", {"message": "hello"}, token=None)
    assert status == 403


def test_refuses_to_bind_to_a_public_interface():
    """This app has the keychain and the broker adapters behind it."""
    with pytest.raises(ValueError) as exc:
        ui.serve(host="0.0.0.0", open_browser=False)
    assert "localhost only" in str(exc.value)


def test_token_is_not_guessable():
    assert len(ui.TOKEN) >= 20


# ====================================================================== page
def test_the_page_loads_and_carries_the_token(app):
    status, body = call(app, "/", token=None)  # the page itself needs no token
    assert status == 200
    assert "<title>EverEvolving Trading Agent</title>" in body
    assert ui.TOKEN in body, "the page was served without a usable token"
    assert "{{TOKEN}}" not in body, "the token placeholder was not substituted"


def test_the_page_states_who_owns_the_strategy(app):
    _status, body = call(app, "/", token=None)
    assert "You own the strategy and you own the risk" in body


def test_unknown_paths_404(app):
    status, _body = call(app, "/nope", token=None)
    assert status == 404


# ==================================================================== status
def test_status_reports_what_is_set_up(app):
    status, body = call(app, "/api/status")
    assert status == 200
    for key in ("model", "model_ready", "keys_present", "keys_missing", "voice", "n_tools", "workspace"):
        assert key in body
    assert body["n_tools"] >= 18


def test_status_never_leaks_a_secret_value(app, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "super-secret-value-12345")
    _status, body = call(app, "/api/status")
    assert "super-secret-value-12345" not in json.dumps(body)
    assert "POLYGON_API_KEY" in body["keys_present"]


# ====================================================================== chat
def test_chat_answers_with_no_model_key(app):
    status, body = call(app, "/api/chat", {"message": "what do you make of gold?"})
    assert status == 200
    assert "no model key" in body["reply"].lower()
    assert "everything else still works" in body["reply"].lower()


def test_empty_message_is_refused(app):
    status, _body = call(app, "/api/chat", {"message": "   "})
    assert status == 400


def test_chat_appears_in_the_activity_feed(app):
    call(app, "/api/chat", {"message": "hello there"})
    _status, body = call(app, "/api/activity")
    kinds = [a["kind"] for a in body["activity"]]
    assert "client" in kinds and "agent" in kinds
    assert any("hello there" in a["text"] for a in body["activity"])


# ===================================================================== tools
def test_a_tool_runs_through_the_app(app):
    status, body = call(
        app, "/api/tool",
        {"name": "load_data", "arguments": {"symbol": "MNQ", "timeframe": "5m", "fixtures_only": True}},
    )
    assert status == 200
    assert body["result"]["symbol"] == "MNQ"
    assert body["result"]["bars"] > 1000
    assert "MNQ" in body["workspace"]


def test_an_unknown_tool_is_refused_with_the_list(app):
    status, body = call(app, "/api/tool", {"name": "make_me_rich", "arguments": {}})
    assert status == 400
    assert "available" in body


def test_a_failing_tool_reports_rather_than_500s(app):
    status, body = call(app, "/api/tool", {"name": "backtest", "arguments": {}})
    assert status == 200
    assert "error" in body["result"]


def test_the_app_exposes_no_order_placing_tool(app):
    """The same guarantee as the CLI: a conversational surface cannot trade."""
    status, body = call(app, "/api/tools")
    assert status == 200
    names = {t["name"] for t in body["tools"]}
    for forbidden in ("place_order", "submit_order", "send_order", "buy", "sell"):
        assert forbidden not in names
    assert "order_flow" in names  # reading flow is fine; placing orders is not


# ===================================================================== spend
def test_spend_is_always_available(app):
    status, body = call(app, "/api/spend")
    assert status == 200
    assert "total_usd" in body and "banner" in body
    assert body["total_usd"] >= 0


# ===================================================================== voice
def test_voice_status_is_honest(app):
    status, body = call(app, "/api/voice", {"action": "status"})
    assert status == 200
    assert "voice" in body["status"].lower()
    assert body["enabled"] is False


def test_enabling_voice_without_tts_says_so_rather_than_pretending(app, monkeypatch):
    from ee_agent.capture.voice import VoiceStatus

    monkeypatch.setattr(
        "ee_agent.capture.voice.status",
        lambda: VoiceStatus(False, False, "none", "none", []),
    )
    status, body = call(app, "/api/voice", {"action": "enable"})
    assert status == 200
    assert body["enabled"] is False
    assert "never a dependency" in body["note"]


def test_listening_without_whisper_explains_the_fix(app, monkeypatch):
    from ee_agent.capture.voice import VoiceStatus

    monkeypatch.setattr(
        "ee_agent.capture.voice.status",
        lambda: VoiceStatus(False, True, "none", "system", []),
    )
    status, body = call(app, "/api/voice", {"action": "listen"})
    assert status == 200
    assert body["text"] == ""
    assert "faster-whisper" in body["error"]
    assert "sends no audio anywhere" in body["error"]


# ===================================================================== reset
def test_reset_clears_the_session(app):
    call(app, "/api/tool", {"name": "load_data", "arguments": {"symbol": "MNQ", "fixtures_only": True}})
    call(app, "/api/reset", {})
    _status, body = call(app, "/api/status")
    assert "none loaded" in body["workspace"]


# =============================================================== the page css
def test_the_page_works_on_a_narrow_window():
    page = (REPO / "ee_agent/ui/index.html").read_text(encoding="utf-8")
    assert "max-width:700px" in page, "no narrow-window breakpoint"
    assert "@media (prefers-color-scheme: light)" in page, "no light theme"
    assert 'name="viewport"' in page
