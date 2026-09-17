"""Canonical filesystem locations. Everything writable lives under EE_HOME."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def ee_home() -> Path:
    raw = os.environ.get("EE_HOME")
    return Path(raw).expanduser().resolve() if raw else REPO_ROOT


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
