"""The research index (ability 68).

One append-only file, written every run: date, asset, strategy text, spec hash,
every metric, artifact paths, and what the run cost. This is the only thing in
the system that compounds, so nothing is ever overwritten or deleted here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ee_agent.paths import research_index_path


@dataclass
class ResearchEntry:
    ts: str
    spec_id: str
    spec_hash: str
    plan_hash: str
    symbol: str
    timeframe: str
    start: str
    end: str
    n_trades: int
    metrics: dict
    walk_forward: dict | None = None
    monte_carlo: dict | None = None
    lookahead: str = ""
    combine_pass_rate: float | None = None
    adversarial_verdict: str = ""
    survived: bool | None = None
    cost_usd: float = 0.0
    artifact_dir: str = ""
    data_quality: float = 1.0
    strategy_text: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__


def append_run(report) -> ResearchEntry:
    entry = ResearchEntry(
        ts=datetime.now(timezone.utc).isoformat(),
        spec_id=report.spec.id,
        spec_hash=report.spec.hash,
        plan_hash=report.base.plan_hash,
        symbol=report.base.symbol,
        timeframe=report.base.timeframe,
        start=report.base.start,
        end=report.base.end,
        n_trades=report.base.metrics.n_trades,
        metrics=report.base.metrics.to_dict(),
        walk_forward=(
            {
                "in_sample": report.walk_forward.in_sample.to_dict() if report.walk_forward.in_sample else None,
                "out_of_sample": (
                    report.walk_forward.out_of_sample.to_dict() if report.walk_forward.out_of_sample else None
                ),
                "oos_positive_windows": report.walk_forward.oos_positive_windows,
            }
            if report.walk_forward
            else None
        ),
        monte_carlo=report.monte_carlo.to_dict() if report.monte_carlo else None,
        lookahead=report.lookahead.verdict if report.lookahead else "",
        combine_pass_rate=report.combine.pass_rate if report.combine else None,
        adversarial_verdict=report.adversarial.verdict if report.adversarial else "",
        survived=report.adversarial.survived if report.adversarial else None,
        cost_usd=report.cost_usd,
        artifact_dir=report.artifact_dir,
        data_quality=report.base.data_quality,
        strategy_text=report.spec.describe(),
    )
    append_entry(entry)
    return entry


def append_entry(entry: ResearchEntry, path: Path | None = None) -> None:
    p = path or research_index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), default=str) + "\n")


def read_index(path: Path | None = None) -> Iterator[dict]:
    p = path or research_index_path()
    if not p.exists():
        return iter(())
    def gen():
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
    return gen()


def search(spec_id: str | None = None, symbol: str | None = None, survived: bool | None = None) -> list[dict]:
    out = []
    for row in read_index():
        if spec_id and row.get("spec_id") != spec_id:
            continue
        if symbol and row.get("symbol") != symbol:
            continue
        if survived is not None and row.get("survived") is not survived:
            continue
        out.append(row)
    return out


def summary(limit: int = 20) -> str:
    rows = list(read_index())
    if not rows:
        return "[research] index is empty. Every analyze run appends to it."
    lines = [f"[research] {len(rows)} run(s) recorded. Most recent {min(limit, len(rows))}:"]
    total_cost = sum(r.get("cost_usd", 0.0) for r in rows)
    for row in rows[-limit:]:
        m = row.get("metrics", {})
        lines.append(
            f"  {row['ts'][:19]}  {row['spec_id']:<22} {row['symbol']:<8} "
            f"trades {m.get('n_trades', 0):>4}  net {m.get('net_pnl', 0):>12,.0f}  "
            f"{'survived' if row.get('survived') else 'failed  '}  ${row.get('cost_usd', 0):.4f}"
        )
    lines.append(f"  total recorded spend: ${total_cost:,.4f}")
    return "\n".join(lines)
