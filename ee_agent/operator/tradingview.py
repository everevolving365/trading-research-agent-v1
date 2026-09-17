"""The TradingView flow (abilities 26-33, 46).

Log in, open the Pine Editor, paste the generated script, save it to the
client's account, add it to the chart, run the Strategy Tester, enable Deep
Backtesting, read the report, set up alerts and webhooks.

Everything here READS AND CONFIGURES. Nothing here places an order.

Section 9, honest limit 1, is enforced in code, not just documented: Deep
Backtest results appear only in the Strategy Tester report panel, never on the
chart, and the chart's trades come from the regular backtest. Reading chart
trades would silently produce wrong numbers, so :meth:`read_report` refuses to
read them and says why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ee_agent.compile.to_live import LiveConfig
from ee_agent.compile.to_pine import PineArtifact
from ee_agent.errors import OperatorRefusal
from ee_agent.operator.browser import AuditTrail, Driver
from ee_agent.secrets.vault import get_secret

SELECTORS = {
    "signin_url": "https://www.tradingview.com/accounts/signin/",
    "chart_url": "https://www.tradingview.com/chart/",
    "pine_url": "https://www.tradingview.com/pine-editor/",
    "tester_url": "https://www.tradingview.com/strategy-tester/",
    "username": "#username",
    "password": "#password",
    "signin_submit": "#signin-submit",
    "login_error": "#login-error",
    "plan_badge": "#plan-badge",
    "pine_source": "#pine-source",
    "script_name": "#script-name",
    "pine_save": "#pine-save",
    "pine_add_to_chart": "#pine-add-to-chart",
    "tester_run": "#strategy-tester-run",
    "strategy_report": "#strategy-report",
    "deep_toggle": "#deep-backtest-toggle",
    "deep_enable": "#deep-backtest-enable",
    "deep_generate": "#deep-backtest-generate",
    "deep_error": "#deep-backtest-error",
    "alert_name": "#alert-name",
    "alert_condition": "#alert-condition",
    "alert_message": "#alert-message",
    "alert_webhook": "#alert-webhook",
    "alert_create": "#alert-create",
    "publish": "#publish-script",
    "chart_trades": "#chart-trades-list",
}


@dataclass
class TVReport:
    """A parsed Strategy Tester report. Deep-backtest flag is never assumed."""

    net_profit: float = 0.0
    net_profit_pct: float = 0.0
    total_trades: int = 0
    percent_profitable: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_trade: float = 0.0
    commission_paid: float = 0.0
    deep: bool = False
    range_text: str = ""
    raw: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "raw"}

    def summary(self) -> str:
        kind = "DEEP backtest" if self.deep else "regular backtest"
        return (
            f"[tradingview] {kind}: net {self.net_profit:,.2f} ({self.net_profit_pct:.2f}%), "
            f"{self.total_trades} trades, {self.percent_profitable:.2f}% profitable, "
            f"PF {self.profit_factor:.2f}, max DD {self.max_drawdown:,.2f}, "
            f"commission {self.commission_paid:,.2f}"
            + (f", range {self.range_text}" if self.range_text else "")
        )


def parse_report(text: str) -> TVReport:
    """Parse the report panel. Tolerant of thousands separators and % columns."""

    def num(pattern: str, default: float = 0.0) -> float:
        m = re.search(pattern, text, re.I)
        if not m:
            return default
        return float(m.group(1).replace(",", "").replace("%", ""))

    report = TVReport(raw=text)
    report.net_profit = num(r"Net Profit\s+(-?[\d,\.]+)")
    report.net_profit_pct = num(r"Net Profit\s+-?[\d,\.]+\s*\w*\s+(-?[\d,\.]+)%")
    report.total_trades = int(num(r"Total Closed Trades\s+([\d,]+)"))
    report.percent_profitable = num(r"Percent Profitable\s+([\d,\.]+)")
    report.profit_factor = num(r"Profit Factor\s+([\d,\.]+)")
    report.max_drawdown = num(r"Max Drawdown\s+(-?[\d,\.]+)")
    report.max_drawdown_pct = num(r"Max Drawdown\s+-?[\d,\.]+\s*\w*\s+([\d,\.]+)%")
    report.avg_trade = num(r"Avg Trade\s+(-?[\d,\.]+)")
    report.commission_paid = num(r"Commission Paid\s+([\d,\.]+)")
    m = re.search(r"Deep Backtesting\s+([\d\-]+ to [\d\-]+)", text, re.I)
    if m:
        report.deep = True
        report.range_text = m.group(1)
    return report


@dataclass
class FlowResult:
    ok: bool
    steps: list[str] = field(default_factory=list)
    report: TVReport | None = None
    saved_script: str = ""
    alerts_created: int = 0
    plan: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"[operator] {'completed' if self.ok else 'INCOMPLETE'}: {len(self.steps)} step(s)"]
        lines += [f"    {s}" for s in self.steps]
        for e in self.errors:
            lines.append(f"    ERROR: {e}")
        if self.report:
            lines.append("    " + self.report.summary())
        return "\n".join(lines)


class TradingViewOperator:
    """Drives TradingView as a human would. Reads and configures. Never trades."""

    def __init__(self, driver: Driver, audit: AuditTrail | None = None, screenshots: bool = True):
        self.driver = driver
        self.audit = audit or AuditTrail()
        self.screenshots = screenshots

    # ------------------------------------------------------------------ auth
    def login(self, username: str | None = None, password: str | None = None) -> bool:
        """Credentials come from the OS keychain, are never logged, and are never
        sent to any model. `ee-agent secrets delete TRADINGVIEW_PASSWORD` removes
        them in one command (ability 26)."""
        username = username or get_secret("TRADINGVIEW_USERNAME")
        password = password or get_secret("TRADINGVIEW_PASSWORD")
        if not username or not password:
            raise OperatorRefusal(
                "No TradingView credentials. Either store them with "
                "`ee-agent secrets set TRADINGVIEW_USERNAME` / `TRADINGVIEW_PASSWORD`, or run the "
                "Operator against an already-logged-in browser profile "
                "(operator.credential_mode: attach_to_profile) so nothing is stored at all."
            )
        self.driver.goto(SELECTORS["signin_url"])
        self.driver.fill(SELECTORS["username"], username)
        self.driver.fill(SELECTORS["password"], password)
        self.driver.click(SELECTORS["signin_submit"])
        error = self.driver.text(SELECTORS["login_error"])
        ok = not error
        self.audit.record("login", "tradingview", "signed in" if ok else error, self.driver, ok,
                          screenshot=self.screenshots)
        return ok

    def detect_plan(self) -> str:
        """Plan detection for the Premium gate (Phase 6)."""
        plan = (self.driver.text(SELECTORS["plan_badge"]) or "unknown").strip().lower()
        self.audit.record("detect_plan", "tradingview", plan, self.driver, screenshot=False)
        return plan

    # ------------------------------------------------------------- pine flow
    def paste_and_save(self, artifact: PineArtifact, name: str) -> str:
        self.driver.goto(SELECTORS["pine_url"])
        self.driver.fill(SELECTORS["pine_source"], artifact.source)
        self.driver.fill(SELECTORS["script_name"], name)
        self.driver.click(SELECTORS["pine_save"])
        self.audit.record(
            "save_script", name, f"{artifact.kind}, {len(artifact.source.splitlines())} lines, "
            f"hash {artifact.hash[:19]}", self.driver, screenshot=self.screenshots
        )
        return name

    def add_to_chart(self, name: str) -> None:
        self.driver.click(SELECTORS["pine_add_to_chart"])
        self.audit.record("add_to_chart", name, "", self.driver, screenshot=self.screenshots)

    def run_strategy_tester(self) -> None:
        self.driver.goto(SELECTORS["tester_url"])
        self.driver.click(SELECTORS["tester_run"])
        self.driver.wait_for(SELECTORS["strategy_report"], timeout_s=120)
        self.audit.record("strategy_tester", "run", "", self.driver, screenshot=self.screenshots)

    def enable_deep_backtest(self, start: str = "", end: str = "") -> bool:
        """Deep Backtesting requires Premium. If the plan is not Premium, say so
        plainly and carry on with the regular backtest -- never silently
        substitute one for the other."""
        if not self.driver.exists(SELECTORS["deep_toggle"]):
            self.audit.record(
                "deep_backtest", "unavailable",
                "Deep Backtesting is gated behind TradingView Premium on this account",
                self.driver, ok=False, screenshot=False,
            )
            return False
        self.driver.click(SELECTORS["deep_enable"])
        if start:
            self.driver.fill("#deep-start", start)
        if end:
            self.driver.fill("#deep-end", end)
        self.driver.click(SELECTORS["deep_generate"])
        error = self.driver.text(SELECTORS["deep_error"])
        ok = not error
        self.audit.record("deep_backtest", "generate", error or f"{start} to {end}", self.driver, ok,
                          screenshot=self.screenshots)
        return ok

    def read_report(self, expect_deep: bool = False) -> TVReport:
        """Read the Strategy Tester REPORT PANEL. Never the chart's trade list.

        The chart's trades come from the regular backtest even while a deep
        backtest is displayed in the panel; reading them would silently produce
        wrong numbers (Section 9, honest limit 1).
        """
        text = self.driver.text(SELECTORS["strategy_report"])
        if not text:
            raise OperatorRefusal("The Strategy Tester report panel is empty; nothing to read.")
        report = parse_report(text)
        self.audit.note_vision_call(0)  # the panel is read as text, not as a screenshot
        if expect_deep and not report.deep:
            self.audit.record(
                "read_report", "mismatch",
                "asked for a deep backtest but the panel is showing a regular one",
                self.driver, ok=False, screenshot=False,
            )
        self.audit.record("read_report", "strategy_tester", report.summary(), self.driver,
                          screenshot=self.screenshots)
        return report

    def read_chart_trades(self) -> None:
        raise OperatorRefusal(
            "Refusing to read the chart's trade list. During a Deep Backtest the chart still shows "
            "the REGULAR backtest's trades, so these numbers would not be the ones you asked for. "
            "The report panel is the only correct source. (Section 9, honest limit 1.)"
        )

    # ---------------------------------------------------------------- alerts
    def create_alert(self, config: LiveConfig, side: str, webhook_url: str) -> dict:
        """Alerts and webhooks so live trading runs off the indicator's signals
        (ability 30). The payload carries the spec hash, so a stale script's
        alert is refused by the receiver."""
        payload = config.webhook_payload_template(side)
        import json as _json

        self.driver.fill(SELECTORS["alert_name"], f"{config.spec_id}-{side}")
        self.driver.fill(SELECTORS["alert_condition"], f"{side} signal")
        self.driver.fill(SELECTORS["alert_message"], _json.dumps(payload))
        self.driver.fill(SELECTORS["alert_webhook"], webhook_url)
        self.driver.click(SELECTORS["alert_create"])
        self.audit.record("create_alert", f"{config.spec_id}-{side}", webhook_url, self.driver,
                          screenshot=self.screenshots)
        return payload

    # --------------------------------------------------------------- publish
    def publish_script(self, name: str, confirm: bool = False) -> bool:
        """Publishing is public and irreversible, so it always requires an
        explicit human confirmation (ability 33). Saving privately does not."""
        if not confirm:
            raise OperatorRefusal(
                f"Publishing '{name}' would make it public and cannot be undone. That needs your "
                "explicit confirmation -- pass confirm=True only after a human has said yes. "
                "Saving privately to your account needs no confirmation and has already happened."
            )
        self.driver.click(SELECTORS["publish"])
        self.audit.record("publish_script", name, "PUBLIC, human-confirmed", self.driver,
                          screenshot=self.screenshots)
        return True

    # ------------------------------------------------------------ whole flow
    def full_flow(
        self,
        indicator: PineArtifact,
        strategy: PineArtifact,
        live_config: LiveConfig,
        webhook_url: str = "",
        deep_range: tuple[str, str] = ("", ""),
    ) -> FlowResult:
        """The end-to-end Phase 6 flow, start to finish."""
        result = FlowResult(ok=True)
        try:
            if not self.login():
                result.ok = False
                result.errors.append("login failed")
                return result
            result.steps.append("logged in to the client's TradingView account")

            result.plan = self.detect_plan()
            result.steps.append(f"plan detected: {result.plan}")

            name = f"{live_config.spec_id} [indicator]"
            self.paste_and_save(indicator, name)
            self.add_to_chart(name)
            result.saved_script = name
            result.steps.append(f"indicator saved to the account and added to the chart: {name}")

            strategy_name = f"{live_config.spec_id} [strategy]"
            self.paste_and_save(strategy, strategy_name)
            self.add_to_chart(strategy_name)
            result.steps.append(f"strategy script saved and added: {strategy_name}")

            self.run_strategy_tester()
            result.steps.append("Strategy Tester run")

            deep_ok = self.enable_deep_backtest(*deep_range)
            if deep_ok:
                result.steps.append("Deep Backtesting enabled and generated")
            else:
                result.steps.append(
                    "Deep Backtesting unavailable on this plan -- continuing with the regular "
                    "backtest and saying so rather than passing one off as the other"
                )

            result.report = self.read_report(expect_deep=deep_ok)
            result.steps.append("report panel parsed")

            if webhook_url:
                for side in ("long", "short"):
                    self.create_alert(live_config, side, webhook_url)
                    result.alerts_created += 1
                result.steps.append(f"{result.alerts_created} alert(s) with webhooks configured")
        except Exception as exc:
            result.ok = False
            result.errors.append(str(exc))
        return result


def cross_check(python_report, tv_report: TVReport, tolerance_pct: float = 25.0) -> dict:
    """Ability 46: TradingView's deep backtest as an independent cross-check of
    the Python engine. The Python engine stays authoritative; this reports the
    gap and what would explain it."""
    py_net = python_report.base.metrics.net_pnl
    py_trades = python_report.base.metrics.n_trades
    gap_net = tv_report.net_profit - py_net
    trade_gap = tv_report.total_trades - py_trades
    pct = abs(gap_net) / max(abs(py_net), 1e-9) * 100.0
    explanations = []
    if trade_gap:
        explanations.append(
            f"{abs(trade_gap)} {'more' if trade_gap > 0 else 'fewer'} trades on TradingView -- "
            "different data feed, different session handling, or a different contract"
        )
    if abs(tv_report.commission_paid - python_report.base.costs_total) > 1:
        explanations.append(
            f"commission differs: {tv_report.commission_paid:,.2f} there against "
            f"{python_report.base.costs_total:,.2f} here"
        )
    if not tv_report.deep:
        explanations.append(
            "the TradingView number is a REGULAR backtest, which uses far less history than the "
            "deep one -- not a like-for-like comparison"
        )
    return {
        "python_net": round(py_net, 2),
        "tradingview_net": round(tv_report.net_profit, 2),
        "gap": round(gap_net, 2),
        "gap_pct": round(pct, 2),
        "python_trades": py_trades,
        "tradingview_trades": tv_report.total_trades,
        "agree": pct <= tolerance_pct,
        "explanations": explanations,
        "authority": "The Python engine is authoritative. TradingView is a cross-check, not a referee.",
    }
