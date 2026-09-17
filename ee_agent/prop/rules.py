"""Prop firm rule simulation (ability 42).

Every backtest on a prop account ends by answering three questions:

* what percentage of simulated runs pass the combine,
* in how many days,
* and what percentage blow it.

The rules themselves are data files under ``ee_agent/prop/firms/``. Adding a
firm is adding a YAML file. There is no firm-specific code path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

FIRMS_DIR = Path(__file__).with_name("firms")


@dataclass
class AccountRules:
    id: str
    label: str
    starting_balance: float
    profit_target: float
    daily_loss_limit: float | None
    trailing_max_drawdown: float
    trailing_type: str = "intraday_peak"
    trailing_stops_at_initial_balance: bool = True
    max_contracts: int = 5
    min_trading_days: int = 2
    consistency_rule_pct: float | None = None
    eval_fee_monthly: float = 0.0
    reset_fee: float = 0.0
    payout_threshold: float = 0.0
    scaling_plan: bool = False
    allow_overnight: bool = False
    allow_news_trading: bool = True


@dataclass
class FirmRules:
    firm: str
    display_name: str
    updated: str
    source: str
    platform: str
    accounts: list[AccountRules]
    contract_equivalents: dict[str, int] = field(default_factory=dict)
    flat_by: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def account(self, account_id: str | None = None) -> AccountRules:
        if account_id is None:
            return self.accounts[0]
        for a in self.accounts:
            if a.id == account_id:
                return a
        raise KeyError(f"{self.firm} has no account '{account_id}'. Known: {[a.id for a in self.accounts]}")


@lru_cache(maxsize=None)
def load_firm(firm: str) -> FirmRules:
    path = FIRMS_DIR / f"{firm.lower()}.yaml"
    if not path.exists():
        raise KeyError(f"No rule pack for '{firm}'. Available: {', '.join(available_firms())}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    accounts = [AccountRules(**a) for a in raw.get("accounts", [])]
    return FirmRules(
        firm=raw["firm"],
        display_name=raw.get("display_name", raw["firm"]),
        updated=raw.get("updated", ""),
        source=raw.get("source", ""),
        platform=raw.get("platform", ""),
        accounts=accounts,
        contract_equivalents=raw.get("contract_equivalents", {}),
        flat_by=raw.get("flat_by", {}),
        notes=raw.get("notes", []),
    )


def available_firms() -> list[str]:
    return sorted(p.stem for p in FIRMS_DIR.glob("*.yaml"))


# ------------------------------------------------------------------ simulation
@dataclass
class CombineOutcome:
    passed: bool
    blew_up: bool
    days_taken: int
    final_pnl: float
    reason: str = ""
    breached_rule: str = ""


@dataclass
class CombineReport:
    firm: str
    account: str
    n_runs: int
    pass_rate: float
    blowup_rate: float
    timeout_rate: float
    median_days_to_pass: float | None
    p90_days_to_pass: float | None
    expected_resets: float
    expected_cost_to_funding: float
    breach_reasons: dict[str, int] = field(default_factory=dict)
    consistency_failures: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "firm": self.firm,
            "account": self.account,
            "n_runs": self.n_runs,
            "pass_rate": round(self.pass_rate, 4),
            "blowup_rate": round(self.blowup_rate, 4),
            "timeout_rate": round(self.timeout_rate, 4),
            "median_days_to_pass": self.median_days_to_pass,
            "p90_days_to_pass": self.p90_days_to_pass,
            "expected_resets": round(self.expected_resets, 2),
            "expected_cost_to_funding": round(self.expected_cost_to_funding, 2),
            "breach_reasons": self.breach_reasons,
            "consistency_failures": self.consistency_failures,
            "note": self.note,
        }

    def summary(self) -> str:
        days = f"{self.median_days_to_pass:.0f}" if self.median_days_to_pass else "n/a"
        p90 = f"{self.p90_days_to_pass:.0f}" if self.p90_days_to_pass else "n/a"
        lines = [
            f"[combine] {self.firm} {self.account}: {self.pass_rate:.0%} of {self.n_runs:,} simulated runs pass, "
            f"{self.blowup_rate:.0%} blow it, {self.timeout_rate:.0%} neither.",
            f"          median {days} trading days to pass (90th percentile {p90}).",
            f"          expected {self.expected_resets:.1f} reset(s), "
            f"about ${self.expected_cost_to_funding:,.0f} in fees to reach funding.",
        ]
        if self.breach_reasons:
            top = sorted(self.breach_reasons.items(), key=lambda kv: -kv[1])[:3]
            lines.append("          failures: " + ", ".join(f"{k} x{v}" for k, v in top))
        if self.consistency_failures:
            lines.append(
                f"          {self.consistency_failures} run(s) hit the profit target but failed the "
                "consistency rule -- one day carried too much of the profit."
            )
        if self.note:
            lines.append(f"          {self.note}")
        return "\n".join(lines)


def simulate_combine(
    daily_pnls: list[float] | np.ndarray,
    firm: str = "topstep",
    account_id: str | None = None,
    n_runs: int = 1000,
    max_days: int = 60,
    seed: int = 41,
    intraday_swing_factor: float = 1.6,
) -> CombineReport:
    """Bootstrap whole trading days and run each simulated account under the
    firm's real rules.

    ``intraday_swing_factor`` models the fact that a day that closes -400 may
    have been -640 at its worst, which is what actually breaches a daily loss
    limit. Without it, every combine simulation is optimistic.
    """
    rules = load_firm(firm)
    account = rules.account(account_id)
    pnls = np.asarray(list(daily_pnls), dtype=float)
    report = CombineReport(
        firm=rules.display_name,
        account=account.label,
        n_runs=n_runs,
        pass_rate=0.0,
        blowup_rate=0.0,
        timeout_rate=0.0,
        median_days_to_pass=None,
        p90_days_to_pass=None,
        expected_resets=0.0,
        expected_cost_to_funding=0.0,
    )
    if len(pnls) < 2:
        report.note = "not enough trading days in the backtest to simulate a combine"
        return report

    rng = np.random.default_rng(seed)
    outcomes: list[CombineOutcome] = []
    for _ in range(n_runs):
        outcomes.append(
            _run_one(rng, pnls, account, max_days=max_days, swing=intraday_swing_factor)
        )

    passed = [o for o in outcomes if o.passed]
    blew = [o for o in outcomes if o.blew_up]
    report.pass_rate = len(passed) / n_runs
    report.blowup_rate = len(blew) / n_runs
    report.timeout_rate = 1.0 - report.pass_rate - report.blowup_rate
    if passed:
        days = np.array([o.days_taken for o in passed], dtype=float)
        report.median_days_to_pass = float(np.percentile(days, 50))
        report.p90_days_to_pass = float(np.percentile(days, 90))
    for o in outcomes:
        if o.breached_rule:
            report.breach_reasons[o.breached_rule] = report.breach_reasons.get(o.breached_rule, 0) + 1
    report.consistency_failures = sum(1 for o in outcomes if o.breached_rule == "consistency_rule")

    if report.pass_rate > 0:
        attempts = 1.0 / report.pass_rate
        report.expected_resets = max(attempts - 1.0, 0.0)
        months = max((report.median_days_to_pass or 20) / 21.0, 1.0)
        report.expected_cost_to_funding = (
            account.eval_fee_monthly * months * attempts + account.reset_fee * report.expected_resets
        )
    else:
        report.expected_cost_to_funding = float("inf")
        report.note = "No simulated run passed. At this pass rate the evaluation fee is a recurring loss."
    return report


def _run_one(rng, pnls: np.ndarray, account: AccountRules, max_days: int, swing: float) -> CombineOutcome:
    balance = account.starting_balance
    peak = balance
    floor = balance - account.trailing_max_drawdown
    day_pnls: list[float] = []

    for day in range(1, max_days + 1):
        day_pnl = float(rng.choice(pnls))
        day_pnls.append(day_pnl)

        # intraday worst point, modelled from the day's close
        worst = min(day_pnl * swing, day_pnl) if day_pnl < 0 else -abs(day_pnl) * (swing - 1.0) * 0.5
        intraday_low = balance + worst
        intraday_high = balance + max(day_pnl, 0.0) * swing

        if account.daily_loss_limit is not None and -worst >= account.daily_loss_limit:
            return CombineOutcome(False, True, day, sum(day_pnls), "daily loss limit", "daily_loss_limit")

        if account.trailing_type == "intraday_peak":
            peak = max(peak, intraday_high)
            floor = peak - account.trailing_max_drawdown
            if account.trailing_stops_at_initial_balance:
                floor = min(floor, account.starting_balance)
            if intraday_low <= floor:
                return CombineOutcome(
                    False, True, day, sum(day_pnls), "trailing drawdown", "trailing_max_drawdown"
                )

        balance += day_pnl

        if account.trailing_type == "end_of_day":
            peak = max(peak, balance)
            floor = peak - account.trailing_max_drawdown
            if account.trailing_stops_at_initial_balance:
                floor = min(floor, account.starting_balance)
            if balance <= floor:
                return CombineOutcome(
                    False, True, day, sum(day_pnls), "trailing drawdown", "trailing_max_drawdown"
                )

        profit = balance - account.starting_balance
        if profit >= account.profit_target and day >= account.min_trading_days:
            if account.consistency_rule_pct:
                best_day = max(day_pnls)
                if profit > 0 and best_day / profit * 100.0 > account.consistency_rule_pct:
                    return CombineOutcome(
                        False, False, day, profit, "consistency rule", "consistency_rule"
                    )
            return CombineOutcome(True, False, day, profit, "profit target reached")

    return CombineOutcome(False, False, max_days, sum(day_pnls), "ran out of days", "timeout")


def daily_pnls_from_trades(trades, bars) -> list[float]:
    """Group closed trades into trading days, which is the unit prop rules use."""
    by_day: dict[str, float] = {}
    for t in trades:
        if not t.closed:
            continue
        idx = min(t.exit_index, len(bars) - 1)
        day = bars.local_date[idx]
        by_day[day] = by_day.get(day, 0.0) + t.net_pnl
    return [by_day[d] for d in sorted(by_day)]


def position_limit_ok(symbol: str, size: float, firm: str, account_id: str | None = None) -> tuple[bool, str]:
    rules = load_firm(firm)
    account = rules.account(account_id)
    weight = rules.contract_equivalents.get(symbol.upper(), 1)
    used = size * weight
    if used > account.max_contracts:
        return False, (
            f"{size} {symbol} is {used} micro-equivalents; {account.label} allows "
            f"{account.max_contracts}."
        )
    return True, ""
