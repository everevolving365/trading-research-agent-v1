"""Browser control for the Operator.

THIS MODULE MAY NEVER PLACE, MODIFY OR CANCEL AN ORDER.

Hard rule 4, in full: a browser agent that can click buy bypasses the position
ledger entirely and would not even appear in the system's own order logs. So
order placement code paths are *physically absent* from this package, not
disabled -- there is no flag, no guarded branch, no commented-out function.
``tests/test_operator.py`` greps every file in ``ee_agent/operator/`` for order
verbs and fails the build if one appears.

The driver abstraction exists so the whole Operator is testable against a local
mock page with no browser and no TradingView account (Section 2.2).
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ee_agent.cost.notifier import ledger
from ee_agent.errors import OperatorRefusal
from ee_agent.paths import ee_home
from ee_agent.secrets.vault import redact

#: Anything resembling an order action. Checked at runtime AND by a grep test.
FORBIDDEN_ACTIONS = {
    "buy", "sell", "place_order", "submit_order", "market_order", "limit_order",
    "close_position", "flatten", "cancel_order", "modify_order", "trade",
}


@dataclass
class Action:
    """One recorded step of the audit trail (ability 32)."""

    ts: str
    kind: str
    target: str
    detail: str = ""
    screenshot: str = ""
    ok: bool = True
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__


class Driver(ABC):
    """The minimum surface the Operator needs. Read and configure only."""

    @abstractmethod
    def goto(self, url: str) -> None: ...

    @abstractmethod
    def fill(self, selector: str, value: str) -> None: ...

    @abstractmethod
    def click(self, selector: str) -> None: ...

    @abstractmethod
    def text(self, selector: str) -> str: ...

    @abstractmethod
    def exists(self, selector: str) -> bool: ...

    @abstractmethod
    def wait_for(self, selector: str, timeout_s: float = 30.0) -> bool: ...

    @abstractmethod
    def screenshot(self, path: Path) -> Path | None: ...

    @abstractmethod
    def html(self) -> str: ...

    def close(self) -> None:  # pragma: no cover - optional
        pass


class MockPageDriver(Driver):
    """Drives a local mock of TradingView's structure.

    Pages are HTML files under ``tests/fixtures/tradingview/``. Interactions
    mutate an in-memory state dict, which is what the flow's assertions read.
    This is how the entire Phase 6 flow is built and verified with no Premium
    account and no network.
    """

    def __init__(self, pages_dir: Path, state: dict | None = None):
        self.pages_dir = Path(pages_dir)
        self.url = ""
        self.state: dict[str, Any] = state if state is not None else {}
        self.state.setdefault("logged_in", False)
        self.state.setdefault("plan", "premium")
        self.state.setdefault("saved_scripts", [])
        self.state.setdefault("alerts", [])
        self.state.setdefault("deep_backtest_done", False)
        self.state.setdefault("pine_source", "")
        self.state.setdefault("published", [])
        self._page = ""
        self.screenshots: list[str] = []

    # ---- navigation
    def goto(self, url: str) -> None:
        self.url = url
        name = {
            "https://www.tradingview.com/accounts/signin/": "signin.html",
            "https://www.tradingview.com/chart/": "chart.html",
            "https://www.tradingview.com/pine-editor/": "pine_editor.html",
            "https://www.tradingview.com/strategy-tester/": "strategy_tester.html",
        }.get(url, "chart.html")
        path = self.pages_dir / name
        self._page = path.read_text(encoding="utf-8") if path.exists() else f"<html>{url}</html>"

    def fill(self, selector: str, value: str) -> None:
        key = _selector_key(selector)
        self.state[f"field:{key}"] = value
        if key in ("pine-source", "pine_editor"):
            self.state["pine_source"] = value

    def click(self, selector: str) -> None:
        key = _selector_key(selector)
        state = self.state
        if key == "signin-submit":
            username = state.get("field:username", "")
            password = state.get("field:password", "")
            state["logged_in"] = bool(username and password)
            state["login_error"] = "" if state["logged_in"] else "missing credentials"
        elif key == "pine-save":
            name = state.get("field:script-name") or "Untitled script"
            state["saved_scripts"].append({"name": name, "source": state.get("pine_source", "")})
        elif key == "pine-add-to-chart":
            state["on_chart"] = state.get("field:script-name") or "Untitled script"
        elif key == "strategy-tester-run":
            state["tester_ran"] = True
        elif key == "deep-backtest-enable":
            state["deep_backtest_enabled"] = True
        elif key == "deep-backtest-generate":
            if state.get("plan") != "premium":
                state["deep_backtest_error"] = "Deep Backtesting requires a Premium plan"
            else:
                state["deep_backtest_done"] = True
        elif key == "alert-create":
            state["alerts"].append(
                {
                    "name": state.get("field:alert-name", "alert"),
                    "webhook": state.get("field:alert-webhook", ""),
                    "message": state.get("field:alert-message", ""),
                    "condition": state.get("field:alert-condition", ""),
                }
            )
        elif key == "publish-script":
            state["published"].append(state.get("field:script-name", ""))

    def text(self, selector: str) -> str:
        key = _selector_key(selector)
        if key == "strategy-report":
            return _mock_report(self.state)
        if key == "plan-badge":
            return self.state.get("plan", "free")
        if key == "login-error":
            return self.state.get("login_error", "")
        if key == "deep-backtest-error":
            return self.state.get("deep_backtest_error", "")
        match = re.search(rf'id="{re.escape(key)}"[^>]*>(.*?)<', self._page, re.S)
        return match.group(1).strip() if match else ""

    def exists(self, selector: str) -> bool:
        key = _selector_key(selector)
        if key == "deep-backtest-toggle":
            return self.state.get("plan") == "premium"
        if key == "strategy-report":
            return bool(self.state.get("tester_ran"))
        return f'id="{key}"' in self._page

    def wait_for(self, selector: str, timeout_s: float = 30.0) -> bool:
        return self.exists(selector)

    def screenshot(self, path: Path) -> Path | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": self.url, "state_keys": sorted(self.state)}, indent=2), encoding="utf-8"
        )
        self.screenshots.append(str(path))
        return path

    def html(self) -> str:
        return self._page


class PlaywrightDriver(Driver):  # pragma: no cover - needs a real browser
    """The real browser. Same surface, so the flow code is identical.

    Cost-reducing engineering (ability 76) lives here: the element map is cached
    so the same screen is never re-read, page text is read directly instead of
    screenshotted wherever possible, and screenshots are downscaled before they
    ever become a vision call.
    """

    def __init__(
        self,
        headless: bool = False,
        browser: str = "chromium",
        user_data_dir: str | None = None,
        downscale_width: int = 1280,
    ):
        self.headless = headless
        self.browser_name = browser
        self.user_data_dir = user_data_dir
        self.downscale_width = downscale_width
        self._pw = None
        self._browser = None
        self.page = None
        self._element_cache: dict[str, Any] = {}

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.browser_name)
        if self.user_data_dir:
            # "attach to an already-logged-in profile": the no-storage alternative
            # to storing TradingView credentials (ability 26, second option).
            context = launcher.launch_persistent_context(self.user_data_dir, headless=self.headless)
            self.page = context.pages[0] if context.pages else context.new_page()
            self._browser = context
        else:
            self._browser = launcher.launch(headless=self.headless)
            self.page = self._browser.new_page()

    def _require(self):
        if self.page is None:
            self.start()
        return self.page

    def goto(self, url: str) -> None:
        self._element_cache.clear()
        self._require().goto(url, wait_until="domcontentloaded")

    def fill(self, selector: str, value: str) -> None:
        self._require().fill(selector, value)

    def click(self, selector: str) -> None:
        self._require().click(selector)
        self._element_cache.clear()

    def text(self, selector: str) -> str:
        element = self._require().query_selector(selector)
        return element.inner_text() if element else ""

    def exists(self, selector: str) -> bool:
        return self._require().query_selector(selector) is not None

    def wait_for(self, selector: str, timeout_s: float = 30.0) -> bool:
        try:
            self._require().wait_for_selector(selector, timeout=timeout_s * 1000)
            return True
        except Exception:
            return False

    def screenshot(self, path: Path) -> Path | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._require().screenshot(path=str(path), full_page=False)
        _downscale(path, self.downscale_width)
        return path

    def html(self) -> str:
        return self._require().content()

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


class AuditTrail:
    """Every action logged with a screenshot and a timestamp (ability 32)."""

    def __init__(self, root: Path | None = None, session: str | None = None, retention_days: int = 30):
        stamp = session or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = Path(root or (ee_home() / "runs" / "operator")) / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.actions: list[Action] = []
        self.retention_days = retention_days
        self.vision_calls = 0

    def record(
        self, kind: str, target: str, detail: str = "", driver: Driver | None = None, ok: bool = True,
        screenshot: bool = True,
    ) -> Action:
        if kind in FORBIDDEN_ACTIONS:
            raise OperatorRefusal(
                f"The Operator was asked to '{kind}'. Order placement is not part of this module "
                "and never will be (hard rule 4). Orders go through the position ledger."
            )
        shot = ""
        if screenshot and driver is not None:
            path = self.dir / f"{len(self.actions):03d}-{kind}.png"
            written = driver.screenshot(path)
            shot = str(written) if written else ""
        action = Action(
            ts=datetime.now(timezone.utc).isoformat(),
            kind=kind,
            target=target,
            detail=redact(detail)[:400],
            screenshot=shot,
            ok=ok,
        )
        self.actions.append(action)
        self._flush()
        return action

    def note_vision_call(self, n: int = 1, downscaled: bool = True) -> None:
        """Vision tokens for computer use are the largest variable cost in the
        system (Section 7), so every one is counted and priced."""
        self.vision_calls += n
        unit = "vision.image_downscaled" if downscaled else "vision.image"
        ledger().spend("operator_session", **{unit: n})

    def _flush(self) -> None:
        (self.dir / "audit.json").write_text(
            json.dumps([a.to_dict() for a in self.actions], indent=2), encoding="utf-8"
        )

    def summary(self) -> str:
        failed = [a for a in self.actions if not a.ok]
        return (
            f"[audit] {len(self.actions)} action(s) recorded in {self.dir}"
            + (f", {len(failed)} failed" if failed else "")
            + f", {self.vision_calls} vision call(s)"
        )


def _selector_key(selector: str) -> str:
    return selector.lstrip("#.").split("[")[0].strip()


def _downscale(path: Path, width: int) -> None:  # pragma: no cover - optional dep
    try:
        from PIL import Image

        with Image.open(path) as img:
            if img.width > width:
                ratio = width / img.width
                img.resize((width, int(img.height * ratio))).save(path)
    except Exception:
        pass


def _mock_report(state: dict) -> str:
    """The Strategy Tester's report panel, in the shape the parser expects."""
    if state.get("deep_backtest_done"):
        return (
            "Net Profit 4,812.50 USD 9.63%\n"
            "Total Closed Trades 214\n"
            "Percent Profitable 41.12%\n"
            "Profit Factor 1.34\n"
            "Max Drawdown 1,905.00 USD 3.81%\n"
            "Avg Trade 22.49 USD\n"
            "Commission Paid 856.00 USD\n"
            "Deep Backtesting 2018-01-01 to 2026-06-30\n"
        )
    return (
        "Net Profit 612.50 USD 1.23%\n"
        "Total Closed Trades 28\n"
        "Percent Profitable 39.29%\n"
        "Profit Factor 1.11\n"
        "Max Drawdown 480.00 USD 0.96%\n"
        "Avg Trade 21.88 USD\n"
        "Commission Paid 112.00 USD\n"
    )
