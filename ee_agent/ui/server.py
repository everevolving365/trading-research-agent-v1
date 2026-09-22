"""The desktop app.

"Extremely user friendly, while still being maximally capable." A terminal is
not that for a client who does not live in one, so this is a real window: you
double-click, a browser opens on a local page, and you talk to the agent.

Deliberately built on ``http.server`` from the standard library. No Flask, no
FastAPI, no node, no build step. A client who can run Python can run this, and
the zero-cost floor stays intact -- the app works with no keys at all, exactly
like the CLI.

Security: it binds to 127.0.0.1 only, never a public interface, and every
request must carry a session token minted at startup. That stops a web page you
happen to have open from talking to your trading agent.
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ee_agent.conversation import tools as toolbox
from ee_agent.conversation.agent import Conversation
from ee_agent.conversation.model import get_model
from ee_agent.cost.notifier import CeilingReached, ledger

HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)


@dataclass
class AppState:
    """One session's worth of state, shared by every request."""

    conversation: Conversation | None = None
    activity: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    voice_enabled: bool = False
    speaker: Any = None

    def ensure(self, provider: str | None = None) -> Conversation:
        if self.conversation is None:
            self.conversation = Conversation(model=get_model(provider), sink=lambda _m: None)
        return self.conversation

    def note(self, kind: str, text: str, detail: dict | None = None) -> None:
        self.activity.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "text": text,
                "detail": detail or {},
            }
        )
        del self.activity[:-200]


STATE = AppState()


class Handler(BaseHTTPRequestHandler):
    server_version = "ee-agent"

    # ----------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):  # quiet: the app window is the UI
        pass

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        """Local-only plus a session token: a page you have open in another tab
        must not be able to drive your trading agent."""
        if self.headers.get("X-EE-Token") == TOKEN:
            return True
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # --------------------------------------------------------------- GET
    def do_GET(self) -> None:
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            page = (HERE / "index.html").read_text(encoding="utf-8")
            page = page.replace("{{TOKEN}}", TOKEN)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        if path.startswith("/api/"):
            if not self._authorised():
                return self._send(403, {"error": "bad or missing session token"})
            return self._api_get(path)

        asset = (HERE / path.lstrip("/")).resolve()
        if asset.is_file() and HERE in asset.parents:
            kind = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            return self._send(200, asset.read_bytes(), kind)
        self._send(404, {"error": "not found"})

    def _api_get(self, path: str) -> None:
        if path == "/api/status":
            return self._send(200, _status())
        if path == "/api/activity":
            return self._send(200, {"activity": STATE.activity[-80:]})
        if path == "/api/spend":
            book = ledger()
            return self._send(
                200,
                {
                    "total_usd": book.total_usd,
                    "ceiling_usd": book.ceiling_usd,
                    "banner": book.banner(),
                    "by_operation": {
                        op: round(sum(r.actual_usd for r in book.records if r.operation == op), 6)
                        for op in sorted({r.operation for r in book.records})
                    },
                },
            )
        if path == "/api/tools":
            return self._send(200, {"tools": [t.schema() for t in toolbox.REGISTRY.values()]})
        self._send(404, {"error": f"no endpoint {path}"})

    # -------------------------------------------------------------- POST
    def do_POST(self) -> None:
        from urllib.parse import urlparse

        if not self._authorised():
            return self._send(403, {"error": "bad or missing session token"})
        path = urlparse(self.path).path
        payload = self._body()

        if path == "/api/chat":
            return self._chat(payload)
        if path == "/api/tool":
            return self._tool(payload)
        if path == "/api/voice":
            return self._voice(payload)
        if path == "/api/reset":
            with STATE.lock:
                STATE.conversation = None
                STATE.activity.clear()
                toolbox.reset_workspace()
            return self._send(200, {"ok": True})
        self._send(404, {"error": f"no endpoint {path}"})

    def _chat(self, payload: dict) -> None:
        message = (payload.get("message") or "").strip()
        if not message:
            return self._send(400, {"error": "say something"})
        with STATE.lock:
            conversation = STATE.ensure(payload.get("provider"))
            STATE.note("client", message)
            used: list[str] = []

            def on_tool(name: str, arguments: dict) -> None:
                used.append(name)
                STATE.note("tool", name, {"arguments": arguments})

            spend_before = ledger().total_usd
            try:
                reply = conversation.say(message, on_tool=on_tool)
            except CeilingReached as exc:
                STATE.note("ceiling", str(exc))
                return self._send(200, {"reply": str(exc), "ceiling": True, "tools": used})
            cost = ledger().total_usd - spend_before
            STATE.note("agent", reply, {"tools": used, "cost_usd": round(cost, 6)})

        if STATE.voice_enabled and STATE.speaker is not None:
            try:
                STATE.speaker.say(reply)
            except Exception:
                pass

        self._send(
            200,
            {
                "reply": reply,
                "tools": used,
                "cost_usd": round(cost, 6),
                "spend": ledger().total_usd,
                "workspace": toolbox.WORKSPACE.summary(),
            },
        )

    def _tool(self, payload: dict) -> None:
        name = payload.get("name") or ""
        arguments = payload.get("arguments") or {}
        if name not in toolbox.REGISTRY:
            return self._send(400, {"error": f"no tool {name!r}", "available": sorted(toolbox.REGISTRY)})
        with STATE.lock:
            STATE.note("tool", name, {"arguments": arguments, "direct": True})
            raw = toolbox.call(name, arguments)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"raw": raw}
        self._send(200, {"tool": name, "result": result, "workspace": toolbox.WORKSPACE.summary()})

    def _voice(self, payload: dict) -> None:
        """Voice out is local and free. Voice IN needs local Whisper; when it is
        not installed this says so rather than pretending."""
        from ee_agent.capture.voice import Speaker, status as voice_status

        action = payload.get("action", "status")
        if action == "status":
            return self._send(200, {"status": voice_status().summary(), "enabled": STATE.voice_enabled})
        if action == "enable":
            state = voice_status()
            if not state.output_available:
                return self._send(
                    200,
                    {
                        "enabled": False,
                        "status": state.summary(),
                        "note": "No local text-to-speech is available on this machine. "
                        "Everything still works -- voice is a shell, never a dependency.",
                    },
                )
            if STATE.speaker is None:
                STATE.speaker = Speaker(enabled=True)
            STATE.voice_enabled = True
            return self._send(200, {"enabled": True, "status": state.summary()})
        if action == "disable":
            STATE.voice_enabled = False
            if STATE.speaker is not None:
                STATE.speaker.interrupt()
            return self._send(200, {"enabled": False})
        if action == "interrupt":
            if STATE.speaker is not None:
                STATE.speaker.interrupt()
            return self._send(200, {"ok": True})
        if action == "listen":
            return self._listen()
        self._send(400, {"error": f"unknown voice action {action!r}"})

    def _listen(self) -> None:
        from ee_agent.capture.voice import Listener, status as voice_status

        state = voice_status()
        if not state.input_available:
            return self._send(
                200,
                {
                    "text": "",
                    "error": "Local speech input is not installed. "
                    "`pip install faster-whisper sounddevice` turns it on -- it runs on your machine, "
                    "costs nothing, and sends no audio anywhere. Typing works identically.",
                },
            )
        try:
            text = Listener().listen(seconds=float(self.headers.get("X-EE-Seconds") or 8))
            return self._send(200, {"text": text})
        except Exception as exc:  # pragma: no cover - needs a microphone
            return self._send(200, {"text": "", "error": f"{type(exc).__name__}: {exc}"})


def _status() -> dict:
    from ee_agent.capture.voice import status as voice_status
    from ee_agent.conversation.model import get_model
    from ee_agent.execution.ledger import ledger as position_ledger
    from ee_agent.library import loader
    from ee_agent.secrets.vault import vault

    model = get_model()
    secrets_present = [s.name for s in vault().describe() if s.present]
    return {
        "model": model.name,
        "model_ready": model.name != "none",
        "keys_present": secrets_present,
        "keys_missing": [s.name for s in vault().describe() if not s.present],
        "voice": voice_status().summary(),
        "voice_in": voice_status().input_available,
        "voice_out": voice_status().output_available,
        "positions": position_ledger().summary(),
        "workspace": toolbox.WORKSPACE.summary(),
        "spend": ledger().total_usd,
        "library": [e.id for e in loader.entries() if e.has_spec],
        "n_tools": len(toolbox.REGISTRY),
    }


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> ThreadingHTTPServer:
    """Start the app. Localhost only -- never bind this to a public interface."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing to bind to {host!r}. This app has your keychain and your broker adapters "
            "behind it; it listens on localhost only."
        )
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{httpd.server_port}/?token={TOKEN}"
    print(f"\n  EverEvolving Trading Agent -- desktop app")
    print(f"  {url}")
    print("  (localhost only; the token keeps other pages out. Ctrl+C to stop.)\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return httpd


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    httpd = serve(host, port, open_browser)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        httpd.server_close()
    return 0
