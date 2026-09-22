"""A driver for the mock export portals.

Extends the Operator's mock page driver with the one thing a portal does that a
chart page does not: put a file on disk when you click download. That makes the
whole retrieval path -- login, fields, export, paywall, download, ingestion --
testable with no account and no network.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ee_agent.operator.browser import MockPageDriver, _selector_key


class MockPortalDriver(MockPageDriver):
    def __init__(
        self,
        pages_dir: Path,
        download_dir: Path,
        page: str = "export.html",
        produces: str = "bars",
        rows: int = 240,
    ):
        super().__init__(pages_dir)
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.page = page
        self.produces = produces
        self.rows = rows
        self.downloads: list[Path] = []
        self.state["logged_in"] = False

    def goto(self, url: str) -> None:
        self.url = url
        path = self.pages_dir / self.page
        self._page = path.read_text(encoding="utf-8") if path.exists() else f"<html>{url}</html>"

    def click(self, selector: str) -> None:
        key = _selector_key(selector)
        if key == "login-submit":
            self.state["logged_in"] = bool(
                self.state.get("field:username") and self.state.get("field:password")
            )
            self.state["login_error"] = "" if self.state["logged_in"] else "missing credentials"
            return
        if key in ("export-download", "download-csv", "download-file", "job-download"):
            self.downloads.append(self._write_file())
            return
        super().click(selector)

    def text(self, selector: str) -> str:
        key = _selector_key(selector)
        if key == "login-error":
            return self.state.get("login_error", "")
        if key in ("export-error", "report-error", "order-error", "job-error"):
            return self.state.get(f"error:{key}", "")
        return super().text(selector)

    def exists(self, selector: str) -> bool:
        return f'id="{_selector_key(selector)}"' in self._page

    def wait_for(self, selector: str, timeout_s: float = 30.0) -> bool:
        return self.exists(selector)

    def _write_file(self) -> Path:
        """What the portal actually hands you: a file with its own column names,
        which universal ingestion has to work out unaided."""
        index = len(self.downloads) + 1
        if self.produces == "fills":
            path = self.download_dir / f"trades_export_{index}.csv"
            stamps = pd.date_range("2026-03-02 09:35", periods=self.rows, freq="20min")
            frame = pd.DataFrame(
                {
                    "Filled at": stamps.strftime("%Y-%m-%d %H:%M:%S"),
                    "Instrument": ["MNQ"] * self.rows,
                    "B/S": ["Buy", "Sell"] * (self.rows // 2),
                    "Filled Qty": [1] * self.rows,
                    "Avg Price": [20000 + (i % 7) * 5 for i in range(self.rows)],
                }
            )
        else:
            path = self.download_dir / f"chart_export_{index}.csv"
            stamps = pd.date_range("2026-03-02 08:30", periods=self.rows, freq="5min")
            base = [20000 + (i % 40) * 1.25 for i in range(self.rows)]
            frame = pd.DataFrame(
                {
                    "Gmt time": stamps.strftime("%d.%m.%Y %H:%M:%S"),
                    "Open": base,
                    "High": [b + 3.5 for b in base],
                    "Low": [b - 3.5 for b in base],
                    "Close": [b + 1.0 for b in base],
                    "Volume": [500 + (i % 11) * 25 for i in range(self.rows)],
                }
            )
        frame.to_csv(path, index=False)
        return path
