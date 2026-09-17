"""
StrategyEngine — bridge between user English and runnable Python.

Build a fully-specified prompt for Gemini, return the generated code as a
string, and display it (optionally syntax-highlighted) to the user.
Execution is handled by backtest_runner — NOT here.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

from .gemini_client import GeminiClient


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKTESTS_DIR = os.path.join(PROJECT_ROOT, "backtests")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")


class StrategyEngine:

    def __init__(self, gemini: GeminiClient):
        self.gemini = gemini

    def build_run_paths(self, symbol: str) -> dict:
        """Allocate filesystem paths for this run. Caller writes into these."""
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(BACKTESTS_DIR, f"{symbol}_{ts}")
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(RESULTS_DIR, exist_ok=True)
        return {
            "timestamp": ts,
            "run_dir": run_dir,
            "code_path": os.path.join(run_dir, "strategy_code.py"),
            "results_path": os.path.join(run_dir, "results.txt"),
            "chart_path": os.path.join(RESULTS_DIR, f"{symbol}_{ts}_equity_curve.png"),
        }

    def generate(self, strategy_description: str, df: pd.DataFrame,
                 symbol: str, csv_path: str, chart_path: str) -> str:
        info = self._summarize_data(df, symbol, csv_path, chart_path)
        code = self.gemini.generate_backtest_code(strategy_description, info)
        return code

    def display_code(self, code: str) -> None:
        print()
        print("=" * 60)
        print("  GEMINI-GENERATED BACKTEST CODE")
        print("=" * 60)
        try:
            from pygments import highlight
            from pygments.lexers import PythonLexer
            from pygments.formatters import TerminalFormatter
            print(highlight(code, PythonLexer(), TerminalFormatter()))
        except Exception:
            print(code)
        print("=" * 60)

    # ----- internals ----------------------------------------------------

    @staticmethod
    def _summarize_data(df: pd.DataFrame, symbol: str,
                        csv_path: str, chart_path: str) -> dict:
        start_date = pd.to_datetime(df["Date"].iloc[0]).strftime("%Y-%m-%d %H:%M UTC")
        end_date = pd.to_datetime(df["Date"].iloc[-1]).strftime("%Y-%m-%d %H:%M UTC")
        sample_rows = df.head(5).to_string(index=False)

        # Forward slashes are safer inside a generated Python string literal,
        # especially on Windows.
        return {
            "symbol": symbol,
            "total_rows": len(df),
            "start_date": start_date,
            "end_date": end_date,
            "sample_rows": sample_rows,
            "csv_file_path": csv_path.replace("\\", "/"),
            "chart_output_path": chart_path.replace("\\", "/"),
        }
