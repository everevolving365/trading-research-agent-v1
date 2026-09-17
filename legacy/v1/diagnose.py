"""
diagnose.py — 10-second health check for the EverEvolving Trading Agent.

Verifies, in order:
  1. Python version
  2. All Python dependencies installed
  3. Internet reachable
  4. Binance.com reachable (likely 451 from US — that's OK)
  5. Binance.US reachable (the actual crypto source)
  6. yfinance can return 1-min data (stock/ETF fallback)
  7. Polygon key present + valid (if set)
  8. Databento key present (if set)
  9. Gemini key present + responds

Run:  python diagnose.py
"""

from __future__ import annotations

import os
import sys


PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def line(label: str, status: str, detail: str = "") -> None:
    print(f"  {status}  {label:<38} {detail}")


def check_python() -> None:
    v = sys.version_info
    ok = v >= (3, 11)
    line(f"Python version (need 3.11+)",
         PASS if ok else FAIL,
         f"{v.major}.{v.minor}.{v.micro}")


def check_deps() -> None:
    deps = [
        ("google.generativeai", "google-generativeai"),
        ("polygon", "polygon-api-client"),
        ("databento", "databento"),
        ("yfinance", "yfinance"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("matplotlib", "matplotlib"),
        ("dotenv", "python-dotenv"),
        ("requests", "requests"),
        ("pygments", "pygments"),
    ]
    missing = []
    for mod, pkg in deps:
        try:
            __import__(mod)
            line(f"dep: {pkg}", PASS)
        except ImportError:
            missing.append(pkg)
            line(f"dep: {pkg}", FAIL, "not installed")
    if missing:
        print()
        print(f"  ↳ Fix: pip install {' '.join(missing)}")


def check_internet() -> bool:
    try:
        import requests
        r = requests.get("https://www.google.com", timeout=10)
        ok = r.status_code == 200
        line("Internet reachable", PASS if ok else FAIL, f"HTTP {r.status_code}")
        return ok
    except Exception as e:
        line("Internet reachable", FAIL, str(e)[:60])
        return False


def check_binance(host: str) -> None:
    try:
        import requests
        r = requests.get(
            f"{host}/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 1},
            timeout=15,
        )
        if r.status_code == 200:
            line(f"{host}", PASS, "BTCUSDT klines reachable")
        elif r.status_code == 451:
            line(f"{host}", WARN, "HTTP 451 geo-blocked (expected for binance.com in US)")
        else:
            line(f"{host}", FAIL, f"HTTP {r.status_code}")
    except Exception as e:
        line(f"{host}", FAIL, str(e)[:60])


def check_yfinance() -> None:
    try:
        import yfinance as yf
        df = yf.download("AAPL", period="1d", interval="1m", progress=False, auto_adjust=False)
        if df is None or df.empty:
            line("yfinance 1-min (AAPL)", FAIL, "returned empty frame")
        else:
            line("yfinance 1-min (AAPL)", PASS, f"{len(df)} rows in last 1d")
    except ImportError:
        line("yfinance 1-min (AAPL)", FAIL, "yfinance not installed")
    except Exception as e:
        line("yfinance 1-min (AAPL)", FAIL, str(e)[:80])


def check_polygon() -> None:
    key = os.getenv("POLYGON_API_KEY", "").strip()
    if not key:
        line("POLYGON_API_KEY", WARN, "not set — stocks/ETFs will use yfinance (7d cap), forex disabled")
        return
    try:
        import requests
        r = requests.get(
            "https://api.polygon.io/v3/reference/tickers/AAPL",
            params={"apiKey": key},
            timeout=15,
        )
        if r.status_code == 200:
            line("Polygon REST key", PASS, "valid")
        elif r.status_code in (401, 403):
            line("Polygon REST key", FAIL, "rejected — key may be invalid or wrong tier")
        else:
            line("Polygon REST key", WARN, f"HTTP {r.status_code}")
    except Exception as e:
        line("Polygon REST key", FAIL, str(e)[:80])


def check_databento() -> None:
    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key:
        line("DATABENTO_API_KEY", WARN, "not set — futures disabled")
        return
    line("DATABENTO_API_KEY", PASS, "present (not test-fetching to avoid burning credits)")


def check_gemini() -> None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        line("GEMINI_API_KEY", FAIL, "not set — agent will not start")
        return
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        candidates = [
            os.getenv("GEMINI_MODEL", "").strip() or None,
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-flash-latest",
        ]
        candidates = [c for c in candidates if c]
        last_err = None
        for name in candidates:
            try:
                model = genai.GenerativeModel(name)
                resp = model.generate_content("Reply with just the word PONG.")
                text = (getattr(resp, "text", "") or "").strip().upper()
                if "PONG" in text:
                    line("Gemini API", PASS, f'model "{name}" responded "{text[:20]}"')
                else:
                    line("Gemini API", WARN, f'model "{name}" responded {text[:40]!r}')
                break
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "404" in msg or "not found" in msg or "not supported" in msg:
                    continue
                line("Gemini API", FAIL, f'{name}: {str(e)[:80]}')
                break
        else:
            line("Gemini API", FAIL, f"all candidates 404'd: {last_err}")
    except Exception as e:
        line("Gemini API", FAIL, str(e)[:80])


def main() -> None:
    print()
    print("=" * 60)
    print("  EverEvolving Trading Agent — Health Check")
    print("=" * 60)

    # Load .env so the API key checks see the actual values
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass

    print("\n[1] Python & dependencies")
    check_python()
    check_deps()

    print("\n[2] Connectivity")
    if not check_internet():
        print("\nNo internet — skipping remaining checks.")
        return

    print("\n[3] Crypto sources (Binance)")
    check_binance("https://api.binance.com")
    check_binance("https://api.binance.us")

    print("\n[4] Stock/ETF fallback (yfinance)")
    check_yfinance()

    print("\n[5] API keys")
    check_polygon()
    check_databento()
    check_gemini()

    print()
    print("=" * 60)
    print("  Done. Send this entire output to debug.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
