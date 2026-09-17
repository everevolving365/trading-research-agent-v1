"""The first-run wizard (ability 81).

Walks the client through obtaining each key they want -- and makes very clear
that they need none of them to reach a real backtest.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ee_agent.capture.voice import status as voice_status
from ee_agent.data.source_registry import CAPABILITIES
from ee_agent.paths import ee_home
from ee_agent.secrets.vault import KNOWN_SECRETS, vault

STEPS = [
    (
        "model",
        ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"],
        "Conversation, the interrogation engine and the adversarial pass get better with a model.",
        "Without one: the interrogation still runs from its checklist, and the adversarial pass "
        "still computes every finding -- a model only argues them in nicer prose.",
    ),
    (
        "data",
        ["POLYGON_API_KEY", "DATABENTO_API_KEY"],
        "Paid data unlocks stocks, forex, and CME futures with real order flow.",
        "Without either: crypto is keyless and free, any file you already have can be ingested, "
        "and every paid fetcher runs against recorded fixtures.",
    ),
    (
        "tradingview",
        ["TRADINGVIEW_USERNAME", "TRADINGVIEW_PASSWORD"],
        "Your TradingView login lets the Operator paste scripts, run the Strategy Tester and set "
        "up alerts on your own account.",
        "Without it: everything compiles and backtests here. You can also attach to an "
        "already-logged-in browser profile so nothing is stored at all.",
    ),
    (
        "execution",
        ["TOPSTEPX_USERNAME", "TOPSTEPX_API_KEY", "TOPSTEPX_ACCOUNT_ID"],
        "TopstepX credentials are needed only to place real orders.",
        "Without them: the replay harness runs your strategy through the identical live code path "
        "on recorded history, and paper mode runs indefinitely.",
    ),
]


def run_wizard(input_fn=input, sink=print) -> int:
    v = vault()
    sink("\n  EverEvolving Trading Agent -- first run\n" + "  " + "-" * 60)
    sink(
        "  Bring your own key. This product ships with nobody's credentials, and the owner\n"
        "  carries no per-user cost. You can skip every single step below and still reach a\n"
        "  real backtest -- that is the zero-cost floor, and it is deliberate.\n"
    )

    for name, secrets, why, without in STEPS:
        have = [s for s in secrets if v.get(s)]
        sink(f"  [{name}] {why}")
        sink(f"      {without}")
        if have:
            sink(f"      Already set: {', '.join(have)}\n")
            continue
        answer = (input_fn("      Set one now? [y/N] ") or "").strip().lower()
        if not answer.startswith("y"):
            sink("      Skipped.\n")
            continue
        for secret in secrets:
            unlocks, where = KNOWN_SECRETS[secret]
            sink(f"      {secret}: {unlocks}")
            sink(f"      Get it at: {where}")
            entered = _prompt_secret(secret, input_fn)
            if entered:
                backend = v.set(secret, entered)
                sink(f"      Stored in the {backend}. Remove it any time: ee-agent secrets delete {secret}")
            else:
                sink("      Skipped.")
        sink("")

    sink("  [voice] " + voice_status().summary().replace("\n", "\n  "))
    sink(
        "\n  Voice is a shell. With it switched off you lose nothing at all -- every capability in\n"
        "  this system is reachable from text."
    )

    config = Path("config/client.yaml")
    if not config.exists():
        example = Path("config/client.example.yaml")
        if example.exists():
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            sink(f"\n  [config] wrote {config} from the example. Everything in it has a working default.")

    sink(f"\n  [home] artifacts, runs and the research index live in {ee_home()}")
    sink(
        "\n  You are ready. Two ways to start:\n"
        "      ee-agent demo        a real backtest on fixture data, right now, costing nothing\n"
        "      ee-agent capture     describe your own strategy and I will interrogate it\n"
    )
    return 0


def _prompt_secret(name: str, input_fn) -> str:
    if input_fn is input:
        import getpass

        try:
            return getpass.getpass(f"      {name} (hidden, never logged): ").strip()
        except Exception:  # pragma: no cover - no tty
            return ""
    return (input_fn(f"      {name}: ") or "").strip()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_wizard())
