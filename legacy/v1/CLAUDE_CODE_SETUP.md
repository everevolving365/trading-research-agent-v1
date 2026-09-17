# Setup instructions — for the recipient's Claude Code

> **Hey Claude Code:** The human who just shared this repo with you wants this trading agent running on their machine. Follow these steps in order. Don't skip the API-key gate — the bot will not start without at least a Gemini key.

## What this is

EverEvolving Trading Agent — a CLI Python research bot. It fetches 1-minute OHLCV data for any asset (crypto, stocks, ETFs, indices, forex, futures), takes a plain-English strategy description from the user, uses Google Gemini to generate a Python backtest, runs it in subprocess, and returns metrics + equity curve.

## Setup sequence — execute these steps

### 1. Verify prerequisites

```bash
python --version     # need 3.11+
git --version
```

If Python isn't installed, install Python 3.11+ from python.org. Halt and tell the user. Don't continue.

### 2. Install dependencies

From inside the cloned repo directory:

```bash
pip install -r requirements.txt
```

### 3. Set up the .env file

```bash
cp .env.example .env       # macOS/Linux
copy .env.example .env     # Windows
```

Then **prompt the human** for which API keys they have. They need at minimum a Gemini key. Edit `.env` to add their keys:

- `GEMINI_API_KEY` — **mandatory.** Get free at https://aistudio.google.com
- `POLYGON_API_KEY` — optional, unlocks stocks/ETFs/indices beyond 7 days + forex. Free tier at https://polygon.io/dashboard/api-keys
- `DATABENTO_API_KEY` — optional, unlocks futures. Paid only at https://databento.com

Do NOT proceed without at least the Gemini key.

### 4. Run the diagnostic

```bash
python diagnose.py
```

This verifies every component: Python version, dependencies, internet, Binance.US reachability, yfinance, every API key. Read the output. If anything besides Polygon/Databento is red, debug it before continuing.

### 5. Launch the agent

```bash
python main.py
```

At the prompts, type an asset (e.g. `Bitcoin`, `AAPL`, `SPY`, `EUR/USD`, `ES futures`), days of data, then describe a trading strategy in plain English. The bot will show the generated Python code before executing it — the human must type `yes` to confirm before any backtest runs.

## Where data is stored

- Cached price data: `data/{SYMBOL}/{SYMBOL}_1min.csv`
- Generated backtest code: `backtests/{SYMBOL}_{TIMESTAMP}/strategy_code.py`
- Equity curve charts: `results/{SYMBOL}_{TIMESTAMP}_equity_curve.png`

## Known geo issues

If the user is in the US:
- Binance.com returns HTTP 451 — the fetcher auto-fails over to Binance.US.
- If their state also blocks Binance.US (NY/TX/HI/VT historically), the fetcher will raise a clear error. They'd need a different crypto source — tell the user and stop.

## Architecture (for context if you need to debug)

```
main.py                  → CLI loop
agent/classifier.py      → rules + Gemini fallback → asset type
agent/router.py          → asset type → correct fetcher
agent/gemini_client.py   → ALL Gemini API calls (codegen, classification, interpretation)
agent/strategy_engine.py → builds the codegen prompt, displays generated code
agent/backtest_runner.py → executes generated code in subprocess, parses metrics
fetchers/base_fetcher.py → universal data contract (Date, Symbol, OHLCV)
fetchers/{source}.py     → Polygon, Binance, Databento, yfinance
```

Every fetcher returns the same column structure. The backtest engine never knows which source the data came from.

## If something breaks

Paste the full error to the user. Common issues:
- `gemini-1.5-flash` 404 → already handled by candidate fallback; should auto-resolve to `gemini-2.5-flash`
- Polygon 403 → key is invalid or wrong tier; tell user to verify at polygon.io/dashboard
- Empty data from yfinance → market was closed or symbol invalid
- `RuntimeError` from a fetcher → read the message; it's intentionally human-readable
