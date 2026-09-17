"""
GeminiClient — the ONLY module that talks to Gemini.

Model: gemini-1.5-flash (fast, cheap, capable for codegen + classification).
Responsibilities: classify_asset, generate_backtest_code, interpret_results,
debug_backtest_code.
"""

from __future__ import annotations

import os
import time

import google.generativeai as genai


VALID_CATEGORIES = {"STOCK", "CRYPTO", "FOREX", "FUTURES", "ETF", "INDEX", "UNKNOWN"}


class GeminiClient:

    # Tried in order until one succeeds. gemini-1.5-* was retired from v1beta;
    # the current production family is 2.x. `gemini-flash-latest` is Google's
    # rolling alias as a final safety net.
    DEFAULT_MODEL_CANDIDATES = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-flash-latest",
    ]

    def __init__(self, model_name: str | None = None):
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is required. Add it to your .env file. "
                "Get a key at https://aistudio.google.com"
            )
        genai.configure(api_key=api_key)

        override = os.getenv("GEMINI_MODEL", "").strip()
        if model_name:
            self._candidates = [model_name]
        elif override:
            self._candidates = [override]
        else:
            self._candidates = list(self.DEFAULT_MODEL_CANDIDATES)

        self._working_model = None
        self._working_name: str | None = None

    # ----- public API ---------------------------------------------------

    def classify_asset(self, user_input: str) -> str:
        prompt = (
            "You are a financial asset classifier. The user has entered: "
            f"\"{user_input}\".\n"
            "Classify this into exactly ONE of these categories: "
            "STOCK, CRYPTO, FOREX, FUTURES, ETF, INDEX, UNKNOWN.\n"
            "Respond with ONLY the category word. No explanation. No punctuation. "
            "Just the category."
        )
        raw = self._call(prompt).strip().upper()
        # Defensive: pick the first valid token if the model overruns.
        for token in raw.replace("\n", " ").split():
            cleaned = token.strip(".,;:!?\"'`")
            if cleaned in VALID_CATEGORIES:
                return cleaned
        return "UNKNOWN"

    def generate_backtest_code(self, strategy_description: str, sample_data_info: dict) -> str:
        prompt = self._build_codegen_prompt(strategy_description, sample_data_info)
        code = self._call(prompt)
        return self._strip_markdown_fences(code)

    def debug_backtest_code(self, broken_code: str, error_message: str) -> str:
        prompt = (
            "The following Python backtest script raised an error. "
            "Fix the bug and return the COMPLETE corrected script. "
            "Return ONLY raw Python — no markdown, no commentary.\n\n"
            f"--- ERROR ---\n{error_message}\n\n"
            f"--- CODE ---\n{broken_code}"
        )
        return self._strip_markdown_fences(self._call(prompt))

    def interpret_results(self, results_dict: dict) -> str:
        prompt = (
            "You are a quantitative trading analyst. The following are backtest results. "
            "Write a concise 3-5 sentence plain English interpretation. "
            "Mention what worked, what didn't, and any obvious risk signal.\n\n"
            "STRICT NUMERICAL RULES:\n"
            "1. When you reference a metric, quote it EXACTLY as given — same digits, "
            "same decimal places, same sign. Do not round, simplify, or rescale.\n"
            "2. If a value is 0.01%, write '0.01%' — NEVER '1%' or 'about 1%'.\n"
            "3. If a value is -0.02%, write '-0.02%' — NEVER '2%' or 'around 2%'.\n"
            "4. Do not invent metrics that are not in the data.\n"
            "5. If a metric looks unrealistic (e.g. tiny return + high Sharpe), "
            "flag that the numbers may indicate a position-sizing or formula issue, "
            "not a real edge.\n\n"
            "No bullet points. No markdown. Just prose.\n\n"
            f"DATA:\n{results_dict}"
        )
        return self._call(prompt).strip()

    # ----- internals ----------------------------------------------------

    def _call(self, prompt: str, retries: int = 1) -> str:
        """Iterate candidate models on 404 / not-found; retry transient errors."""
        last_err: Exception | None = None

        if self._working_model is not None:
            models_to_try = [(self._working_name, self._working_model)]
        else:
            models_to_try = [(name, genai.GenerativeModel(name)) for name in self._candidates]

        for name, model in models_to_try:
            for attempt in range(retries + 1):
                try:
                    resp = model.generate_content(prompt)
                    if self._working_model is None:
                        print(f"[GEMINI] Using model: {name}")
                        self._working_model = model
                        self._working_name = name
                    return getattr(resp, "text", "") or ""
                except Exception as e:
                    last_err = e
                    msg = str(e).lower()
                    # Model-not-found → skip to next candidate immediately.
                    if "404" in msg or "not found" in msg or "not supported" in msg:
                        break
                    if attempt < retries:
                        time.sleep(3)
                        continue
                    break

        raise RuntimeError(f"Gemini API call failed across all candidates: {last_err}")

    @staticmethod
    def _strip_markdown_fences(code: str) -> str:
        s = code.strip()
        if s.startswith("```"):
            # remove opening fence (optionally ```python)
            first_newline = s.find("\n")
            if first_newline != -1:
                s = s[first_newline + 1 :]
            if s.endswith("```"):
                s = s[:-3]
        return s.strip() + "\n"

    @staticmethod
    def _build_codegen_prompt(strategy_description: str, info: dict) -> str:
        return f"""You are an expert quantitative trading developer. The user described a trading strategy in plain English. Write a complete, working Python backtest script for it.

USER'S STRATEGY:
"{strategy_description}"

DATA INFORMATION:
- Symbol: {info['symbol']}
- Total rows: {info['total_rows']}
- Date range: {info['start_date']} to {info['end_date']}
- Columns: Date, Symbol, Open, High, Low, Close, Volume  (1-minute bars)
- Sample rows:
{info['sample_rows']}

═══════════════════════════════════════════════════════════
MANDATORY RULES — NON-NEGOTIABLE
═══════════════════════════════════════════════════════════

R1. POSITION SIZING (this is the most common bug — read carefully):
    - INITIAL_CAPITAL = 100_000 (USD)
    - For EVERY trade, position size = floor(INITIAL_CAPITAL / entry_price) shares/contracts/units.
      This means each trade deploys ~all capital. Do NOT default to 1 share.
    - If the user explicitly specifies a risk-per-trade rule (e.g. "risk 1% per trade"),
      override with: shares = floor( (INITIAL_CAPITAL * risk_pct) / sl_distance ).
    - Track equity in DOLLARS, not in points.
    - Always print position size at least once for debugging:
        print(f"# Position size: {{position_size}} units at ${{entry_price:.2f}}")

R2. "POINTS" DEFINITION (depends on the asset):
    - For STOCKS, ETFs, INDICES, FUTURES, CRYPTO: 1 point = 1 unit of the quote price.
      Example: TP=20 points on QQQ at $480 means TP = $500.
    - For FOREX: 1 point = 1 pip = 0.0001 for most pairs, 0.01 for JPY pairs.
    - When in doubt, treat "points" as absolute price units of the quote currency.

R3. TP / SL EXECUTION (check intrabar):
    - On every bar AFTER entry, check if bar's High >= TP_level OR bar's Low <= SL_level.
    - If BOTH would fire in same bar, exit at SL (conservative).
    - Long trades: TP = entry + tp_distance, SL = entry - sl_distance.
    - Short trades: TP = entry - tp_distance, SL = entry + sl_distance.
    - If the user's strategy has no TP/SL, exit only on the opposite signal.

R4. SHARPE RATIO — compute on DAILY equity returns (NOT per-minute):
    - Resample equity curve to daily frequency: daily_equity = equity_series.resample('1D').last().dropna()
    - daily_returns = daily_equity.pct_change().dropna()
    - sharpe = (daily_returns.mean() / daily_returns.std()) * sqrt(252)
    - If daily_returns is empty or std is 0, set sharpe = 0.0.
    - Do NOT annualize by sqrt(252*390) — that's the per-minute formula and inflates Sharpe.

R5. MAX DRAWDOWN — percent of peak equity:
    - equity_array = np.array(equity_curve, dtype=float)
    - cumulative_max = np.maximum.accumulate(equity_array)
    - drawdown_pct = (equity_array - cumulative_max) / cumulative_max
    - max_drawdown = drawdown_pct.min() * 100   # will be a negative number

R6. TOTAL RETURN — percent of initial capital:
    - total_return = ((final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100

R7. WIN RATE — only on closed trades:
    - win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0

R8. ENTRY CADENCE — distinguish time-based from signal-based entries:

    TIME-BASED entries — language like "enter at every 1H close", "trade every N minutes",
    "buy at the open of each session", "place a trade every bar":
      - A NEW trade fires at EVERY occurrence of the entry condition.
      - If a trade is already open when the next entry condition fires, FIRST close the
        existing trade at the current bar's close (time exit), record its P&L, then open
        the new trade at the same bar's close.
      - This produces many overlapping/back-to-back trades. That's correct.

    SIGNAL-BASED entries — language like "when EMA crosses", "when RSI > 70",
    "if price breaks above resistance":
      - A new trade fires ONLY when the signal occurs.
      - If a trade is already open and the same signal re-fires, IGNORE the new signal
        (don't pyramid). The existing trade exits only via TP, SL, or an opposite signal.
      - If the user describes an explicit opposite signal (e.g. "sell when EMA crosses
        back below"), use that as the exit signal in addition to TP/SL.

    Default to TIME-BASED if the user phrases entry with a recurring schedule
    ("every", "each", "at the close of every"). Default to SIGNAL-BASED otherwise.

═══════════════════════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════════════════════

O1. Load data from: "{info['csv_file_path']}"
    Parse the Date column as datetime, set as the index, sort ascending.

O2. Print exactly these five metric lines, in this exact format, one per line:
    Total Return: X.XX%
    Win Rate: X.XX%
    Max Drawdown: -X.XX%
    Total Trades: X
    Sharpe Ratio: X.XX

O3. Save equity curve chart to: "{info['chart_output_path']}"
    Use matplotlib.use('Agg') BEFORE importing pyplot.
    Title: "{info['symbol']} — Backtest Equity Curve"
    Plot equity_curve over time. Add gridlines.

O4. Libraries: only pandas, numpy, matplotlib, os. No installs, no other deps.

O5. Standalone — zero user input, no input() calls.

O6. Brief comments explaining each section. No verbose docstrings.

O7. Handle edges: empty signals, single-trade case, division by zero, insufficient data for daily Sharpe.

═══════════════════════════════════════════════════════════
RETURN FORMAT
═══════════════════════════════════════════════════════════

Return ONLY raw Python code. No markdown fences. No commentary before or after. Start with imports.
"""
