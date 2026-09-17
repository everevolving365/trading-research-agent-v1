"""Auto-generated tearsheet per strategy (ability 69).

Description, spec, equity curve, regime breakdown, adversarial brief, live track
record -- one self-contained HTML file per strategy, plus a plain-text version
that works with no dependencies at all.

The adversarial brief is not an appendix. It is printed at the top, because a
tearsheet that buries the case against the result is marketing.
"""
from __future__ import annotations

import base64
import html
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ee_agent.paths import runs_dir


def _equity_svg(equity: np.ndarray, width: int = 860, height: int = 220) -> str:
    """An inline SVG equity curve. No plotting library required."""
    if len(equity) < 2:
        return "<p>not enough data for a curve</p>"
    sample = equity if len(equity) <= 1500 else equity[:: max(1, len(equity) // 1500)]
    lo, hi = float(np.min(sample)), float(np.max(sample))
    span = max(hi - lo, 1e-9)
    points = []
    peak = -1e18
    dd_points = []
    running_peak = np.maximum.accumulate(sample)
    for i, value in enumerate(sample):
        x = 40 + (width - 60) * i / (len(sample) - 1)
        y = height - 25 - (height - 50) * (value - lo) / span
        points.append(f"{x:.1f},{y:.1f}")
        drawdown = value - running_peak[i]
        dd_points.append(drawdown)
    zero_y = height - 25 - (height - 50) * (sample[0] - lo) / span
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="equity curve">'
        f'<line x1="40" y1="{zero_y:.1f}" x2="{width - 20}" y2="{zero_y:.1f}" '
        f'stroke="#bbb" stroke-dasharray="4 4"/>'
        f'<polyline fill="none" stroke="#2166ac" stroke-width="1.6" points="{" ".join(points)}"/>'
        f'<text x="40" y="16" font-size="11" fill="#666">peak {hi:,.0f}</text>'
        f'<text x="40" y="{height - 6}" font-size="11" fill="#666">trough {lo:,.0f}</text>'
        f"</svg>"
    )


def _table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows
    )
    return f"<table>{body}</table>"


def text_tearsheet(report, live_track: dict | None = None) -> str:
    """The version that always works."""
    spec = report.spec
    lines = [
        "=" * 78,
        f"  {spec.name}  [{spec.id}]",
        f"  spec {spec.hash[:19]}   plan {report.base.plan_hash[:19]}",
        f"  generated {datetime.now(timezone.utc).isoformat()}",
        "=" * 78,
        "",
        "THE CASE AGAINST THIS RESULT (read this first)",
        "-" * 78,
        report.adversarial.summary() if report.adversarial else "not run",
        "",
        "WHAT THE CLIENT DESCRIBED",
        "-" * 78,
        spec.describe(),
        "",
        "RESULT",
        "-" * 78,
        report.report(),
    ]
    if live_track:
        lines += [
            "",
            "LIVE TRACK RECORD (verified signal ledger)",
            "-" * 78,
            json.dumps(live_track, indent=2),
        ]
    return "\n".join(lines)


def html_tearsheet(report, live_track: dict | None = None) -> str:
    spec = report.spec
    m = report.base.metrics
    wf = report.walk_forward
    adversarial = report.adversarial

    attack_rows = ""
    if adversarial:
        for a in adversarial.attacks:
            attack_rows += (
                f'<li class="sev-{html.escape(a.severity)}"><strong>{html.escape(a.title)}</strong>'
                f" &mdash; {html.escape(a.finding)}</li>"
            )
        if not adversarial.attacks:
            attack_rows = "<li>No attack landed. That is rare -- check the sample size.</li>"

    regime_html = ""
    if report.regimes and report.regimes.by_dimension:
        for dimension, buckets in report.regimes.by_dimension.items():
            rows = "".join(
                f"<tr><td>{html.escape(name)}</td><td>{stats['n']}</td>"
                f"<td>{stats['net_pnl']:,.0f}</td><td>{stats['win_rate']:.0%}</td></tr>"
                for name, stats in buckets.items()
            )
            regime_html += (
                f"<h4>{html.escape(dimension.replace('_', ' '))}</h4>"
                f"<table><tr><th>bucket</th><th>trades</th><th>net</th><th>win rate</th></tr>{rows}</table>"
            )

    headline = _table(
        [
            ("Instrument", f"{report.base.symbol} {report.base.timeframe}"),
            ("Window", f"{report.base.start[:10]} to {report.base.end[:10]}"),
            ("Trades", f"{m.n_trades:,}"),
            ("Net P&L", f"{m.net_pnl:,.2f}"),
            ("Max drawdown", f"{m.max_drawdown:,.2f} ({m.max_drawdown_pct:.1f}%)"),
            ("Profit factor", f"{m.profit_factor:.2f}"),
            ("Win rate", f"{m.win_rate:.1%}"),
            ("Sharpe (annualised)", f"{m.sharpe:.2f}"),
            ("Costs paid", f"{report.base.costs_total:,.2f}"),
            ("Data quality", f"{report.base.data_quality:.2f}"),
            (
                "In-sample / out-of-sample",
                f"{wf.in_sample.net_pnl:,.0f} / {wf.out_of_sample.net_pnl:,.0f}"
                if wf and wf.in_sample and wf.out_of_sample
                else "not run",
            ),
            (
                "Monte Carlo drawdown (p95)",
                f"{report.monte_carlo.drawdown_p95:,.0f}" if report.monte_carlo else "not run",
            ),
            ("Lookahead", report.lookahead.verdict if report.lookahead else "not run"),
            (
                "Combine pass rate",
                f"{report.combine.pass_rate:.0%} ({report.combine.firm} {report.combine.account})"
                if report.combine
                else "n/a",
            ),
            ("Cost to produce", f"${report.cost_usd:,.4f}"),
        ]
    )

    live_html = ""
    if live_track:
        live_html = (
            "<h2>Live track record</h2>"
            + _table([(k.replace("_", " "), str(v)) for k, v in live_track.items()])
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(spec.name)} tearsheet</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
        margin: 0 auto; padding: 24px 18px 60px; max-width: 900px; }}
 h1 {{ font-size: 1.5rem; margin-bottom: .2rem; }}
 .sub {{ color: #777; font-size: .85rem; margin-bottom: 1.4rem; font-family: ui-monospace, monospace; }}
 h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; }}
 h4 {{ margin: 1rem 0 .3rem; font-size: .92rem; }}
 table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .9rem; }}
 th, td {{ text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #8883; }}
 th {{ width: 34%; font-weight: 600; color: #666; }}
 pre {{ background: #8881; padding: .8rem; border-radius: 6px; overflow-x: auto; font-size: .82rem; }}
 ul.attacks {{ list-style: none; padding-left: 0; }}
 ul.attacks li {{ padding: .5rem .7rem; margin-bottom: .4rem; border-left: 4px solid #999;
                  background: #8881; border-radius: 0 4px 4px 0; font-size: .9rem; }}
 .sev-fatal {{ border-left-color: #c0392b; }}
 .sev-serious {{ border-left-color: #e67e22; }}
 .sev-caution {{ border-left-color: #f1c40f; }}
 .sev-note {{ border-left-color: #7f8c8d; }}
 .verdict {{ font-weight: 600; margin-top: .6rem; }}
 @media (max-width: 480px) {{ th {{ width: 45%; }} }}
</style></head>
<body>
<h1>{html.escape(spec.name)}</h1>
<div class="sub">{html.escape(spec.id)} &middot; spec {html.escape(spec.hash[:19])} &middot;
 plan {html.escape(report.base.plan_hash[:19])} &middot;
 generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>

<h2>The case against this result</h2>
<ul class="attacks">{attack_rows}</ul>
<div class="verdict">{html.escape(adversarial.verdict if adversarial else '')}</div>

<h2>What the client described</h2>
<pre>{html.escape(spec.describe())}</pre>

<h2>Result</h2>
{headline}
<h2>Equity curve</h2>
{_equity_svg(report.base.equity)}

<h2>Where the result comes from</h2>
{regime_html or "<p>not run</p>"}

{live_html}

<h2>The spec this was compiled from</h2>
<pre>{html.escape(spec.to_yaml())}</pre>

<p class="sub">Every number above was produced with costs charged on every fill, an in-sample and an
out-of-sample split, a Monte Carlo band and a lookahead check. The Python engine is authoritative;
TradingView deep backtests are a cross-check, not a referee.</p>
</body></html>"""


@dataclass
class TearsheetPaths:
    html: Path
    text: Path


def write_tearsheet(report, live_track: dict | None = None, out_dir: Path | None = None) -> TearsheetPaths:
    directory = Path(out_dir or (runs_dir() / "tearsheets"))
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.spec.id}-{report.base.symbol}-{report.spec.short_hash}"
    html_path = directory / f"{stem}.html"
    text_path = directory / f"{stem}.txt"
    html_path.write_text(html_tearsheet(report, live_track), encoding="utf-8")
    text_path.write_text(text_tearsheet(report, live_track), encoding="utf-8")
    return TearsheetPaths(html_path, text_path)
