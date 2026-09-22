"""The tools the conversation can reach for.

This is what makes it an agent rather than a chatbot: when the client says
"pull me a year of MNQ and backtest my opening range idea", the model does not
describe how that would work -- it calls :func:`load_data` and :func:`backtest`
and reports what actually came back.

Two rules hold here, and they are enforced in code rather than in the prompt:

* **No tool places an order.** Order placement lives behind the position ledger
  and the autonomy ladder, and a conversational model does not get to reach it.
  ``tests/test_conversation.py`` greps this module for order verbs.
* **No tool invents a strategy or a risk rule.** The client owns both. Tools
  that mutate the spec only apply what the client actually said, and anything
  ambiguous becomes an unapproved assumption.
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ee_agent.cost.notifier import ledger

#: Verbs a conversational tool may never contain. Checked by a test.
FORBIDDEN = {"place_order", "submit_order", "send_order", "buy", "sell", "flatten_live", "go_live"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., Any]
    reads_only: bool = True

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict, reads_only: bool = True):
    def decorator(fn):
        REGISTRY[name] = Tool(name, description, parameters, fn, reads_only)
        return fn

    return decorator


def _schema(**props) -> dict:
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {"type": "object", "properties": props, "required": required}


# ============================================================ session state
@dataclass
class Workspace:
    """What the conversation is holding: the spec under discussion, the data
    loaded, the last result. Persisted so a session resumes cold."""

    spec: Any = None
    bars: Any = None
    last_report: Any = None
    transcript_chunks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        parts.append(f"spec: {self.spec.id + ' @ ' + self.spec.short_hash if self.spec else 'none yet'}")
        parts.append(
            f"data: {self.bars.symbol + ' ' + self.bars.timeframe + f' ({len(self.bars):,} bars)' if self.bars is not None else 'none loaded'}"
        )
        parts.append(f"last result: {'yes' if self.last_report else 'none'}")
        return " | ".join(parts)


WORKSPACE = Workspace()


def reset_workspace() -> None:
    global WORKSPACE
    WORKSPACE = Workspace()


# ================================================================== tools
@tool(
    "describe_strategy",
    "Record what the client said about their strategy and turn it into a structured spec. "
    "Call this with the client's own words, verbatim and in full -- do not summarise, do not "
    "tidy, do not fill in anything they did not say. Returns what was understood and what is "
    "still missing.",
    _schema(
        text={"type": "string", "description": "The client's own words about their strategy, verbatim.", "_required": True},
        strategy_id={"type": "string", "description": "Short id, e.g. 'opening-range-v1'."},
    ),
    reads_only=False,
)
def describe_strategy(text: str, strategy_id: str = "my-strategy-v1") -> dict:
    from ee_agent.capture.parser import parse
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.spec.validator import validate

    WORKSPACE.transcript_chunks.append(text)
    combined = "\n\n".join(WORKSPACE.transcript_chunks)
    parsed = parse(combined, strategy_id=strategy_id)
    WORKSPACE.spec = parsed.spec
    engine = InterrogationEngine(parsed.spec, sink=lambda _m: None)
    outstanding = [
        {"topic": q.topic, "question": q.text, "why": q.why} for q in engine.state.outstanding
    ]
    report = validate(parsed.spec)
    return {
        "understood": parsed.found,
        "quotes": parsed.quotes,
        "could_not_parse": parsed.missing,
        "still_to_ask": outstanding,
        "spec_id": parsed.spec.id,
        "spec_hash": parsed.spec.short_hash,
        "blocking": [str(f) for f in report.blocking],
        "note": (
            "Ask the client about every item in still_to_ask, one at a time, in their own language. "
            "Do NOT answer any of them yourself -- the client owns the strategy and the risk."
        ),
    }


@tool(
    "answer_question",
    "Apply the client's answer to one interrogation checklist item. Use the exact words they "
    "used. If they did not answer, do not call this.",
    _schema(
        topic={"type": "string", "description": "Checklist topic, e.g. 'stop_placement'.", "_required": True},
        answer={"type": "string", "description": "The client's answer, in their words.", "_required": True},
    ),
    reads_only=False,
)
def answer_question(topic: str, answer: str) -> dict:
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.spec.validator import validate

    if WORKSPACE.spec is None:
        return {"error": "No strategy captured yet. Call describe_strategy first."}
    engine = InterrogationEngine(WORKSPACE.spec, sink=lambda _m: None)
    applied = engine.apply(topic, answer)
    report = validate(WORKSPACE.spec)
    return {
        "applied": applied,
        "note": "" if applied else f"I could not turn {answer!r} into a {topic} setting. Ask them to be more specific.",
        "still_to_ask": [q.topic for q in engine.state.outstanding],
        "spec_hash": WORKSPACE.spec.short_hash,
        "blocking": [str(f) for f in report.blocking],
    }


@tool(
    "show_spec",
    "Show the current strategy spec back to the client in plain language, so they can confirm "
    "it is actually their strategy.",
    _schema(),
)
def show_spec() -> dict:
    if WORKSPACE.spec is None:
        return {"error": "No strategy captured yet."}
    from ee_agent.spec.validator import validate

    report = validate(WORKSPACE.spec)
    return {
        "plain_english": WORKSPACE.spec.describe(),
        "yaml": WORKSPACE.spec.to_yaml(),
        "spec_hash": WORKSPACE.spec.hash,
        "findings": [str(f) for f in report.findings],
        "unresolved_assumptions": [a.id for a in WORKSPACE.spec.unresolved_assumptions()],
    }


@tool(
    "edit_strategy",
    "Change the strategy from one sentence the client said, e.g. 'make the stop 1.5 ATR' or "
    "'only trade shorts'. Shows before and after.",
    _schema(sentence={"type": "string", "description": "The client's instruction.", "_required": True}),
    reads_only=False,
)
def edit_strategy(sentence: str) -> dict:
    from ee_agent.capture.intake import edit

    if WORKSPACE.spec is None:
        return {"error": "No strategy captured yet."}
    result = edit(WORKSPACE.spec, sentence)
    if result.applied:
        WORKSPACE.spec = result.after
        return {"applied": True, "change": result.change, "diff": result.diff_summary()}
    return {"applied": False, "reason": result.reason}


@tool(
    "find_data_sources",
    "Search the web for where to get market data for an asset the agent does not already know "
    "a source for. Returns candidate sources with what they cover and what they cost.",
    _schema(
        query={"type": "string", "description": "What data is needed, e.g. '1-minute copper futures history'.", "_required": True},
        asset_class={"type": "string", "description": "future | crypto | equity | forex | index | option"},
    ),
)
def find_data_sources(query: str, asset_class: str = "") -> dict:
    from ee_agent.data.discovery import discover_sources

    found = discover_sources(query, asset_class=asset_class or None)
    return found.to_dict()


@tool(
    "read_screenshots",
    "Read marked-up chart screenshots the client uploaded and report what is actually drawn on "
    "them. Returns observations plus the questions to ask -- it never turns a picture into a rule.",
    _schema(
        paths={"type": "array", "items": {"type": "string"}, "description": "Image file paths.", "_required": True},
    ),
    reads_only=False,
)
def read_screenshots(paths: list[str]) -> dict:
    from ee_agent.capture.vision import read_charts, questions_for, spec_from_screenshots

    intake = read_charts(paths)
    payload = intake.to_dict()
    payload["questions_to_ask"] = questions_for(intake)
    payload["note"] = (
        "These are OBSERVATIONS, not a strategy. Ask the client every question above before "
        "anything becomes a rule -- they own the strategy, not the screenshot."
    )
    if intake.seen:
        WORKSPACE.spec = spec_from_screenshots(intake)
        payload["spec_id"] = WORKSPACE.spec.id
    return payload


@tool(
    "retrieve_from_portal",
    "Log into an export portal or broker statement page, download the file and hand it to "
    "ingestion. Use `list` to see which portals are known. Reads and downloads only.",
    _schema(
        portal={"type": "string", "description": "Portal id, or 'list' to see them all.", "_required": True},
        symbol={"type": "string", "description": "Symbol filter, where the portal has one."},
        start={"type": "string", "description": "Start date, YYYY-MM-DD."},
        end={"type": "string", "description": "End date, YYYY-MM-DD."},
        live={"type": "boolean", "description": "True for a real browser, False for the mock pages."},
    ),
    reads_only=False,
)
def retrieve_from_portal(
    portal: str, symbol: str = "", start: str = "", end: str = "", live: bool = False
) -> dict:
    from ee_agent.operator.browser import AuditTrail, PlaywrightDriver
    from ee_agent.operator.portals import PORTALS, PortalOperator, catalogue
    from ee_agent.paths import REPO_ROOT, ee_home

    if portal in ("list", ""):
        return {"catalogue": catalogue(), "portals": sorted(PORTALS)}
    if live:
        driver = PlaywrightDriver(headless=False)
    else:
        from tests.portal_driver import MockPortalDriver

        driver = MockPortalDriver(REPO_ROOT / "tests/fixtures/portals", ee_home() / "downloads")
    operator = PortalOperator(driver, AuditTrail())
    result = operator.retrieve(portal, symbol=symbol, start=start, end=end)
    return result.to_dict()


@tool(
    "load_data",
    "Load candle data for any symbol on any asset class. Reports the source it came from, the "
    "cache age, the last bar and a data quality score.",
    _schema(
        symbol={"type": "string", "description": "e.g. MNQ, BTCUSDT, SPY, EURUSD.", "_required": True},
        timeframe={"type": "string", "description": "e.g. 1m, 5m, 15m, 1h, 1d. Default 5m."},
        days={"type": "integer", "description": "How far back. Default 365."},
        fixtures_only={"type": "boolean", "description": "True to use committed fixtures (offline, free)."},
    ),
    reads_only=False,
)
def load_data(symbol: str, timeframe: str = "5m", days: int = 365, fixtures_only: bool = False) -> dict:
    from ee_agent.data.loader import load_bars

    lines: list[str] = []
    bars = load_bars(
        symbol, timeframe, lookback_days=days, fixtures_only=fixtures_only,
        use_cache=not fixtures_only, sink=lines.append,
    )
    WORKSPACE.bars = bars
    return {
        "symbol": bars.symbol,
        "timeframe": bars.timeframe,
        "bars": len(bars),
        "first": bars.first_bar_time.isoformat() if bars.first_bar_time else None,
        "last": bars.last_bar_time.isoformat() if bars.last_bar_time else None,
        "source": bars.source,
        "quality_score": bars.quality_score,
        "has_order_flow": bars.has_flow,
        "log": lines,
    }


@tool(
    "show_data",
    "Show the client the data itself: recent bars, the range, the session structure. Use this "
    "often -- the client should always be able to see what they are working with.",
    _schema(rows={"type": "integer", "description": "How many recent bars to show. Default 10."}),
)
def show_data(rows: int = 10) -> dict:
    if WORKSPACE.bars is None:
        return {"error": "No data loaded. Call load_data first."}
    bars = WORKSPACE.bars
    tail = bars.df.tail(rows)
    return {
        "age_line": bars.age_line(),
        "recent_bars": [
            {
                "time": row["ts"].isoformat(),
                "open": round(float(row["open"]), 6),
                "high": round(float(row["high"]), 6),
                "low": round(float(row["low"]), 6),
                "close": round(float(row["close"]), 6),
                "volume": round(float(row["volume"]), 2),
            }
            for _i, row in tail.iterrows()
        ],
        "session_high": float(bars.high.max()),
        "session_low": float(bars.low.min()),
    }


@tool(
    "backtest",
    "Run the full truth engine on the captured strategy: costs on every fill, in-sample and "
    "out-of-sample, Monte Carlo band, lookahead verdict, regime breakdown, prop firm combine "
    "pass rate, and the case against the result.",
    _schema(
        monte_carlo={"type": "integer", "description": "Monte Carlo paths. Default 1000."},
        prop_firm={"type": "string", "description": "topstep | apex | takeprofit. Default topstep."},
    ),
    reads_only=False,
)
def backtest(monte_carlo: int = 1000, prop_firm: str = "topstep") -> dict:
    from ee_agent.engine.truth import TruthEngine

    if WORKSPACE.spec is None:
        return {"error": "No strategy captured yet."}
    if WORKSPACE.bars is None:
        return {"error": "No data loaded. Call load_data first."}
    report = TruthEngine(
        WORKSPACE.spec, monte_carlo_paths=monte_carlo, synthetic_paths=4, prop_firm=prop_firm
    ).analyze(WORKSPACE.bars)
    WORKSPACE.last_report = report
    return {
        "report": report.report(),
        "defensible": report.defensible,
        "survived_adversarial": report.adversarial.survived if report.adversarial else None,
        "cost_usd": round(report.cost_usd, 4),
        "artifact": report.artifact_dir,
        "note": (
            "Read the adversarial findings out to the client. Do not soften them and do not "
            "recommend a change to the strategy or the risk -- that is theirs to decide."
        ),
    }


@tool(
    "compile_indicator",
    "Compile the strategy to a TradingView arrow indicator, a Pine strategy script, a live "
    "config and a standalone Python backtest. Returns the Pine source ready to paste.",
    _schema(out_dir={"type": "string", "description": "Where to write them. Default generated/<id>."}),
    reads_only=False,
)
def compile_indicator(out_dir: str = "") -> dict:
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy

    if WORKSPACE.spec is None:
        return {"error": "No strategy captured yet."}
    spec = WORKSPACE.spec
    destination = Path(out_dir or f"generated/{spec.id}")
    destination.mkdir(parents=True, exist_ok=True)
    indicator = compile_to_pine_indicator(spec)
    strategy = compile_to_pine_strategy(spec)
    live = compile_to_live(spec)
    indicator.save(destination / f"{spec.id}.pine")
    strategy.save(destination / f"{spec.id}-STRATEGY.pine")
    live.save(destination / f"{spec.id}-live.json")
    return {
        "indicator_pine": indicator.source,
        "files": [str(p) for p in sorted(destination.iterdir())],
        "plan_hash": live.plan_hash,
        "pine_limitations": indicator.limitations or ["none -- the emission is an exact expression of the spec"],
    }


@tool(
    "prove_parity",
    "Prove the Python backtest, the Pine indicator, the Pine strategy and the live config are "
    "the same strategy, signal for signal. This is the guarantee the product is sold on.",
    _schema(),
)
def prove_parity() -> dict:
    from ee_agent.parity.harness import run_parity

    if WORKSPACE.spec is None or WORKSPACE.bars is None:
        return {"error": "Need a captured strategy and loaded data first."}
    result = run_parity(WORKSPACE.spec, WORKSPACE.bars)
    return {
        "agreed": result.agreed,
        "report": result.report(),
        "divergences": [d.explain() for d in result.divergences[:10]],
    }


@tool(
    "run_operator",
    "Drive TradingView in a real browser: log into the client's account, paste the generated "
    "scripts, save them, run the Strategy Tester, run the Deep Backtest and read the report. "
    "Reads and configures only -- it can never place an order.",
    _schema(
        live={"type": "boolean", "description": "True for a real browser, False for the local mock page set."},
        webhook={"type": "string", "description": "Webhook URL for alerts, if the client wants live signals."},
    ),
    reads_only=False,
)
def run_operator(live: bool = False, webhook: str = "") -> dict:
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
    from ee_agent.operator.browser import AuditTrail, MockPageDriver, PlaywrightDriver
    from ee_agent.operator.tradingview import TradingViewOperator
    from ee_agent.paths import REPO_ROOT

    if WORKSPACE.spec is None:
        return {"error": "No strategy captured yet."}
    driver = (
        PlaywrightDriver(headless=False)
        if live
        else MockPageDriver(REPO_ROOT / "tests/fixtures/tradingview")
    )
    operator = TradingViewOperator(driver, AuditTrail())
    spec = WORKSPACE.spec
    result = operator.full_flow(
        compile_to_pine_indicator(spec), compile_to_pine_strategy(spec), compile_to_live(spec),
        webhook_url=webhook,
    )
    return {
        "ok": result.ok,
        "steps": result.steps,
        "errors": result.errors,
        "tradingview_report": result.report.to_dict() if result.report else None,
        "audit": operator.audit.summary(),
        "live": live,
    }


@tool(
    "order_flow",
    "Read volume and order flow on the loaded data: cumulative delta, absorption, bid-ask "
    "imbalance, volume profile with point of control and value area, and the liquidity map.",
    _schema(),
)
def order_flow() -> dict:
    from ee_agent.flow.primitives import flow_report

    if WORKSPACE.bars is None:
        return {"error": "No data loaded."}
    return flow_report(WORKSPACE.bars)


@tool(
    "spending",
    "What this session has cost so far, and what each operation cost. The agent never refuses "
    "on cost grounds -- this is for the client's information.",
    _schema(),
)
def spending() -> dict:
    book = ledger()
    return {
        "total_usd": book.total_usd,
        "ceiling_usd": book.ceiling_usd,
        "by_operation": {
            op: round(sum(r.actual_usd for r in book.records if r.operation == op), 6)
            for op in sorted({r.operation for r in book.records})
        },
        "banner": book.banner(),
    }


@tool(
    "library",
    "Show the owner's own strategies, which the client may start from instead of describing "
    "their own. Offer this ONCE and never push it -- the default is always the client's own "
    "strategy.",
    _schema(entry_id={"type": "string", "description": "Load this entry as the working spec."}),
    reads_only=False,
)
def library(entry_id: str = "") -> dict:
    from ee_agent.library import loader

    if not entry_id:
        return {"catalogue": loader.catalogue(), "offer": loader.offer_text()}
    entry = loader.get(entry_id)
    WORKSPACE.spec = entry.load_spec()
    return {
        "loaded": entry_id,
        "plain_english": WORKSPACE.spec.describe(),
        "how_i_trade_it": entry.writeup() or "(the owner has not supplied the write-up yet)",
    }


@tool(
    "research_history",
    "Every run this agent has ever done, with its metrics and what it cost. The only thing in "
    "the system that compounds.",
    _schema(limit={"type": "integer", "description": "How many recent runs. Default 20."}),
)
def research_history(limit: int = 20) -> dict:
    from ee_agent.research.index import summary

    return {"index": summary(limit=limit)}


@tool(
    "agent_status",
    "What is configured and what is not: keys present, voice availability, open positions, "
    "kill switches, data sources. Use this when the client asks what the agent can do.",
    _schema(),
)
def agent_status() -> dict:
    from ee_agent.capture.voice import status as voice_status
    from ee_agent.conversation.model import describe_models
    from ee_agent.data.source_registry import capability_matrix
    from ee_agent.execution.ledger import ledger as position_ledger
    from ee_agent.secrets.vault import vault

    return {
        "keys": {s.name: s.present for s in vault().describe()},
        "models": describe_models(),
        "voice": voice_status().summary(),
        "positions": position_ledger().summary(),
        "data_sources": capability_matrix(),
        "workspace": WORKSPACE.summary(),
    }


# ================================================================ dispatch
def schemas() -> list[dict]:
    return [t.schema() for t in REGISTRY.values()]


def call(name: str, arguments: dict) -> str:
    """Run one tool and return a JSON string for the model. Never raises: a
    failed tool is information the conversation needs, not a crash."""
    tool_obj = REGISTRY.get(name)
    if tool_obj is None:
        return json.dumps({"error": f"no tool named {name!r}", "available": sorted(REGISTRY)})
    try:
        result = tool_obj.run(**(arguments or {}))
    except TypeError as exc:
        return json.dumps({"error": f"bad arguments for {name}: {exc}"})
    except Exception as exc:
        return json.dumps(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "where": traceback.format_exc(limit=2).splitlines()[-1],
                "note": "Tell the client plainly what failed. Do not pretend it worked.",
            }
        )
    return json.dumps(result, default=str)[:60000]
