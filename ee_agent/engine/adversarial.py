"""The adversarial pass (ability 45).

A second reasoning pass whose only job is to attack the result. Every result
ships with the case against it attached.

It runs with no model key at all: the attacks are computed from the result
itself. When a model IS configured, :func:`narrate` hands the same evidence to
the model to argue in prose -- but the findings, and the verdict, are computed
either way. A model is a better writer, never the source of the finding.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Attack:
    code: str
    title: str
    severity: str  # fatal | serious | caution | note
    finding: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "severity": self.severity,
            "finding": self.finding,
            "evidence": self.evidence,
        }


@dataclass
class AdversarialBrief:
    attacks: list[Attack] = field(default_factory=list)
    verdict: str = ""
    survived: bool = True

    @property
    def fatal(self) -> list[Attack]:
        return [a for a in self.attacks if a.severity == "fatal"]

    @property
    def serious(self) -> list[Attack]:
        return [a for a in self.attacks if a.severity == "serious"]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "survived": self.survived,
            "n_attacks": len(self.attacks),
            "attacks": [a.to_dict() for a in self.attacks],
        }

    def summary(self) -> str:
        if not self.attacks:
            return "[adversarial] no attack landed. That is rare -- check the sample size."
        lines = ["[adversarial] the case against this result:"]
        order = {"fatal": 0, "serious": 1, "caution": 2, "note": 3}
        for a in sorted(self.attacks, key=lambda x: order.get(x.severity, 9)):
            lines.append(f"    [{a.severity.upper():<7}] {a.title}: {a.finding}")
        lines.append(f"    VERDICT: {self.verdict}")
        return "\n".join(lines)


def attack(report) -> AdversarialBrief:
    """``report`` is a :class:`ee_agent.engine.truth.StrategyReport`."""
    brief = AdversarialBrief()
    base = report.base
    m = base.metrics

    # 1 -- sample size
    if m.n_trades < 30:
        brief.attacks.append(
            Attack(
                "ADV-SAMPLE",
                "sample size",
                "fatal" if m.n_trades < 10 else "serious",
                f"{m.n_trades} trades is not enough to distinguish this from luck. "
                f"With a {m.win_rate:.0%} win rate the 95% confidence interval on the win rate alone "
                f"is roughly +/-{_wr_ci(m.win_rate, m.n_trades):.0%}.",
                {"n_trades": m.n_trades},
            )
        )

    # 2 -- lookahead
    if report.lookahead and not report.lookahead.clean:
        brief.attacks.append(
            Attack(
                "ADV-LOOKAHEAD",
                "lookahead",
                "fatal",
                "The strategy uses information it could not have had at the time. "
                "Nothing downstream of this is meaningful.",
                report.lookahead.to_dict(),
            )
        )

    # 3 -- out-of-sample degradation
    wf = report.walk_forward
    if wf and wf.in_sample and wf.out_of_sample:
        ratio = wf.degradation.get("profit_factor_ratio")
        if ratio is not None and ratio < 0.5:
            brief.attacks.append(
                Attack(
                    "ADV-OOS",
                    "out-of-sample degradation",
                    "serious",
                    f"Out-of-sample profit factor is {ratio:.0%} of in-sample. The edge is largely "
                    "an artefact of the window it was measured on.",
                    {"ratio": ratio},
                )
            )
        if wf.windows and wf.oos_positive_windows <= len(wf.windows) // 3:
            brief.attacks.append(
                Attack(
                    "ADV-WINDOWS",
                    "window dependence",
                    "serious",
                    f"Only {wf.oos_positive_windows} of {len(wf.windows)} out-of-sample windows are "
                    "profitable. The average hides the fact that most periods lose.",
                    {"positive": wf.oos_positive_windows, "total": len(wf.windows)},
                )
            )

    # 4 -- regime concentration
    if report.regimes:
        bucket, share = report.regimes.concentration()
        if share > 0.7 and bucket:
            brief.attacks.append(
                Attack(
                    "ADV-REGIME",
                    "regime dependence",
                    "serious",
                    f"{share:.0%} of the gross profit comes from one bucket ({bucket}). This is a bet "
                    "on that regime persisting, not a general edge.",
                    {"bucket": bucket, "share": round(share, 4)},
                )
            )

    # 5 -- costs as a share of the result
    if m.n_trades and base.costs_total > 0:
        gross = abs(m.gross_pnl) or 1.0
        cost_share = base.costs_total / gross
        if cost_share > 0.5:
            brief.attacks.append(
                Attack(
                    "ADV-COSTS",
                    "cost sensitivity",
                    "serious" if cost_share < 1.0 else "fatal",
                    f"Costs are {cost_share:.0%} of gross P&L. A small increase in slippage or "
                    "commission erases the result; this strategy is a bet on execution quality.",
                    {"cost_share": round(cost_share, 4)},
                )
            )

    # 6 -- Monte Carlo
    if report.monte_carlo and report.monte_carlo.n_paths:
        mc = report.monte_carlo
        if mc.prob_profitable < 0.6:
            brief.attacks.append(
                Attack(
                    "ADV-MC",
                    "path dependence",
                    "serious",
                    f"Only {mc.prob_profitable:.0%} of resampled paths finish profitable. The single "
                    "historical ordering flatters this result.",
                    mc.to_dict(),
                )
            )
        if m.max_drawdown and mc.drawdown_p95 > m.max_drawdown * 1.8:
            brief.attacks.append(
                Attack(
                    "ADV-DD",
                    "drawdown understated",
                    "caution",
                    f"The historical drawdown was {m.max_drawdown:,.0f}, but the 95th percentile of "
                    f"resampled paths is {mc.drawdown_p95:,.0f}. Size for the second number.",
                    {"historical": m.max_drawdown, "p95": mc.drawdown_p95},
                )
            )

    # 7 -- synthetic markets
    if report.synthetic and report.synthetic.n_paths:
        if report.synthetic.percentile_of_real < 75:
            brief.attacks.append(
                Attack(
                    "ADV-SYNTH",
                    "indistinguishable from noise",
                    "serious",
                    f"The real result sits at the {report.synthetic.percentile_of_real:.0f}th percentile "
                    "of bootstrapped histories with the same statistical character and no edge.",
                    report.synthetic.to_dict(),
                )
            )

    # 8 -- selection
    if report.correction and report.correction.n_tested > 1:
        if report.correction.corrected_metric <= 0 < report.correction.best_metric:
            brief.attacks.append(
                Attack(
                    "ADV-SELECT",
                    "multiple comparisons",
                    "fatal",
                    f"{report.correction.n_tested} variants were tested. Corrected for selection, the "
                    "Sharpe of the winner is not distinguishable from zero.",
                    report.correction.to_dict(),
                )
            )

    # 9 -- parameter fragility
    if report.fragility:
        worst = report.fragility.get("worst_drop_pct")
        if worst is not None and worst > 60:
            brief.attacks.append(
                Attack(
                    "ADV-FRAGILE",
                    "parameter fragility",
                    "serious",
                    f"A {report.fragility.get('perturbation_pct', 10)}% change to one parameter costs "
                    f"{worst:.0f}% of the result. Real edges are flatter than this.",
                    report.fragility,
                )
            )

    # 10 -- data quality
    if base.data_quality < 0.9:
        brief.attacks.append(
            Attack(
                "ADV-DATA",
                "data quality",
                "caution",
                f"The dataset scored {base.data_quality:.2f}. Gaps and bad bars move fills, and this "
                "result was computed on them.",
                {"quality": base.data_quality},
            )
        )

    # 11 -- concentration in a few trades
    if m.n_trades >= 10:
        pnls = np.array([t.net_pnl for t in base.trades if t.closed])
        if len(pnls) and pnls.sum() > 0:
            top = np.sort(pnls)[::-1][: max(1, len(pnls) // 20)]
            share = top.sum() / pnls.sum()
            if share > 0.8:
                brief.attacks.append(
                    Attack(
                        "ADV-CONC",
                        "profit concentration",
                        "serious",
                        f"The top {len(top)} trade(s) are {share:.0%} of the net result. Remove them and "
                        "there is no strategy.",
                        {"top_n": int(len(top)), "share": round(float(share), 4)},
                    )
                )

    fatal = len(brief.fatal)
    serious = len(brief.serious)
    if fatal:
        brief.survived = False
        brief.verdict = f"{fatal} fatal finding(s). Do not trade this. Do not report the headline number."
    elif serious >= 2:
        brief.survived = False
        brief.verdict = f"{serious} serious findings. This does not survive the adversarial pass."
    elif serious == 1:
        brief.survived = True
        brief.verdict = "One serious finding. Treat the headline number as an upper bound."
    else:
        brief.survived = True
        brief.verdict = "No fatal or serious findings. That is not proof it works -- it is absence of proof it does not."
    return brief


def _wr_ci(win_rate: float, n: int) -> float:
    if n <= 1:
        return 1.0
    return float(1.96 * np.sqrt(max(win_rate * (1 - win_rate), 1e-9) / n))


def narrate(brief: AdversarialBrief, model=None) -> str:
    """Optional prose. The findings are computed; the model only argues them."""
    if model is None:
        return brief.summary()
    evidence = "\n".join(f"- [{a.severity}] {a.title}: {a.finding}" for a in brief.attacks)
    prompt = (
        "You are the adversary. Below are computed findings against a trading strategy backtest. "
        "Argue the case against the result in plain language for a non-technical trader. Do not "
        "soften anything, do not add findings that are not listed, and do not recommend a strategy "
        "or a risk setting.\n\n" + evidence
    )
    return model.complete(prompt)
