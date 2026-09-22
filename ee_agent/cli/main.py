"""``ee-agent`` -- the product surface.

Extremely user friendly, with no loss of capability (ability 82). Every command
works with zero credentials against the committed fixtures; the ones that need a
key say which key and where to get it, then carry on with what they can do.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path



VERSION = "2.2.0"

BANNER = r"""
  EverEvolving Trading Agent  v{version}
  Your strategy. Your risk. Proven to be the same object everywhere.
"""


def _spec_arg(value: str):
    """Accept a path, a library id, or a bare name under library/."""
    from ee_agent.library import loader
    from ee_agent.spec.model import StrategySpec

    path = Path(value)
    if path.exists():
        return StrategySpec.load(path)
    for candidate in (Path("library") / value / "spec.yaml", Path(value) / "spec.yaml"):
        if candidate.exists():
            return StrategySpec.load(candidate)
    try:
        return loader.get(value).load_spec()
    except Exception as exc:
        raise SystemExit(f"Cannot find a spec at {value!r}: {exc}")


def _load(symbol: str, timeframe: str, fixtures: bool, days: int = 365):
    from ee_agent.data.loader import load_bars

    return load_bars(symbol, timeframe, lookback_days=days, fixtures_only=fixtures, use_cache=not fixtures)


# ================================================================= commands
def cmd_demo(args) -> int:
    """The free-tier demo: a real backtest before the client enters a single key."""
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.parity.harness import run_parity
    from ee_agent.spec.model import StrategySpec

    print(BANNER.format(version=VERSION))
    print("Running the demo on committed fixture data. No keys, no network, no cost.\n")
    spec = StrategySpec.load(Path(__file__).resolve().parents[2] / "tests/fixtures/specs/sweep-return-atr.yaml")
    bars = _load(args.symbol, args.timeframe, fixtures=True)
    print()
    report = TruthEngine(spec, monte_carlo_paths=400, synthetic_paths=3).analyze(bars)
    print(report.report())
    print()
    print(run_parity(spec, bars.slice(0, min(4000, len(bars)))).report())
    print(
        "\nThat is the whole loop: your rules -> one spec -> Python, Pine indicator, Pine strategy "
        "and live config, all proven identical.\n"
        "Next: `ee-agent capture` to describe your own strategy out loud or in text."
    )
    return 0


def cmd_chat(args) -> int:
    """Open-ended conversation that can operate the whole agent."""
    from ee_agent.conversation.agent import run_repl

    print(BANNER.format(version=VERSION))
    return run_repl(voice=args.voice, provider=args.provider)


def cmd_sources(args) -> int:
    """Search for where to get data for anything the agent has no source for."""
    from ee_agent.data.discovery import discover_sources

    result = discover_sources(args.query, asset_class=args.asset_class, use_web=not args.offline)
    print(result.summary())
    return 0


def cmd_options(args) -> int:
    """Option chains and single-contract candles."""
    from ee_agent.data.options import load_chain, load_option_bars
    from ee_agent.instruments.options import option_instrument, parse_option

    if args.action == "chain":
        chain = load_chain(args.target, expiries=args.expiries, fixtures_only=args.fixtures)
        if args.tradeable:
            print()
            print(chain.tradeable_only().summary(limit=args.limit))
        if args.delta is not None:
            row = chain.by_delta(args.delta, right=args.right)
            if row:
                print()
                print(
                    f"[pick] nearest {args.delta:g}-delta {args.right}: {row.contract.readable} "
                    f"-- delta {row.greeks.delta:.3f}, bid {row.bid:.2f}, ask {row.ask:.2f}, "
                    f"OI {row.open_interest:,.0f}, {'tradeable' if row.tradeable else 'NOT tradeable'}"
                )
                print(f"       symbol: {row.contract.occ}")
        return 0

    if args.action == "contract":
        contract = parse_option(args.target)
        instrument = option_instrument(contract)
        print(f"[option] {contract.readable}")
        print(f"         OCC symbol   {contract.occ}")
        print(f"         multiplier   {instrument.multiplier:g}  (1 tick = ${instrument.tick_value:.2f})")
        print(f"         settlement   {instrument.extra['settlement']}")
        print(f"         correlates   {instrument.correlation_group} (follows the underlying)")
        print(f"         expires in   {contract.days_to_expiry()} day(s)")
        bars = load_option_bars(contract, args.timeframe, fixtures_only=args.fixtures)
        print(f"         {len(bars):,} candle(s) available for backtesting")
        return 0
    return 1


def cmd_screenshots(args) -> int:
    """Read marked-up chart screenshots and propose a spec to confirm."""
    from ee_agent.capture.vision import read_charts, spec_from_screenshots

    intake = read_charts(args.paths)
    print(intake.summary())
    if not intake.seen:
        return 0
    spec = spec_from_screenshots(intake, strategy_id=args.id)
    print()
    print("[proposal] every item below is UNAPPROVED until you confirm it:")
    for assumption in spec.assumptions:
        print(f"    [{assumption.id}] {assumption.question}")
        print(f"        I saw: {assumption.resolution}")
        if assumption.sensitivity.get("if_wrong"):
            print(f"        if wrong: {assumption.sensitivity['if_wrong']}")
    if args.out:
        spec.save(args.out)
        print()
        print(f"[saved] {args.out} -- blocked from live trading until the assumptions are approved.")
    return 0


def cmd_portal(args) -> int:
    """Drive an export portal and hand what comes back to ingestion."""
    from pathlib import Path as _Path

    from ee_agent.operator.browser import AuditTrail, MockPageDriver, PlaywrightDriver
    from ee_agent.operator.portals import PortalOperator, catalogue

    if args.action == "list":
        print(catalogue())
        return 0
    if args.live:
        driver = PlaywrightDriver(headless=args.headless)
    else:
        root = _Path(__file__).resolve().parents[2] / "tests/fixtures/portals"
        print(f"[portal] running against the local mock pages at {root} (no network, no account).")
        from tests.portal_driver import MockPortalDriver

        from ee_agent.paths import ee_home

        driver = MockPortalDriver(root, ee_home() / "downloads")
    operator = PortalOperator(driver, AuditTrail())
    result = operator.retrieve(args.portal, symbol=args.symbol or "", start=args.start or "", end=args.end or "")
    print(result.summary())
    return 0 if result.ok else 1


def cmd_capture(args) -> int:
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.capture.parser import parse
    from ee_agent.capture.voice import VoiceShell, status as voice_status
    from ee_agent.library import loader
    from ee_agent.spec.validator import validate

    shell = VoiceShell(enabled=args.voice)
    if args.voice:
        print(voice_status().summary())

    if args.transcript:
        text = Path(args.transcript).read_text(encoding="utf-8")
    else:
        shell.say(
            "Tell me about your strategy. Take as long as you like, in any order -- I will ask about "
            "anything you leave out. Finish with an empty line."
        )
        chunks = []
        while True:
            try:
                line = input("  ")
            except EOFError:
                break
            if not line.strip():
                break
            chunks.append(line)
        text = "\n".join(chunks)

    if not text.strip():
        print("Nothing captured.")
        return 1

    parsed = parse(text, strategy_id=args.id, name=args.name or args.id.replace("-", " ").title())
    print()
    print(parsed.summary())
    print()

    engine = InterrogationEngine(parsed.spec, sink=shell.say if args.voice else print)
    if args.no_questions:
        spec = engine.run(lambda q: None)
    else:
        spec = engine.run(lambda q: (shell.ask("") if args.voice else input("  > ")))
    print()
    print(engine.ledger_report())
    print()
    report = validate(spec)
    print(report or "spec valid")
    out = Path(args.out or f"{spec.id}.yaml")
    spec.save(out)
    print(f"\n[saved] {out}  (spec hash {spec.short_hash})")
    if loader.should_offer():
        offer = loader.offer_text()
        if offer:
            print("\n" + offer)
            loader.mark_offered()
    return 0 if report.ok else 2


def cmd_compile(args) -> int:
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
    from ee_agent.compile.to_python import emit_script

    spec = _spec_arg(args.spec)
    out = Path(args.out or f"generated/{spec.id}")
    out.mkdir(parents=True, exist_ok=True)
    indicator = compile_to_pine_indicator(spec)
    strategy = compile_to_pine_strategy(spec)
    live = compile_to_live(spec)
    indicator.save(out / f"{spec.id}.pine")
    strategy.save(out / f"{spec.id}-STRATEGY.pine")
    live.save(out / f"{spec.id}-live.json")
    (out / f"{spec.id}_backtest.py").write_text(emit_script(spec), encoding="utf-8")
    print(f"[compile] {spec.id} -> {out}")
    print(f"    spec hash  {spec.hash}")
    print(f"    plan hash  {live.plan_hash}")
    print(f"    {spec.id}.pine              (arrow indicator, with alertconditions)")
    print(f"    {spec.id}-STRATEGY.pine     (Strategy Tester / Deep Backtesting)")
    print(f"    {spec.id}-live.json         (live config, same plan hash)")
    print(f"    {spec.id}_backtest.py       (standalone, reviewable, sandbox-ready)")
    for note in indicator.limitations:
        print(f"    [pine limitation] {note}")
    if not indicator.limitations:
        print("    No Pine limitations: the emission is an exact expression of the spec.")
    return 0


def cmd_analyze(args) -> int:
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.research.tearsheet import write_tearsheet

    spec = _spec_arg(args.spec)
    bars = _load(args.symbol or spec.universe.instruments[0], args.timeframe, args.fixtures)
    report = TruthEngine(
        spec,
        monte_carlo_paths=args.monte_carlo,
        synthetic_paths=args.synthetic,
        prop_firm=args.firm,
        n_variants_tested=args.variants,
    ).analyze(bars)
    print(report.report())
    if args.tearsheet:
        paths = write_tearsheet(report)
        print(f"[tearsheet] {paths.html}")
    return 0


def cmd_parity(args) -> int:
    from ee_agent.parity.harness import run_parity

    spec = _spec_arg(args.spec)
    failures = 0
    for symbol in args.symbols or spec.universe.instruments:
        try:
            bars = _load(symbol, args.timeframe, args.fixtures)
        except Exception as exc:
            print(f"[parity] {symbol}: no data ({exc})")
            continue
        if args.bars:
            bars = bars.slice(0, args.bars)
        result = run_parity(spec, bars)
        print(result.report())
        failures += 0 if result.agreed else 1
    return 0 if failures == 0 else 1


def cmd_replay(args) -> int:
    from ee_agent.execution.autonomy import KillSwitchConfig
    from ee_agent.execution.replay import ReplayHarness

    spec = _spec_arg(args.spec)
    bars = _load(args.symbol or spec.universe.instruments[0], args.timeframe, args.fixtures)
    harness = ReplayHarness(
        spec,
        autonomy_level=args.level,
        prop_firm=args.firm,
        kill_config=KillSwitchConfig(
            daily_loss_usd=args.daily_loss, consecutive_losses=args.consecutive_losses
        ),
        sink=print if args.verbose else (lambda _m: None),
    )
    result = harness.run(bars, max_sessions=args.sessions)
    print(result.summary())
    return 0


def cmd_operator(args) -> int:
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
    from ee_agent.operator.browser import AuditTrail, MockPageDriver, PlaywrightDriver
    from ee_agent.operator.tradingview import TradingViewOperator

    spec = _spec_arg(args.spec)
    if args.live:
        print(
            "[operator] Driving your real browser. Deep Backtesting needs TradingView Premium; "
            "if the account is not Premium I will say so and use the regular backtest rather than "
            "passing one off as the other."
        )
        driver = PlaywrightDriver(headless=args.headless, user_data_dir=args.profile)
    else:
        root = Path(__file__).resolve().parents[2] / "tests/fixtures/tradingview"
        print(f"[operator] Running against the local mock page set at {root} (no network, no account).")
        driver = MockPageDriver(root)
    operator = TradingViewOperator(driver, AuditTrail())
    result = operator.full_flow(
        compile_to_pine_indicator(spec),
        compile_to_pine_strategy(spec),
        compile_to_live(spec),
        webhook_url=args.webhook or "",
        deep_range=(args.start or "", args.end or ""),
    )
    print(result.summary())
    print(operator.audit.summary())
    return 0 if result.ok else 1


def cmd_overnight(args) -> int:
    from ee_agent.research.autonomy import run_overnight

    spec = _spec_arg(args.spec)
    bars = _load(args.symbol or spec.universe.instruments[0], args.timeframe, args.fixtures)
    report = run_overnight(
        spec, bars, max_variants=args.variants, monte_carlo_paths=args.monte_carlo,
        synthetic_paths=args.synthetic,
    )
    print()
    print(report.morning_brief())
    return 0


def cmd_scan(args) -> int:
    from ee_agent.research.autonomy import cross_asset_scan

    spec = _spec_arg(args.spec)
    report = cross_asset_scan(
        spec, symbols=args.symbols, timeframe=args.timeframe, fixtures_only=args.fixtures,
        monte_carlo_paths=args.monte_carlo,
    )
    print(report.summary())
    return 0


def cmd_secrets(args) -> int:
    from ee_agent.secrets.vault import KNOWN_SECRETS, vault

    v = vault()
    if args.action == "list":
        print(f"{'SECRET':<24}{'STATUS':<12}{'BACKEND':<16}UNLOCKS")
        for status in v.describe():
            mark = "set" if status.present else "-"
            print(f"{status.name:<24}{mark:<12}{status.backend:<16}{status.unlocks}")
        print("\nBring your own key: this product ships with none. Nothing above is required for "
              "backtesting, compiling, parity or ingestion.")
        return 0
    if args.action == "set":
        import getpass

        if args.name not in KNOWN_SECRETS:
            print(f"Unknown secret {args.name!r}. Known: {', '.join(KNOWN_SECRETS)}")
            return 1
        value = getpass.getpass(f"{args.name} (input hidden, never logged): ")
        backend = v.set(args.name, value)
        print(f"Stored in the {backend}. Remove it with: ee-agent secrets delete {args.name}")
        return 0
    if args.action == "delete":
        v.delete(args.name)
        print(f"{args.name} removed from every backend.")
        return 0
    return 1


def cmd_data(args) -> int:
    from ee_agent.data.cache import BarCache
    from ee_agent.data.source_registry import capability_matrix

    if args.action == "sources":
        print(capability_matrix())
        return 0
    if args.action == "ingest":
        from ee_agent.data.ingest import ingest_any

        result = ingest_any(args.path, symbol=args.symbol)
        print(result.summary())
        if result.bars is not None:
            print(result.bars.age_line())
        if result.fills is not None:
            from ee_agent.capture.intake import reconstruct_from_fills, spec_from_fills

            behaviour = reconstruct_from_fills(result.fills)
            print(behaviour.summary())
            if args.out:
                spec = spec_from_fills(behaviour)
                spec.save(args.out)
                print(f"[saved] {args.out} -- every inferred rule is an UNAPPROVED assumption.")
        return 0
    if args.action == "show":
        for meta in BarCache().describe(args.symbol):
            print(json.dumps(meta, indent=2))
        return 0
    if args.action == "refresh":
        removed = BarCache().invalidate(args.symbol)
        print(f"[cache] cleared {removed} file(s) for {args.symbol}")
        return 0
    return 1


def cmd_research(args) -> int:
    from ee_agent.research.index import summary

    print(summary(limit=args.limit))
    return 0


def cmd_ledger(args) -> int:
    from ee_agent.ledger.signals import SignalLedger

    ledger = SignalLedger()
    if args.action == "verify":
        print(ledger.verify().summary())
        print(f"[anchor] {ledger.anchor_line()}")
        return 0 if ledger.verify().valid else 1
    if args.action == "track":
        print(json.dumps(ledger.track_record(args.spec), indent=2))
        return 0
    if args.action == "proof":
        print(json.dumps(ledger.proof_for(args.seq), indent=2))
        return 0
    return 1


def cmd_library(args) -> int:
    from ee_agent.library import loader

    print(loader.catalogue())
    print("\nThe default path is always your own strategy: `ee-agent capture`.")
    return 0


def cmd_status(args) -> int:
    from ee_agent.cost.notifier import ledger as cost_ledger
    from ee_agent.data.source_registry import capability_matrix
    from ee_agent.execution.ledger import ledger as position_ledger
    from ee_agent.ledger.signals import SignalLedger
    from ee_agent.capture.voice import status as voice_status
    from ee_agent.secrets.vault import vault

    print(BANNER.format(version=VERSION))
    present = [s.name for s in vault().describe() if s.present]
    print(f"[keys] {len(present)} credential(s) present: {', '.join(present) or 'none -- zero-cost floor'}")
    print(voice_status().summary())
    print(position_ledger().summary())
    print(SignalLedger().verify().summary())
    print(cost_ledger().banner())
    print()
    print(capability_matrix())
    return 0


def cmd_verify(args) -> int:
    from ee_agent.verify import run_all

    return run_all(quick=args.quick)


def cmd_wizard(args) -> int:
    from ee_agent.cli.wizard import run_wizard

    return run_wizard()


# ================================================================== parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ee-agent",
        description="Your strategy. Your risk. Proven to be the same object everywhere.",
    )
    p.add_argument("--version", action="version", version=f"ee-agent {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="a real backtest on free data, before you enter any key")
    d.add_argument("--symbol", default="MNQ")
    d.add_argument("--timeframe", default="5m")
    d.set_defaults(func=cmd_demo)

    w = sub.add_parser("wizard", help="first-run setup")
    w.set_defaults(func=cmd_wizard)

    ch = sub.add_parser("chat", help="talk to it about anything; it can run every tool it has")
    ch.add_argument("--voice", action="store_true", help="speak and listen (optional; loses nothing when off)")
    ch.add_argument("--provider", help="anthropic | openai | gemini | none (default: whichever key is present)")
    ch.set_defaults(func=cmd_chat)

    op = sub.add_parser("options", help="option chains, greeks and single-contract candles")
    op.add_argument("action", choices=["chain", "contract"])
    op.add_argument("target", help="underlying for a chain (SPY), or a contract (SPY 580 call 2026-12-18)")
    op.add_argument("--expiries", type=int, default=3)
    op.add_argument("--limit", type=int, default=10)
    op.add_argument("--delta", type=float, help="pick the contract nearest this delta")
    op.add_argument("--right", choices=["call", "put"], default="call")
    op.add_argument("--tradeable", action="store_true", help="drop illiquid contracts")
    op.add_argument("--timeframe", default="5m")
    op.add_argument("--fixtures", action="store_true", help="offline synthetic chain, no key")
    op.set_defaults(func=cmd_options)

    sh = sub.add_parser("screenshots", help="read marked-up chart screenshots and infer the rules")
    sh.add_argument("paths", nargs="+", help="image files")
    sh.add_argument("--id", default="from-screenshots-v1")
    sh.add_argument("--out", help="write the spec proposal here")
    sh.set_defaults(func=cmd_screenshots)

    po = sub.add_parser("portal", help="retrieve data from an export portal or statement download")
    po.add_argument("action", choices=["list", "get"])
    po.add_argument("portal", nargs="?", default="", help="portal id, see `ee-agent portal list`")
    po.add_argument("--symbol")
    po.add_argument("--start")
    po.add_argument("--end")
    po.add_argument("--live", action="store_true", help="real browser instead of the mock pages")
    po.add_argument("--headless", action="store_true")
    po.set_defaults(func=cmd_portal)

    so = sub.add_parser("sources", help="search for where to get data for any asset")
    so.add_argument("query", help="e.g. '1-minute copper futures history'")
    so.add_argument("--asset-class", dest="asset_class", help="future | crypto | equity | forex | index | option")
    so.add_argument("--offline", action="store_true", help="catalogue only, no web search")
    so.set_defaults(func=cmd_sources)

    c = sub.add_parser("capture", help="describe your strategy; I interrogate it until it is unambiguous")
    c.add_argument("--transcript", help="read the description from a file instead of asking")
    c.add_argument("--id", default="my-strategy-v1")
    c.add_argument("--name", default="")
    c.add_argument("--out", help="where to write the spec (default <id>.yaml)")
    c.add_argument("--voice", action="store_true", help="speak and listen (optional; loses nothing when off)")
    c.add_argument("--no-questions", action="store_true", help="skip the interrogation (assumptions stay UNAPPROVED)")
    c.set_defaults(func=cmd_capture)

    co = sub.add_parser("compile", help="spec -> Python, Pine indicator, Pine strategy, live config")
    co.add_argument("spec")
    co.add_argument("--out")
    co.set_defaults(func=cmd_compile)

    a = sub.add_parser("analyze", help="the truth engine: costs, in/out of sample, Monte Carlo, lookahead, the case against")
    a.add_argument("spec")
    a.add_argument("--symbol")
    a.add_argument("--timeframe", default="5m")
    a.add_argument("--fixtures", action="store_true", help="use committed fixtures (no network, no keys)")
    a.add_argument("--monte-carlo", type=int, default=2000, dest="monte_carlo")
    a.add_argument("--synthetic", type=int, default=8)
    a.add_argument("--variants", type=int, default=1, help="how many variants you tested, for the selection correction")
    a.add_argument("--firm", default="topstep")
    a.add_argument("--tearsheet", action="store_true")
    a.set_defaults(func=cmd_analyze)

    pa = sub.add_parser("parity", help="prove all four targets are the same strategy")
    pa.add_argument("spec")
    pa.add_argument("--symbols", nargs="*")
    pa.add_argument("--timeframe", default="5m")
    pa.add_argument("--bars", type=int, help="limit the window")
    pa.add_argument("--fixtures", action="store_true")
    pa.set_defaults(func=cmd_parity)

    r = sub.add_parser("replay", help="recorded history through the LIVE path, in accelerated time")
    r.add_argument("spec")
    r.add_argument("--symbol")
    r.add_argument("--timeframe", default="5m")
    r.add_argument("--sessions", type=int, default=5)
    r.add_argument("--level", type=int, default=4, help="autonomy level 1-5")
    r.add_argument("--firm", default=None)
    r.add_argument("--daily-loss", type=float, default=1000.0, dest="daily_loss")
    r.add_argument("--consecutive-losses", type=int, default=4, dest="consecutive_losses")
    r.add_argument("--fixtures", action="store_true")
    r.add_argument("--verbose", action="store_true")
    r.set_defaults(func=cmd_replay)

    o = sub.add_parser("operator", help="drive TradingView: paste, save, test, deep backtest, alerts")
    o.add_argument("spec")
    o.add_argument("--live", action="store_true", help="use a real browser (needs playwright + your account)")
    o.add_argument("--headless", action="store_true")
    o.add_argument("--profile", help="attach to an already-logged-in browser profile instead of storing a password")
    o.add_argument("--webhook")
    o.add_argument("--start")
    o.add_argument("--end")
    o.set_defaults(func=cmd_operator)

    ov = sub.add_parser("overnight", help="generate variants, test out of sample, report only survivors")
    ov.add_argument("spec")
    ov.add_argument("--symbol")
    ov.add_argument("--timeframe", default="5m")
    ov.add_argument("--variants", type=int, default=12)
    ov.add_argument("--monte-carlo", type=int, default=500, dest="monte_carlo")
    ov.add_argument("--synthetic", type=int, default=3)
    ov.add_argument("--fixtures", action="store_true")
    ov.set_defaults(func=cmd_overnight)

    sc = sub.add_parser("scan", help="one strategy across the whole instrument universe")
    sc.add_argument("spec")
    sc.add_argument("--symbols", nargs="*")
    sc.add_argument("--timeframe", default="5m")
    sc.add_argument("--monte-carlo", type=int, default=300, dest="monte_carlo")
    sc.add_argument("--fixtures", action="store_true", default=True)
    sc.set_defaults(func=cmd_scan)

    s = sub.add_parser("secrets", help="bring your own key: store, list, remove")
    s.add_argument("action", choices=["list", "set", "delete"])
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_secrets)

    da = sub.add_parser("data", help="sources, ingestion, cache")
    da.add_argument("action", choices=["sources", "ingest", "show", "refresh"])
    da.add_argument("path", nargs="?")
    da.add_argument("--symbol")
    da.add_argument("--out")
    da.set_defaults(func=cmd_data)

    re_ = sub.add_parser("research", help="the research index: every run ever, and what it cost")
    re_.add_argument("--limit", type=int, default=20)
    re_.set_defaults(func=cmd_research)

    le = sub.add_parser("ledger", help="the verified signal ledger")
    le.add_argument("action", choices=["verify", "track", "proof"])
    le.add_argument("--spec")
    le.add_argument("--seq", type=int, default=1)
    le.set_defaults(func=cmd_ledger)

    li = sub.add_parser("library", help="the founder's library (your own strategy is the default)")
    li.set_defaults(func=cmd_library)

    st = sub.add_parser("status", help="what is set up, what is running, what it has cost")
    st.set_defaults(func=cmd_status)

    v = sub.add_parser("verify", help="run every acceptance check for every completed phase")
    v.add_argument("--quick", action="store_true")
    v.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[stopped]")
        return 130
    except Exception as exc:  # user-facing: no tracebacks unless asked
        if "--debug" in (argv or sys.argv):
            raise
        print(f"\n[error] {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
