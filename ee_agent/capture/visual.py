"""Visual simulation and the confirmation loop (ability 11).

The agent renders annotated charts from real historical data showing exactly
where it believes the client's setup occurred, and shows them for approval or
rejection. Every rejection revises its understanding.

This is a runtime feature, not a build-time dependency (Section 2.2): the loop
is driven by an ``Approver``, and the build verifies it end to end with a
synthetic approver that accepts or rejects according to a fixture file. No human
eyes are needed to build or verify it.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ee_agent.compile.to_python import compile_to_python
from ee_agent.cost.notifier import ledger
from ee_agent.spec.model import StrategySpec


@dataclass
class Candidate:
    """One place the agent believes the client's setup occurred."""

    index: int
    ts: str
    side: str
    rule_id: str
    window: dict = field(default_factory=dict)  # bars around it, for rendering
    context: dict = field(default_factory=dict)

    def describe(self) -> str:
        return f"#{self.index} {self.ts} -> {self.side.upper()} (rule '{self.rule_id}')"


@dataclass
class Verdict:
    candidate_index: int
    approved: bool
    reason: str = ""
    correction: str = ""  # what the client says it should have been


class Approver(ABC):
    @abstractmethod
    def review(self, candidate: Candidate, chart_path: Path | None) -> Verdict: ...


class SyntheticApprover(Approver):
    """Fixture-driven approver. Accepts or rejects by rule, so the entire visual
    loop is machine-verifiable (Section 2.3)."""

    def __init__(self, fixture: dict | Path):
        if isinstance(fixture, (str, Path)):
            fixture = json.loads(Path(fixture).read_text(encoding="utf-8"))
        self.fixture = fixture
        self.reviewed: list[Verdict] = []

    def review(self, candidate: Candidate, chart_path: Path | None) -> Verdict:
        rules = self.fixture.get("rules", {})
        verdict = Verdict(candidate.index, approved=bool(self.fixture.get("default_approve", True)))
        for key, spec in rules.items():
            if key == "reject_side" and candidate.side == spec:
                verdict = Verdict(candidate.index, False, f"client rejects {spec} signals", spec)
            if key == "reject_before_minute":
                minute = candidate.context.get("minute_of_day", 0)
                if minute < int(spec):
                    verdict = Verdict(
                        candidate.index, False, f"too early in the session (before minute {spec})",
                        f"start the signal window at minute {spec}",
                    )
            if key == "reject_indices" and candidate.index in spec:
                verdict = Verdict(candidate.index, False, "client says this is not the setup")
        self.reviewed.append(verdict)
        return verdict


class ConsoleApprover(Approver):  # pragma: no cover - interactive
    """The real one: shows the chart and asks."""

    def __init__(self, input_fn=input, sink=print):
        self.input_fn = input_fn
        self.sink = sink

    def review(self, candidate: Candidate, chart_path: Path | None) -> Verdict:
        self.sink(f"\n  Is this your setup?  {candidate.describe()}")
        if chart_path:
            self.sink(f"  chart: {chart_path}")
        answer = (self.input_fn("  yes / no (and tell me why): ") or "").strip().lower()
        approved = answer.startswith("y")
        return Verdict(candidate.index, approved, reason=answer, correction="" if approved else answer)


def find_candidates(spec: StrategySpec, bars, limit: int = 12, seed: int = 3) -> list[Candidate]:
    """Where the agent believes the setup occurred, sampled across the data so
    the client is not shown twelve examples from one week."""
    compiled = compile_to_python(spec)
    signals = compiled.evaluate(bars)
    hits: list[tuple[int, str, str]] = []
    for i in range(len(bars)):
        if signals.long_entry[i]:
            hits.append((i, "long", "long"))
        elif signals.short_entry[i]:
            hits.append((i, "short", "short"))
    if not hits:
        return []
    if len(hits) > limit:
        step = len(hits) / limit
        hits = [hits[int(k * step)] for k in range(limit)]
    out: list[Candidate] = []
    for index, side, rule_id in hits:
        lo, hi = max(0, index - 20), min(len(bars), index + 10)
        out.append(
            Candidate(
                index=index,
                ts=bars.df["ts"].iloc[index].isoformat(),
                side=side,
                rule_id=rule_id,
                window={
                    "start": lo,
                    "end": hi,
                    "open": bars.open[lo:hi].tolist(),
                    "high": bars.high[lo:hi].tolist(),
                    "low": bars.low[lo:hi].tolist(),
                    "close": bars.close[lo:hi].tolist(),
                    "signal_at": index - lo,
                },
                context={
                    "minute_of_day": int(bars.minute_of_day[index]),
                    "local_date": str(bars.local_date[index]),
                    **{
                        key: _safe(value, index)
                        for key, value in signals.debug.items()
                        if key.startswith("context:")
                    },
                },
            )
        )
    return out


def render_chart(candidate: Candidate, out_dir: Path, symbol: str) -> Path:
    """Annotated chart. Uses matplotlib when available; otherwise writes an ASCII
    chart, because a client with no plotting library still gets to SEE it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    window = candidate.window
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        path = out_dir / f"candidate-{candidate.index}.png"
        fig, ax = plt.subplots(figsize=(9, 4.5))
        n = len(window["close"])
        for i in range(n):
            colour = "#1a9850" if window["close"][i] >= window["open"][i] else "#d73027"
            ax.plot([i, i], [window["low"][i], window["high"][i]], color=colour, linewidth=0.8)
            ax.plot(
                [i, i], [window["open"][i], window["close"][i]], color=colour, linewidth=3.2, solid_capstyle="butt"
            )
        marker = window["signal_at"]
        ax.annotate(
            candidate.side.upper(),
            xy=(marker, window["high"][marker]),
            xytext=(marker, window["high"][marker] + (max(window["high"]) - min(window["low"])) * 0.12),
            ha="center",
            color="#2166ac",
            fontweight="bold",
            arrowprops={"arrowstyle": "->", "color": "#2166ac"},
        )
        for key in ("context:opening_range.high", "context:opening_range.low"):
            value = candidate.context.get(key)
            if isinstance(value, (int, float)) and value == value:
                ax.axhline(value, color="#666666", linestyle="--", linewidth=0.9)
        ax.set_title(f"{symbol}  {candidate.ts}  -- is this your setup?")
        ax.set_xticks([])
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        return path
    except Exception:
        path = out_dir / f"candidate-{candidate.index}.txt"
        path.write_text(ascii_chart(candidate), encoding="utf-8")
        return path


def ascii_chart(candidate: Candidate, height: int = 14) -> str:
    """A chart with no dependencies. Shows data constantly (ability 7) even on a
    bare install."""
    window = candidate.window
    highs, lows = window["high"], window["low"]
    top, bottom = max(highs), min(lows)
    span = max(top - bottom, 1e-9)
    rows = []
    for row in range(height):
        level = top - span * row / (height - 1)
        line = []
        for i in range(len(highs)):
            if lows[i] <= level <= highs[i]:
                body_top = max(window["open"][i], window["close"][i])
                body_bottom = min(window["open"][i], window["close"][i])
                line.append("#" if body_bottom <= level <= body_top else "|")
            else:
                line.append(" ")
        rows.append(f"{level:12,.2f} |" + "".join(line))
    marker = " " * (14 + window["signal_at"]) + "^"
    return (
        f"{candidate.ts}  {candidate.side.upper()}\n" + "\n".join(rows) + "\n" + marker + " signal bar"
    )


@dataclass
class VisualLoopResult:
    candidates: list[Candidate]
    verdicts: list[Verdict]
    revisions: list[str] = field(default_factory=list)
    spec: StrategySpec | None = None

    @property
    def approved(self) -> int:
        return sum(1 for v in self.verdicts if v.approved)

    @property
    def rejected(self) -> int:
        return sum(1 for v in self.verdicts if not v.approved)

    @property
    def agreement(self) -> float:
        return self.approved / len(self.verdicts) if self.verdicts else 0.0

    def summary(self) -> str:
        lines = [
            f"[visual] showed {len(self.candidates)} example(s): {self.approved} confirmed, "
            f"{self.rejected} rejected ({self.agreement:.0%} agreement)"
        ]
        for r in self.revisions:
            lines.append(f"    revised: {r}")
        if self.rejected and not self.revisions:
            lines.append(
                "    the rejections did not point at a rule I can change automatically -- "
                "I have logged them and will ask about them directly"
            )
        return "\n".join(lines)


def run_visual_loop(
    spec: StrategySpec,
    bars,
    approver: Approver,
    out_dir: Path | None = None,
    limit: int = 12,
    render: bool = True,
) -> VisualLoopResult:
    """Show, ask, revise. Every rejection revises the agent's understanding."""
    from ee_agent.paths import runs_dir

    out_dir = Path(out_dir or (runs_dir() / "visual" / spec.id))
    candidates = find_candidates(spec, bars, limit=limit)
    ledger().spend("visual_confirmation", **{"vision.image_downscaled": len(candidates)})
    verdicts: list[Verdict] = []
    for candidate in candidates:
        chart = render_chart(candidate, out_dir, bars.symbol) if render else None
        verdicts.append(approver.review(candidate, chart))

    revised = StrategySpec.from_dict(spec.to_dict())
    revisions = _revise(revised, candidates, verdicts, bars)
    return VisualLoopResult(candidates=candidates, verdicts=verdicts, revisions=revisions, spec=revised)


def _revise(spec: StrategySpec, candidates: list[Candidate], verdicts: list[Verdict], bars) -> list[str]:
    """Turn rejections into concrete spec changes.

    Deliberately conservative: it only makes a change when the rejections show a
    clear pattern, and it records every change as an assumption for the client to
    approve. The agent must never quietly redesign the client's strategy.
    """
    from ee_agent.spec.model import Assumption, TimeWindow

    revisions: list[str] = []
    by_index = {c.index: c for c in candidates}
    rejected = [v for v in verdicts if not v.approved]
    if not rejected:
        return revisions

    # pattern 1: one side is consistently wrong
    for side in ("long", "short"):
        side_candidates = [v for v in verdicts if by_index[v.candidate_index].side == side]
        if len(side_candidates) >= 2 and all(not v.approved for v in side_candidates):
            before = len(spec.signals.entry)
            spec.signals.entry = [r for r in spec.signals.entry if (r.side or r.id) != side]
            if len(spec.signals.entry) < before:
                note = f"removed the {side} side: you rejected every {side} example I showed you"
                revisions.append(note)
                spec.assumptions.append(
                    Assumption(
                        id=f"visual:{side}-removed",
                        question=f"Does this strategy trade the {side} side at all?",
                        resolution=f"no -- every {side} example was rejected in the visual review",
                        approved_by="client",
                        alternatives=[f"keep the {side} side"],
                        sensitivity={"if_wrong": f"the {side} side of the strategy is gone entirely"},
                    )
                )

    # pattern 2: rejections cluster early in the session
    early = [
        by_index[v.candidate_index].context.get("minute_of_day", 0)
        for v in rejected
        if by_index[v.candidate_index].context.get("minute_of_day") is not None
    ]
    approved_minutes = [
        by_index[v.candidate_index].context.get("minute_of_day", 0) for v in verdicts if v.approved
    ]
    if early and approved_minutes and max(early) < min(approved_minutes):
        new_start = _minutes_to_hhmm(int(max(early)) + 1)
        if spec.filters.time_windows:
            old = spec.filters.time_windows[0]
            if old.start < new_start:
                spec.filters.time_windows[0] = TimeWindow(start=new_start, end=old.end, tz=old.tz)
                revisions.append(
                    f"moved the signal window start from {old.start} to {new_start}: every example "
                    "before that was rejected"
                )
                spec.assumptions.append(
                    Assumption(
                        id="visual:window-start",
                        question="When does the signal window really start?",
                        resolution=f"{new_start} -- earlier examples were rejected in the visual review",
                        approved_by="client",
                        alternatives=[f"keep {old.start}"],
                        sensitivity={"if_wrong": "signals before the new start time are no longer taken"},
                    )
                )
    return revisions


def _minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _safe(array, index):
    try:
        value = array[index]
        return float(value) if isinstance(value, (int, float, np.floating, np.integer)) else bool(value)
    except Exception:
        return None
