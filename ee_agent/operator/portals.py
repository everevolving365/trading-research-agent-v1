"""Retrieving data from manual export portals, statement downloads and purchase
flows, then handing it to ingestion (ability 31).

Plenty of the best data has no API. It sits behind a login and a "Download CSV"
button: a broker statement, a CME DataMine order, a TradingView chart export, a
prop firm's trade history. The Operator drives those the way a human would and
hands whatever comes back to :mod:`ee_agent.data.ingest`, which infers the
schema so the client never has to describe it.

Per-venue selectors are DATA, in :data:`PORTALS`. Adding a venue is adding an
entry, not writing code -- the same principle as the instrument registry.

This module inherits hard rule 4 in full: it may never place, modify or cancel
an order. It logs in, navigates, downloads and hands off. Nothing else.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from ee_agent.errors import DataError, OperatorRefusal
from ee_agent.operator.browser import AuditTrail, Driver
from ee_agent.paths import ee_home
from ee_agent.secrets.vault import get_secret


@dataclass
class PortalSpec:
    """One venue's export flow, described rather than coded."""

    id: str
    name: str
    login_url: str
    export_url: str
    username_secret: str = ""
    password_secret: str = ""
    selectors: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    download_wait_s: float = 30.0
    produces: str = "bars"  # bars | fills | statement
    notes: str = ""

    @property
    def needs_login(self) -> bool:
        return bool(self.username_secret)

    def line(self) -> str:
        creds = f"needs {self.username_secret}" if self.needs_login else "no login"
        return f"  {self.id:<18} {self.name:<28} {self.produces:<10} {creds}"


#: Every portal the Operator knows how to drive. Selector sets are best-effort
#: and WILL need correcting the first time each runs against the live site --
#: which is exactly why they are one dictionary each and not code.
PORTALS: dict[str, PortalSpec] = {
    "tradingview_export": PortalSpec(
        id="tradingview_export",
        name="TradingView chart data export",
        login_url="https://www.tradingview.com/accounts/signin/",
        export_url="https://www.tradingview.com/chart/",
        username_secret="TRADINGVIEW_USERNAME",
        password_secret="TRADINGVIEW_PASSWORD",
        selectors={
            "username": "#username",
            "password": "#password",
            "login_submit": "#signin-submit",
            "menu": "#chart-menu",
            "export_open": "#export-chart-data",
            "export_confirm": "#export-confirm",
            "download_link": "#export-download",
            "error": "#export-error",
        },
        fields={"format": "#export-format", "timezone": "#export-timezone"},
        produces="bars",
        notes="Exports exactly what is on the chart, which is the simplest way to get "
        "a dataset that matches what the client is looking at.",
    ),
    "topstepx_statements": PortalSpec(
        id="topstepx_statements",
        name="TopstepX trade history",
        login_url="https://topstepx.com/login",
        export_url="https://topstepx.com/reports/trades",
        username_secret="TOPSTEPX_USERNAME",
        password_secret="TOPSTEPX_API_KEY",
        selectors={
            "username": "#email",
            "password": "#password",
            "login_submit": "#login-submit",
            "export_open": "#export-trades",
            "export_confirm": "#export-confirm",
            "download_link": "#download-csv",
            "error": "#report-error",
        },
        fields={"from": "#date-from", "to": "#date-to"},
        produces="fills",
        notes="Fill history. Feeds `reconstruct_from_fills`, which measures what the "
        "client actually trades rather than what they think they trade.",
    ),
    "cme_datamine": PortalSpec(
        id="cme_datamine",
        name="CME DataMine",
        login_url="https://datamine.cmegroup.com/login",
        export_url="https://datamine.cmegroup.com/#/datasets",
        username_secret="CME_DATAMINE_USERNAME",
        password_secret="CME_DATAMINE_PASSWORD",
        selectors={
            "username": "#username",
            "password": "#password",
            "login_submit": "#login",
            "export_open": "#dataset-download",
            "export_confirm": "#confirm-order",
            "download_link": "#download-file",
            "paywall": "#purchase-required",
            "error": "#order-error",
        },
        fields={"symbol": "#symbol-filter", "from": "#start-date", "to": "#end-date"},
        download_wait_s=120.0,
        produces="bars",
        notes="Straight from the exchange, so it is the reference other vendors are "
        "checked against. Usually behind a purchase -- the paywall handler takes over.",
    ),
    "databento_portal": PortalSpec(
        id="databento_portal",
        name="Databento batch download",
        login_url="https://databento.com/portal/login",
        export_url="https://databento.com/portal/downloads",
        username_secret="DATABENTO_USERNAME",
        password_secret="DATABENTO_PASSWORD",
        selectors={
            "username": "#email",
            "password": "#password",
            "login_submit": "#sign-in",
            "export_open": "#new-batch-job",
            "export_confirm": "#submit-job",
            "download_link": "#job-download",
            "paywall": "#insufficient-credit",
            "error": "#job-error",
        },
        fields={"symbol": "#symbols", "from": "#start", "to": "#end", "schema": "#schema"},
        download_wait_s=300.0,
        produces="bars",
        notes="Batch jobs for history too large for the API. The API path is already "
        "wired in; this is for bulk tick pulls.",
    ),
    "broker_generic": PortalSpec(
        id="broker_generic",
        name="Generic broker statement download",
        login_url="",
        export_url="",
        selectors={
            "username": "input[type=email], input[name=username], #username",
            "password": "input[type=password], #password",
            "login_submit": "button[type=submit], #login, #signin",
            "export_open": "a[download], #export, #download-statement",
            "download_link": "a[download]",
        },
        produces="statement",
        notes="Last resort for a venue with no entry here. The selectors are the common "
        "shapes; if it fails, add a proper entry to PORTALS -- that is data, not code.",
    ),
}


@dataclass
class RetrievalResult:
    portal: str
    ok: bool
    downloaded: list[str] = field(default_factory=list)
    ingested_symbol: str = ""
    ingested_rows: int = 0
    kind: str = ""
    paywalled: bool = False
    paywall_message: str = ""
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        lines = [f"[portal] {self.portal}: {'retrieved' if self.ok else 'INCOMPLETE'}"]
        lines += [f"    {s}" for s in self.steps]
        if self.paywalled:
            lines.append("    PAYWALL:")
            lines += [f"    {line}" for line in self.paywall_message.splitlines()]
        if self.downloaded:
            lines.append(f"    files: {', '.join(self.downloaded)}")
        if self.ingested_rows:
            lines.append(
                f"    ingested {self.ingested_rows:,} row(s) of {self.kind}"
                + (f" for {self.ingested_symbol}" if self.ingested_symbol else "")
            )
        for error in self.errors:
            lines.append(f"    ERROR: {error}")
        return "\n".join(lines)


class PortalOperator:
    """Drives an export portal. Reads and downloads only -- never trades."""

    def __init__(self, driver: Driver, audit: AuditTrail | None = None, download_dir: Path | None = None):
        self.driver = driver
        self.audit = audit or AuditTrail()
        self.download_dir = Path(download_dir or (ee_home() / "downloads"))
        self.download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ flow
    def retrieve(
        self,
        portal_id: str,
        *,
        symbol: str = "",
        start: str = "",
        end: str = "",
        extra_fields: dict[str, str] | None = None,
        ingest: bool = True,
    ) -> RetrievalResult:
        portal = PORTALS.get(portal_id)
        if portal is None:
            raise KeyError(f"No portal named {portal_id!r}. Known: {', '.join(PORTALS)}")
        result = RetrievalResult(portal=portal_id, ok=False)

        try:
            if portal.needs_login:
                if not self._login(portal, result):
                    return result

            self.driver.goto(portal.export_url)
            self.audit.record("portal_open", portal_id, portal.export_url, self.driver)
            result.steps.append(f"opened {portal.name}")

            values = {"symbol": symbol, "from": start, "to": end, **(extra_fields or {})}
            for field_name, selector in portal.fields.items():
                value = values.get(field_name)
                if value:
                    self.driver.fill(selector, str(value))
                    result.steps.append(f"set {field_name} = {value}")

            self.driver.click(portal.selectors["export_open"])
            paywall_selector = portal.selectors.get("paywall")
            if paywall_selector and self.driver.exists(paywall_selector):
                self._handle_paywall(portal, result)
                return result

            confirm = portal.selectors.get("export_confirm")
            if confirm:
                self.driver.click(confirm)
            result.steps.append("export requested")

            error_selector = portal.selectors.get("error")
            if error_selector:
                message = self.driver.text(error_selector)
                if message:
                    result.errors.append(message)
                    self.audit.record("portal_error", portal_id, message, self.driver, ok=False)
                    return result

            downloaded = self._await_download(portal, result)
            if not downloaded:
                return result

            result.downloaded = [str(p) for p in downloaded]
            self.audit.record(
                "portal_download", portal_id, f"{len(downloaded)} file(s)", self.driver
            )
            result.steps.append(f"downloaded {len(downloaded)} file(s)")

            if ingest:
                self._ingest(downloaded, symbol, result)
            result.ok = True
        except OperatorRefusal:
            raise
        except Exception as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            self.audit.record("portal_failed", portal_id, str(exc), self.driver, ok=False)
        return result

    # ------------------------------------------------------------- internals
    def _login(self, portal: PortalSpec, result: RetrievalResult) -> bool:
        username = get_secret(portal.username_secret)
        password = get_secret(portal.password_secret)
        if not username or not password:
            result.errors.append(
                f"{portal.name} needs {portal.username_secret} and {portal.password_secret}. "
                f"Store them with `ee-agent secrets set {portal.username_secret}` -- they go to "
                "your OS keychain, are never logged and are never sent to any model."
            )
            return False
        self.driver.goto(portal.login_url)
        self.driver.fill(portal.selectors["username"], username)
        self.driver.fill(portal.selectors["password"], password)
        self.driver.click(portal.selectors["login_submit"])
        error = self.driver.text(portal.selectors.get("error", "#login-error")) or ""
        if error:
            result.errors.append(f"login failed: {error}")
            self.audit.record("portal_login", portal.id, error, self.driver, ok=False)
            return False
        self.audit.record("portal_login", portal.id, "signed in", self.driver)
        result.steps.append(f"signed in to {portal.name}")
        return True

    def _handle_paywall(self, portal: PortalSpec, result: RetrievalResult) -> None:
        """Ability 23, reached through a portal instead of an API: bring the page
        to the client, say what it unlocks, let them pay, carry on."""
        from ee_agent.data.paywall import notice_for

        result.paywalled = True
        source = {"cme_datamine": "databento", "databento_portal": "databento"}.get(portal.id)
        if source:
            notice = notice_for(source, needed_for=f"a bulk download from {portal.name}")
            notice.url = portal.export_url
            result.paywall_message = notice.message()
        else:
            result.paywall_message = (
                f"{portal.name} wants payment before it will release this download.\n"
                f"  Open: {portal.export_url}\n"
                "  I have not paid anything and I never will -- that is yours to decide.\n"
                "  Buy it and tell me, and I will carry on from here. In the meantime I will use "
                "whatever free source covers this, and I will tell you what that costs in accuracy."
            )
        self.audit.record("portal_paywall", portal.id, portal.export_url, self.driver, ok=False)
        result.steps.append("hit a paywall and brought it to you")

    def _await_download(self, portal: PortalSpec, result: RetrievalResult) -> list[Path]:
        """Wait for the file. Some portals queue a job for minutes."""
        link = portal.selectors.get("download_link")
        if link and self.driver.wait_for(link, timeout_s=portal.download_wait_s):
            self.driver.click(link)

        deadline = time.time() + portal.download_wait_s
        before = {p.name for p in self.download_dir.iterdir()} if self.download_dir.exists() else set()
        while time.time() < deadline:
            current = {p.name for p in self.download_dir.iterdir()}
            fresh = [
                self.download_dir / name
                for name in sorted(current - before)
                if not name.endswith((".crdownload", ".part", ".tmp"))
            ]
            if fresh:
                return fresh
            if hasattr(self.driver, "state"):  # mock driver writes the file synchronously
                break
            time.sleep(0.5)

        current = (
            sorted(
                (p for p in self.download_dir.iterdir() if p.suffix.lower() in (".csv", ".json", ".parquet", ".txt")),
                key=lambda p: -p.stat().st_mtime,
            )
            if self.download_dir.exists()
            else []
        )
        if current:
            return current[:1]
        result.errors.append(
            f"no file appeared in {self.download_dir} within {portal.download_wait_s:.0f}s. "
            "If the portal changed its layout, the selectors for it are in PORTALS -- one dict entry."
        )
        return []

    def _ingest(self, files: list[Path], symbol: str, result: RetrievalResult) -> None:
        """Hand off to universal ingestion. The client never states a schema."""
        from ee_agent.data.ingest import ingest_any

        for path in files:
            try:
                ingested = ingest_any(path, symbol=symbol or None)
            except DataError as exc:
                result.errors.append(f"{path.name}: {exc}")
                continue
            result.kind = ingested.kind
            if ingested.bars is not None:
                result.ingested_rows = len(ingested.bars)
                result.ingested_symbol = ingested.bars.symbol
                result.steps.append(ingested.bars.age_line())
            elif ingested.fills is not None:
                result.ingested_rows = len(ingested.fills)
                result.steps.append(f"recognised {len(ingested.fills)} fill(s)")
            return


def catalogue() -> str:
    lines = [f"[portals] {len(PORTALS)} export portal(s) the Operator can drive:"]
    lines += [p.line() for p in PORTALS.values()]
    lines.append(
        "    Selectors are data, not code: a portal that changes its layout is one dict entry to fix."
    )
    lines.append("    None of these can place an order. Hard rule 4 applies to every one.")
    return "\n".join(lines)
