"""
BacktestRunner — write Gemini-generated code to disk, run it via subprocess,
parse stdout for the standardized metrics, persist results.txt.

Subprocess (not exec) for safer execution + clean stdout/stderr capture.
On error, optionally round-trip back to Gemini for an automated fix.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from .gemini_client import GeminiClient


METRIC_PATTERNS = {
    "total_return":  re.compile(r"Total Return:\s*([-+]?\d+\.?\d*)\s*%", re.IGNORECASE),
    "win_rate":      re.compile(r"Win Rate:\s*([-+]?\d+\.?\d*)\s*%", re.IGNORECASE),
    "max_drawdown":  re.compile(r"Max Drawdown:\s*([-+]?\d+\.?\d*)\s*%", re.IGNORECASE),
    "total_trades":  re.compile(r"Total Trades:\s*(\d+)", re.IGNORECASE),
    "sharpe_ratio":  re.compile(r"Sharpe Ratio:\s*([-+]?\d+\.?\d*)", re.IGNORECASE),
}


class BacktestRunner:

    def __init__(self, gemini: GeminiClient):
        self.gemini = gemini

    def write_code(self, code: str, code_path: str) -> None:
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)

    def run(self, code_path: str, timeout: int = 120) -> dict:
        """Execute the script. Returns dict: {returncode, stdout, stderr}."""
        try:
            proc = subprocess.run(
                [sys.executable, code_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
            }
        except subprocess.TimeoutExpired as e:
            return {
                "returncode": -1,
                "stdout": e.stdout or "",
                "stderr": f"Backtest exceeded {timeout}s timeout.",
            }

    @staticmethod
    def parse_metrics(stdout: str) -> dict:
        out: dict = {}
        for key, pattern in METRIC_PATTERNS.items():
            match = pattern.search(stdout)
            if match:
                raw = match.group(1)
                out[key] = int(raw) if key == "total_trades" else float(raw)
        return out

    def render_results(self, symbol: str, strategy: str, df_meta: dict,
                       metrics: dict, chart_path: str) -> str:
        lines = []
        lines.append("\n" + "═" * 60)
        lines.append(f"   BACKTEST RESULTS — {symbol}")
        lines.append("═" * 60)
        lines.append(f"  Strategy:     {strategy}")
        lines.append(f"  Date Range:   {df_meta['start_date']} → {df_meta['end_date']}")
        lines.append(f"  Total Rows:   {df_meta['total_rows']:,} 1-min bars")
        lines.append("─" * 60)
        lines.append(f"  Total Return:     {self._fmt_pct(metrics.get('total_return'))}")
        lines.append(f"  Win Rate:         {self._fmt_pct(metrics.get('win_rate'), force_unsigned=True)}")
        lines.append(f"  Max Drawdown:     {self._fmt_pct(metrics.get('max_drawdown'))}")
        lines.append(f"  Total Trades:     {metrics.get('total_trades', 'n/a')}")
        lines.append(f"  Sharpe Ratio:     {self._fmt_num(metrics.get('sharpe_ratio'))}")
        lines.append("─" * 60)
        lines.append(f"  Chart saved to:   {chart_path}")
        lines.append("═" * 60)
        return "\n".join(lines)

    def save_results_txt(self, content: str, results_path: str) -> None:
        with open(results_path, "w", encoding="utf-8") as f:
            f.write(content)

    def request_fix(self, broken_code: str, error_message: str) -> str:
        """Hand the error back to Gemini and return the proposed fix."""
        return self.gemini.debug_backtest_code(broken_code, error_message)

    # ----- formatting helpers ------------------------------------------

    @staticmethod
    def _fmt_pct(value, force_unsigned: bool = False) -> str:
        if value is None:
            return "n/a"
        if force_unsigned:
            return f"{value:.2f}%"
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}%"

    @staticmethod
    def _fmt_num(value) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}"
