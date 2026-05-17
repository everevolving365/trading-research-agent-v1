"""
Classifier — turns any user string into a typed asset descriptor.

Order:
  1. Rules-based pre-classifier  (saves Gemini calls for obvious cases)
  2. Gemini fallback              (only for ambiguous strings)

Returns:
  {
    "original_input":   raw user string,
    "classified_type":  STOCK | CRYPTO | FOREX | FUTURES | ETF | INDEX | UNKNOWN,
    "normalized_symbol": the symbol formatted for the target API,
    "confidence":       "high" (rules) | "ai" (Gemini)
  }
"""

from __future__ import annotations

import re

from .gemini_client import GeminiClient


# --- known-asset dictionaries ---------------------------------------------

CRYPTO_ALIASES = {
    "BITCOIN": "BTCUSDT", "BTC": "BTCUSDT", "XBT": "BTCUSDT",
    "ETHEREUM": "ETHUSDT", "ETH": "ETHUSDT", "ETHER": "ETHUSDT",
    "SOLANA": "SOLUSDT", "SOL": "SOLUSDT",
    "RIPPLE": "XRPUSDT", "XRP": "XRPUSDT",
    "CARDANO": "ADAUSDT", "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT", "DOGECOIN": "DOGEUSDT",
    "BNB": "BNBUSDT", "BINANCECOIN": "BNBUSDT",
    "LINK": "LINKUSDT", "CHAINLINK": "LINKUSDT",
    "AVAX": "AVAXUSDT", "AVALANCHE": "AVAXUSDT",
    "MATIC": "MATICUSDT", "POLYGON": "MATICUSDT",
    "DOT": "DOTUSDT", "POLKADOT": "DOTUSDT",
    "LTC": "LTCUSDT", "LITECOIN": "LTCUSDT",
    "SHIB": "SHIBUSDT", "SHIBA": "SHIBUSDT",
    "USDT": "USDTUSD", "USDC": "USDCUSDT",
    "TRX": "TRXUSDT", "TRON": "TRXUSDT",
    "ATOM": "ATOMUSDT", "COSMOS": "ATOMUSDT",
    "NEAR": "NEARUSDT",
    "ARB": "ARBUSDT", "ARBITRUM": "ARBUSDT",
    "OP": "OPUSDT", "OPTIMISM": "OPUSDT",
    "SUI": "SUIUSDT",
    "APT": "APTUSDT", "APTOS": "APTUSDT",
    "INJ": "INJUSDT", "INJECTIVE": "INJUSDT",
    "TIA": "TIAUSDT", "CELESTIA": "TIAUSDT",
    "PEPE": "PEPEUSDT",
    "FIL": "FILUSDT", "FILECOIN": "FILUSDT",
    "ICP": "ICPUSDT",
    "HBAR": "HBARUSDT",
    "BCH": "BCHUSDT", "BITCOINCASH": "BCHUSDT",
    "XLM": "XLMUSDT", "STELLAR": "XLMUSDT",
    "ETC": "ETCUSDT", "ETHEREUMCLASSIC": "ETCUSDT",
}

FUTURES_ROOTS = {
    "ES": "ES", "MES": "MES",        # S&P 500
    "NQ": "NQ", "MNQ": "MNQ",        # Nasdaq 100
    "YM": "YM", "MYM": "MYM",        # Dow
    "RTY": "RTY", "M2K": "M2K",      # Russell 2000
    "CL": "CL", "MCL": "MCL",        # Crude Oil
    "NG": "NG",                       # Natural Gas
    "GC": "GC", "MGC": "MGC",        # Gold
    "SI": "SI", "SIL": "SIL",        # Silver
    "HG": "HG",                       # Copper
    "PL": "PL",                       # Platinum
    "PA": "PA",                       # Palladium
    "ZB": "ZB",                       # 30Y Bond
    "ZN": "ZN",                       # 10Y Note
    "ZF": "ZF",                       # 5Y Note
    "ZT": "ZT",                       # 2Y Note
    "ZC": "ZC",                       # Corn
    "ZS": "ZS",                       # Soybeans
    "ZW": "ZW",                       # Wheat
    "6E": "6E",                       # EUR/USD futures
    "6J": "6J",                       # JPY/USD futures
    "6B": "6B",                       # GBP/USD futures
    "6A": "6A",                       # AUD/USD futures
    "6C": "6C",                       # CAD/USD futures
    "BTC": "BTC", "MBT": "MBT",      # Bitcoin futures (CME)
    "ETH": "ETH", "MET": "MET",      # Ether futures (CME)
}

ETF_TICKERS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VEA", "VWO",
    "GLD", "SLV", "IAU", "GDX", "USO", "UNG",
    "TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "BND",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLU", "XLB", "XLI", "XLRE", "XLC",
    "ARKK", "SOXX", "SMH", "TQQQ", "SQQQ", "UPRO", "SPXU",
    "EEM", "EFA", "FXI", "EWZ", "EWJ", "INDA",
}

INDEX_ALIASES = {
    "SPX": "I:SPX", "S&P500": "I:SPX", "S&P 500": "I:SPX", "SP500": "I:SPX",
    "NDX": "I:NDX", "NASDAQ": "I:NDX", "NASDAQ100": "I:NDX", "NASDAQ 100": "I:NDX",
    "DJI": "I:DJI", "DOW": "I:DJI", "DJIA": "I:DJI", "DOWJONES": "I:DJI",
    "RUT": "I:RUT", "RUSSELL": "I:RUT", "RUSSELL2000": "I:RUT",
    "VIX": "I:VIX",
}

FOREX_PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "EURAUD", "EURCAD", "EURNZD", "EURCHF",
    "AUDJPY", "GBPCAD", "GBPCHF", "GBPAUD", "AUDCAD", "AUDCHF", "AUDNZD",
    "CADJPY", "CHFJPY", "NZDJPY", "USDSGD", "USDHKD", "USDMXN", "USDZAR",
}

FUTURES_PATTERN = re.compile(r"^([A-Z]{1,3})([FGHJKMNQUVXZ]\d{1,2})?$")
FOREX_PATTERN = re.compile(r"^([A-Z]{3})[/\-]?([A-Z]{3})$")


class Classifier:

    def __init__(self, gemini: GeminiClient | None = None):
        self.gemini = gemini

    def classify(self, user_input: str) -> dict:
        if not user_input or not user_input.strip():
            return self._result(user_input, "UNKNOWN", user_input, "high")

        raw = user_input.strip()
        upper = raw.upper().replace(" ", "")

        # 1) crypto by alias
        if upper in CRYPTO_ALIASES:
            return self._result(raw, "CRYPTO", CRYPTO_ALIASES[upper], "high")
        if upper.endswith("USDT") and len(upper) >= 6:
            return self._result(raw, "CRYPTO", upper, "high")

        # 2) index by alias
        clean_index = raw.upper().replace(" ", "")
        if clean_index in INDEX_ALIASES:
            return self._result(raw, "INDEX", INDEX_ALIASES[clean_index], "high")

        # 3) ETF
        if upper in ETF_TICKERS:
            return self._result(raw, "ETF", upper, "high")

        # 4) forex
        fx_match = FOREX_PATTERN.match(upper)
        if fx_match:
            pair = fx_match.group(1) + fx_match.group(2)
            if pair in FOREX_PAIRS or (fx_match.group(1) in {"EUR", "GBP", "USD", "JPY", "AUD", "CHF", "CAD", "NZD"}
                                       and fx_match.group(2) in {"EUR", "GBP", "USD", "JPY", "AUD", "CHF", "CAD", "NZD", "SGD", "HKD", "MXN", "ZAR"}):
                return self._result(raw, "FOREX", f"C:{pair}", "high")

        # 5) futures — detect root + optional contract code (e.g. ESH24)
        fut_match = FUTURES_PATTERN.match(upper)
        if fut_match:
            root = fut_match.group(1)
            if root in FUTURES_ROOTS:
                return self._result(raw, "FUTURES", FUTURES_ROOTS[root], "high")

        # phrasings like "ES futures", "NQ futures"
        compact = upper.replace("FUTURES", "").replace("FUTURE", "")
        if compact in FUTURES_ROOTS:
            return self._result(raw, "FUTURES", FUTURES_ROOTS[compact], "high")

        # 6) plain ticker 1-5 uppercase letters — assume STOCK
        if re.match(r"^[A-Z]{1,5}$", upper):
            return self._result(raw, "STOCK", upper, "high")

        # 7) fall back to Gemini
        if self.gemini is None:
            return self._result(raw, "UNKNOWN", upper, "high")

        ai_class = self.gemini.classify_asset(raw)
        normalized = self._normalize_from_ai(raw, upper, ai_class)
        return self._result(raw, ai_class, normalized, "ai")

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def _normalize_from_ai(raw: str, upper: str, ai_class: str) -> str:
        if ai_class == "CRYPTO":
            return CRYPTO_ALIASES.get(upper, upper if upper.endswith("USDT") else f"{upper}USDT")
        if ai_class == "FOREX":
            stripped = upper.replace("/", "").replace("-", "")
            return f"C:{stripped}" if not stripped.startswith("C:") else stripped
        if ai_class == "INDEX":
            return INDEX_ALIASES.get(upper, f"I:{upper}")
        if ai_class == "FUTURES":
            return FUTURES_ROOTS.get(upper, upper)
        # STOCK, ETF, UNKNOWN
        return upper

    @staticmethod
    def _result(original: str, classified: str, normalized: str, confidence: str) -> dict:
        return {
            "original_input": original,
            "classified_type": classified,
            "normalized_symbol": normalized,
            "confidence": confidence,
        }
