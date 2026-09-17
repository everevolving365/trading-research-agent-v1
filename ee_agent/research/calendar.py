"""Event and calendar awareness (ability 65).

Economic releases, FOMC, earnings, rollovers, holiday sessions. Two uses:
blackout windows that keep a strategy out of a scheduled event, and regime
tagging so the regime decomposition can separate news days from quiet ones.

The shipped calendar is a static data file of scheduled, publicly known events.
It is deliberately explicit about what it does NOT know: an unscheduled event is
not in here, and the agent says so rather than implying full coverage.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

CALENDAR_FILE = Path(__file__).with_name("calendar.json")


@dataclass
class Event:
    date: str
    time_local: str
    tz: str
    name: str
    kind: str  # fomc | cpi | nfp | pce | earnings | rollover | holiday | half_day
    impact: str = "high"  # high | medium | low
    blackout_before_min: int = 15
    blackout_after_min: int = 30
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def window(self) -> tuple[datetime, datetime]:
        stamp = datetime.fromisoformat(f"{self.date}T{self.time_local}")
        return (
            stamp - timedelta(minutes=self.blackout_before_min),
            stamp + timedelta(minutes=self.blackout_after_min),
        )


class EventCalendar:
    def __init__(self, path: Path | None = None):
        self.path = path or CALENDAR_FILE
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.events = [Event(**e) for e in raw.get("events", [])]
        self.coverage_note = raw.get(
            "coverage_note",
            "Scheduled events only. Unscheduled news is not in here and cannot be.",
        )

    # ------------------------------------------------------------- queries
    def on(self, day: date | str) -> list[Event]:
        key = day if isinstance(day, str) else day.isoformat()
        return [e for e in self.events if e.date == key]

    def event_days(self, kinds: list[str] | None = None) -> set[str]:
        return {e.date for e in self.events if not kinds or e.kind in kinds}

    def is_blackout(self, when: datetime, kinds: list[str] | None = None) -> tuple[bool, str]:
        for event in self.events:
            if kinds and event.kind not in kinds:
                continue
            start, end = event.window()
            naive = when.replace(tzinfo=None)
            if start <= naive <= end:
                return True, (
                    f"{event.name} at {event.time_local} {event.tz} "
                    f"(blackout {event.blackout_before_min}m before to {event.blackout_after_min}m after)"
                )
        return False, ""

    def blackout_mask(self, bars, kinds: list[str] | None = None):
        """A per-bar mask the engine and the live path both use."""
        import numpy as np

        mask = np.zeros(len(bars), dtype=bool)
        local = bars.df["ts"].dt.tz_convert(bars.tz)
        by_day: dict[str, list[Event]] = {}
        for event in self.events:
            if kinds and event.kind not in kinds:
                continue
            by_day.setdefault(event.date, []).append(event)
        for i in range(len(bars)):
            day = str(bars.local_date[i])
            if day not in by_day:
                continue
            stamp = local.iloc[i].replace(tzinfo=None)
            for event in by_day[day]:
                start, end = event.window()
                if start <= stamp <= end:
                    mask[i] = True
                    break
        return mask

    def tag_days(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for event in self.events:
            out.setdefault(event.date, []).append(event.kind)
        return out

    def next_events(self, after: date | None = None, limit: int = 5) -> list[Event]:
        after = after or date.today()
        upcoming = [e for e in self.events if date.fromisoformat(e.date) >= after]
        return sorted(upcoming, key=lambda e: (e.date, e.time_local))[:limit]

    def rollover_days(self, symbol: str, start: date, end: date) -> list[date]:
        """Futures rollovers, derived from the instrument registry, not a list."""
        from ee_agent.instruments.registry import get_instrument

        instrument = get_instrument(symbol)
        out: list[date] = []
        cursor = start
        for _ in range(40):
            nxt = instrument.next_rollover(cursor)
            if not nxt or nxt > end:
                break
            out.append(nxt)
            cursor = nxt + timedelta(days=1)
        return out

    def summary(self, limit: int = 6) -> str:
        upcoming = self.next_events(limit=limit)
        lines = [f"[calendar] {len(self.events)} scheduled event(s) on file."]
        for e in upcoming:
            lines.append(f"    {e.date} {e.time_local} {e.tz:<18} {e.name} ({e.impact} impact)")
        lines.append(f"    {self.coverage_note}")
        return "\n".join(lines)
