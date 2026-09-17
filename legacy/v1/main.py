"""
EverEvolving Trading Agent — CLI entry point.

Run:  python main.py
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv


# --- environment & path setup ---------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
load_dotenv(os.path.join(HERE, ".env"))

# After .env is loaded, import modules that read env at construction time.
from agent.gemini_client import GeminiClient
from agent.classifier import Classifier
from agent.router import get_fetcher, availability_report, RoutingError
from agent.strategy_engine import StrategyEngine
from agent.backtest_runner import BacktestRunner
from fetchers.base_fetcher import BaseFetcher


SESSION_HISTORY: list[dict] = []
CURRENT_DATA: dict = {}  # populated after a successful fetch


# --- pretty printing ------------------------------------------------------

def banner() -> None:
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║        EverEvolving Trading Agent v1.0           ║")
    print("║        Powered by Gemini AI                      ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    print("Available asset classes:")
    report = availability_report()
    icons = {True: "✅", False: "❌"}
    label_pad = 14
    for asset, (source, ok, note) in report.items():
        line = f"  {icons[ok]} {asset.title().ljust(label_pad)} ({source} — {note})"
        print(line)
    print()
    gemini_ok = bool(os.getenv("GEMINI_API_KEY", "").strip())
    print(f"Gemini AI: {'✅ Connected' if gemini_ok else '❌ GEMINI_API_KEY missing'}")
    print()
    print("Type an asset name to begin, or type 'help' for commands.\n")


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def show_help() -> None:
    print()
    print("Commands:")
    print("  help              Show this help")
    print("  refresh {SYMBOL}  Delete cached data and re-fetch on next request")
    print("  data {SYMBOL}     Show info about cached data for a symbol")
    print("  history           Show backtests run this session")
    print("  clear             Clear screen")
    print("  exit / quit       Leave the agent")
    print()


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


# --- core flow ------------------------------------------------------------

def fetch_flow(classifier: Classifier) -> None:
    global CURRENT_DATA

    user_input = input("Enter an asset (e.g. AAPL, Bitcoin, ES futures, EUR/USD): ").strip()
    if not user_input:
        print("No asset entered.")
        return

    if user_input.lower() in {"exit", "quit"}:
        raise SystemExit(0)
    if user_input.lower() == "help":
        show_help()
        return
    if user_input.lower() == "clear":
        clear_screen()
        return
    if user_input.lower() == "history":
        show_history()
        return
    if user_input.lower().startswith("refresh "):
        handle_refresh(user_input.split(maxsplit=1)[1].strip())
        return
    if user_input.lower().startswith("data "):
        handle_data_info(user_input.split(maxsplit=1)[1].strip())
        return

    days_raw = input("How many days of 1-minute data do you want? (default: 30): ").strip()
    try:
        days = int(days_raw) if days_raw else 30
        if days <= 0:
            raise ValueError
    except ValueError:
        print("Invalid number — using default of 30 days.")
        days = 30

    section("CLASSIFYING ASSET")
    classified = classifier.classify(user_input)
    print(f"  Input:             {classified['original_input']}")
    print(f"  Classified type:   {classified['classified_type']}")
    print(f"  Normalized symbol: {classified['normalized_symbol']}")
    print(f"  Confidence:        {classified['confidence']}")

    try:
        fetcher, source = get_fetcher(classified)
    except RoutingError as e:
        print(f"\n[ROUTING ERROR] {e}")
        return

    section(f"FETCHING DATA ({source.upper()})")
    sym = classified["normalized_symbol"]
    try:
        df = fetcher.fetch(sym, days)
    except Exception as e:
        print(f"\n[FETCH ERROR] {e}")
        return

    csv_path = fetcher._cache_path(_cache_symbol(sym, source))
    print(f"  Rows fetched:  {len(df):,}")
    print(f"  Date range:    {df['Date'].min()} → {df['Date'].max()}")
    print(f"  Cached at:     {csv_path}")

    CURRENT_DATA = {
        "classified": classified,
        "symbol": _cache_symbol(sym, source),
        "csv_path": csv_path,
        "df": df,
        "days": days,
        "source": source,
    }


def _cache_symbol(normalized: str, source: str) -> str:
    if source == "polygon":
        return normalized.split(":")[-1].upper()
    if source == "databento":
        return normalized.split(".")[0].upper()
    return normalized.upper()


def strategy_flow(engine: StrategyEngine, runner: BacktestRunner, gemini: GeminiClient) -> None:
    if not CURRENT_DATA:
        print("No data loaded. Fetch an asset first.")
        return

    df: pd.DataFrame = CURRENT_DATA["df"]
    symbol = CURRENT_DATA["symbol"]
    csv_path = CURRENT_DATA["csv_path"]

    print()
    description = input("Describe your strategy in plain English: ").strip()
    if not description:
        print("No strategy entered.")
        return

    paths = engine.build_run_paths(symbol)

    section("GENERATING BACKTEST CODE (Gemini)")
    try:
        code = engine.generate(description, df, symbol, csv_path, paths["chart_path"])
    except Exception as e:
        print(f"\n[GEMINI ERROR] {e}")
        return

    engine.display_code(code)
    runner.write_code(code, paths["code_path"])
    print(f"\n  Code saved to: {paths['code_path']}")

    confirm = input("\nRun this backtest? (yes/no): ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Skipped. Code is saved if you want to inspect it.")
        return

    section("RUNNING BACKTEST")
    result = runner.run(paths["code_path"])

    if result["returncode"] != 0:
        print("[BACKTEST FAILED]")
        if result["stdout"]:
            print("--- stdout ---")
            print(result["stdout"])
        print("--- stderr ---")
        print(result["stderr"][-2000:])
        fix = input("\nSend the error to Gemini for debugging? (yes/no): ").strip().lower()
        if fix in {"y", "yes"}:
            try:
                fixed = runner.request_fix(code, result["stderr"])
            except Exception as e:
                print(f"[GEMINI DEBUG ERROR] {e}")
                return
            engine.display_code(fixed)
            runner.write_code(fixed, paths["code_path"])
            again = input("\nRun the fixed code? (yes/no): ").strip().lower()
            if again not in {"y", "yes"}:
                return
            result = runner.run(paths["code_path"])
            if result["returncode"] != 0:
                print("[BACKTEST STILL FAILED]")
                print(result["stderr"][-2000:])
                return
        else:
            return

    metrics = runner.parse_metrics(result["stdout"])
    df_meta = {
        "start_date": str(df["Date"].iloc[0]),
        "end_date":   str(df["Date"].iloc[-1]),
        "total_rows": len(df),
    }
    summary = runner.render_results(symbol, description, df_meta, metrics, paths["chart_path"])
    print(summary)
    runner.save_results_txt(summary, paths["results_path"])

    # Gemini interpretation
    interpretation = ""
    try:
        interpretation = gemini.interpret_results({
            "symbol": symbol,
            "strategy": description,
            "date_range": f"{df_meta['start_date']} → {df_meta['end_date']}",
            "total_rows": df_meta["total_rows"],
            **metrics,
        })
        print("\nINTERPRETATION:\n" + interpretation)
    except Exception as e:
        print(f"\n[GEMINI INTERPRET WARN] {e}")

    SESSION_HISTORY.append({
        "timestamp": paths["timestamp"],
        "symbol": symbol,
        "strategy": description,
        "metrics": metrics,
        "chart": paths["chart_path"],
    })


# --- side commands --------------------------------------------------------

def handle_refresh(symbol: str) -> None:
    if not symbol:
        print("Usage: refresh {SYMBOL}")
        return
    sym = symbol.upper()
    path = os.path.join(HERE, "data", sym, f"{sym}_1min.csv")
    if os.path.exists(path):
        os.remove(path)
        print(f"Cache cleared for {sym}.")
    else:
        print(f"No cache found for {sym}.")


def handle_data_info(symbol: str) -> None:
    if not symbol:
        print("Usage: data {SYMBOL}")
        return
    sym = symbol.upper()
    path = os.path.join(HERE, "data", sym, f"{sym}_1min.csv")
    if not os.path.exists(path):
        print(f"No cached data for {sym}.")
        return
    df = pd.read_csv(path, parse_dates=["Date"])
    print(f"  Symbol:     {sym}")
    print(f"  Rows:       {len(df):,}")
    print(f"  Date range: {df['Date'].min()} → {df['Date'].max()}")
    print(f"  Path:       {path}")


def show_history() -> None:
    if not SESSION_HISTORY:
        print("No backtests run this session.")
        return
    for i, entry in enumerate(SESSION_HISTORY, start=1):
        m = entry["metrics"]
        print(f"  {i}. [{entry['timestamp']}] {entry['symbol']} — \"{entry['strategy'][:60]}\"")
        if m:
            print(f"     ret={m.get('total_return','?')}%  trades={m.get('total_trades','?')}  sharpe={m.get('sharpe_ratio','?')}")


# --- main loop ------------------------------------------------------------

def main() -> None:
    banner()

    try:
        gemini = GeminiClient()
    except RuntimeError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)

    classifier = Classifier(gemini=gemini)
    engine = StrategyEngine(gemini=gemini)
    runner = BacktestRunner(gemini=gemini)

    fetch_flow(classifier)

    while True:
        if CURRENT_DATA:
            strategy_flow(engine, runner, gemini)
            print()
            choice = input("Test another strategy on the same data, or fetch a new asset? (same/new/exit): ").strip().lower()
            if choice in {"exit", "quit"}:
                break
            if choice == "new":
                fetch_flow(classifier)
            # 'same' or anything else → loop back into strategy_flow
        else:
            again = input("\nTry another asset? (yes/no): ").strip().lower()
            if again in {"y", "yes"}:
                fetch_flow(classifier)
            else:
                break

    print("\nGoodbye, Arinze.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye.")
    except SystemExit:
        raise
    except Exception:
        print("\n[UNEXPECTED ERROR]")
        traceback.print_exc()
        sys.exit(1)
