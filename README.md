# EverEvolving Trading Agent

A command-line AI research agent. You type an asset (any class), it pulls 1-minute OHLCV data, you describe a strategy in plain English, Gemini turns it into a Python backtest, you run it, and you get clean metrics + a P&L equity curve.

## Prerequisites

- Python 3.11 or newer
- API keys (only the ones you need — see below)

## Installation

```
git clone <or copy this folder>
cd everevolving-trading-agent
pip install -r requirements.txt
cp .env.example .env
# edit .env and add your keys
python main.py
```

## API keys

| Key | Required for | Where to get it |
|---|---|---|
| `GEMINI_API_KEY` | Strategy generation + asset classification (mandatory) | https://aistudio.google.com |
| `POLYGON_API_KEY` | Stocks, ETFs, indices, forex (1-min REST) | https://polygon.io/dashboard/api-keys |
| `DATABENTO_API_KEY` | Futures (paid) | https://databento.com |

Binance crypto data needs **no key** — public klines endpoint is used directly.

Without `POLYGON_API_KEY`, the agent automatically falls back to **yfinance** for stocks/ETFs/indices (limit: most recent 7 days of 1-min data). Forex has no free fallback.

## Supported asset classes

| Class | Source | Key needed | Coverage |
|---|---|---|---|
| Crypto | Binance | none | All Binance spot pairs (BTCUSDT, ETHUSDT, …) |
| Stocks | Polygon (→ yfinance fallback) | Polygon or none | US listings |
| ETFs | Polygon (→ yfinance fallback) | Polygon or none | US listings |
| Indices | Polygon (→ yfinance fallback) | Polygon or none | SPX, NDX, DJI, RUT, VIX |
| Forex | Polygon | Polygon | All major + cross pairs |
| Futures | Databento (`GLBX.MDP3`) | Databento | CME futures, continuous front-month default |

## Quick example

```
> python main.py

Enter an asset: Bitcoin
How many days of 1-minute data? 7

[CACHE MISS — fetching from Binance…]
Rows fetched: 10,080

Describe your strategy: Buy when the 9-period EMA crosses above the 21-period EMA on close.
Sell when it crosses back below.

[Gemini generates Python backtest code]
[Code displayed for review]

Run this backtest? yes

════════════════════════════════════════
   BACKTEST RESULTS — BTCUSDT
════════════════════════════════════════
  Total Return:     +X.XX%
  Win Rate:         XX.XX%
  Max Drawdown:     -X.XX%
  Total Trades:     XXX
  Sharpe Ratio:     X.XX
  Chart saved to:   results/BTCUSDT_..._equity_curve.png
════════════════════════════════════════
```

## CLI commands

| Command | Action |
|---|---|
| `help` | Show command list |
| `refresh {SYMBOL}` | Delete cached data and re-fetch on next request |
| `data {SYMBOL}` | Show cache info for a symbol |
| `history` | List backtests run this session |
| `clear` | Clear screen |
| `exit` / `quit` | Leave the agent |

## How it works (one paragraph)

`main.py` runs a loop. Your input goes through `agent/classifier.py` (rules first, Gemini fallback) which assigns one of {STOCK, CRYPTO, FOREX, FUTURES, ETF, INDEX} and normalizes the symbol. `agent/router.py` picks the right fetcher under `fetchers/`. Every fetcher returns the **same** columns: `Date, Symbol, Open, High, Low, Close, Volume`. Data is cached at `data/{SYMBOL}/{SYMBOL}_1min.csv` and re-used. `agent/strategy_engine.py` ships your English description + a data summary to Gemini, which writes a runnable Python script. The script is **shown to you first**, then `agent/backtest_runner.py` executes it in a subprocess, parses the printed metrics, and renders the result block. Gemini provides a short prose interpretation. Failed backtests can be sent back to Gemini for an automated fix.

## File layout

```
everevolving-trading-agent/
├── main.py
├── .env / .env.example / requirements.txt / .gitignore
├── agent/
│   ├── classifier.py
│   ├── router.py
│   ├── gemini_client.py
│   ├── strategy_engine.py
│   └── backtest_runner.py
├── fetchers/
│   ├── base_fetcher.py
│   ├── binance_fetcher.py
│   ├── polygon_fetcher.py
│   ├── databento_fetcher.py
│   └── yfinance_fetcher.py
├── data/        # auto-created cache per symbol
├── backtests/   # per-run generated code + results.txt
└── results/     # per-run equity curve PNG
```

## Notes

- AI-generated code is **never** executed silently. You confirm every run.
- Cache is never auto-invalidated. Use `refresh {SYMBOL}` to force re-fetch.
- Subprocess timeout for any backtest run: 120 seconds.
- Chart is saved to disk only (no auto-open).
