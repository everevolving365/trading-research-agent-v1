"""The cache. Age is always visible; stale data is never silent (ability 21).

Every load prints cache age and last bar timestamp. There is no quiet path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ee_agent.data.bars import Bars
from ee_agent.paths import cache_dir


@dataclass
class CacheEntry:
    path: Path
    meta_path: Path

    @property
    def exists(self) -> bool:
        return self.path.exists() and self.meta_path.exists()

    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}

    @property
    def fetched_at(self) -> datetime | None:
        raw = self.meta().get("fetched_at")
        return datetime.fromisoformat(raw) if raw else None

    @property
    def age_seconds(self) -> float:
        fetched = self.fetched_at
        if not fetched:
            return float("inf")
        return (datetime.now(timezone.utc) - fetched).total_seconds()


class BarCache:
    def __init__(self, root: Path | None = None):
        self.root = root or cache_dir()

    def _entry(self, symbol: str, timeframe: str, source: str) -> CacheEntry:
        base = self.root / symbol.upper()
        stem = f"{symbol.upper()}_{timeframe}_{source.split(':')[0]}"
        return CacheEntry(base / f"{stem}.csv", base / f"{stem}.json")

    def get(self, symbol: str, timeframe: str, source: str, max_age_hours: float | None = None) -> Bars | None:
        entry = self._entry(symbol, timeframe, source)
        if not entry.exists:
            return None
        if max_age_hours is not None and entry.age_seconds > max_age_hours * 3600:
            return None
        meta = entry.meta()
        df = pd.read_csv(entry.path)
        return Bars(
            symbol=symbol.upper(),
            timeframe=timeframe,
            df=df,
            tz=meta.get("tz", "America/Chicago"),
            source=f"{meta.get('source', source)}:cached",
            fetched_at=entry.fetched_at or datetime.now(timezone.utc),
            quality_score=float(meta.get("quality_score", 1.0)),
            quality_notes=list(meta.get("quality_notes", [])),
        )

    def put(self, bars: Bars) -> Path:
        entry = self._entry(bars.symbol, bars.timeframe, bars.source)
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        df = bars.df.copy()
        df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        df.to_csv(entry.path, index=False)
        entry.meta_path.write_text(
            json.dumps(
                {
                    "symbol": bars.symbol,
                    "timeframe": bars.timeframe,
                    "source": bars.source,
                    "tz": bars.tz,
                    "fetched_at": bars.fetched_at.isoformat(),
                    "n_bars": len(bars),
                    "first_bar": bars.first_bar_time.isoformat() if bars.first_bar_time else None,
                    "last_bar": bars.last_bar_time.isoformat() if bars.last_bar_time else None,
                    "content_hash": bars.content_hash,
                    "quality_score": bars.quality_score,
                    "quality_notes": bars.quality_notes,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return entry.path

    def invalidate(self, symbol: str, timeframe: str | None = None) -> int:
        base = self.root / symbol.upper()
        if not base.exists():
            return 0
        removed = 0
        for p in base.glob("*"):
            if timeframe and f"_{timeframe}_" not in p.name:
                continue
            p.unlink()
            removed += 1
        return removed

    def describe(self, symbol: str) -> list[dict]:
        base = self.root / symbol.upper()
        if not base.exists():
            return []
        out = []
        for meta_path in sorted(base.glob("*.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(meta["fetched_at"])
            meta["age_hours"] = round((datetime.now(timezone.utc) - fetched).total_seconds() / 3600, 2)
            out.append(meta)
        return out
