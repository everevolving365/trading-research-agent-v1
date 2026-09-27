"""Canonical filesystem locations. Everything writable lives under EE_HOME."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def user_data_dir(system: str | None = None, env: dict | None = None) -> Path:
    """The per-user folder the app writes to: ledgers, research index, cache,
    settings. Never the program's own folder -- a copy of the repository must
    not carry one person's history to the next (D-029)."""
    import platform

    system = system or platform.system()
    env = env if env is not None else dict(os.environ)
    if system == "Windows":
        # Not %LOCALAPPDATA%: when the app is installed or run from inside a
        # packaged (MSIX) app -- the Claude desktop app is one -- Windows silently
        # redirects writes under AppData into that app's private sandbox, and the
        # Desktop icon would point at a folder nobody else can see (D-030).
        base = env.get("USERPROFILE") or str(Path.home())
        return Path(base) / "EverEvolving"
    if system == "Darwin":
        return Path("~/Library/Application Support/EverEvolving").expanduser()
    base = env.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "everevolving"


def ee_home() -> Path:
    raw = os.environ.get("EE_HOME")
    return Path(raw).expanduser().resolve() if raw else user_data_dir()


def runs_dir() -> Path:
    return _mk(ee_home() / "runs")


def artifacts_dir() -> Path:
    return _mk(ee_home() / "artifacts")


def cache_dir() -> Path:
    return _mk(ee_home() / "data" / "cache")


def fixtures_dir() -> Path:
    return REPO_ROOT / "tests" / "fixtures"


def library_dir() -> Path:
    return REPO_ROOT / "library"


def research_index_path() -> Path:
    return ee_home() / "research-index.jsonl"


def signal_ledger_path() -> Path:
    return ee_home() / "signal-ledger.jsonl"


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
