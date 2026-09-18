"""The conversation: deep talk, tool use, and the boundaries it must not cross.

Runs entirely on a scripted model, so it needs no credentials.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ee_agent.conversation import tools as toolbox
from ee_agent.conversation.agent import Conversation, run_repl
from ee_agent.conversation.model import Message, NullModel, get_model
from tests.fake_model import ExplodingModel, ScriptedModel

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_workspace():
    toolbox.reset_workspace()
    yield
    toolbox.reset_workspace()


@pytest.fixture(autouse=True)
def isolated_cost_ledger():
    """The cost ledger is a process global. A test that sets a ceiling must not
    leave it set for every test after it."""
    import ee_agent.cost.notifier as notifier

    before = notifier.ledger()
    yield
    notifier.set_ledger(before)


# ============================================================== the boundary
def test_no_conversational_tool_can_place_an_order():
    """A model must not be able to reach the order layer. Hard rule 4's spirit:
    anything that can click buy bypasses the position ledger."""
    source = (REPO / "ee_agent/conversation/tools.py").read_text(encoding="utf-8")
    order_call = re.compile(r"\b(?:place_order|submit_order|send_order|\.submit\(|OrderIntent\()")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "FORBIDDEN" in line or stripped.startswith('"'):
            continue
        assert not order_call.search(line), f"a conversational tool reaches the order layer: {stripped[:80]}"

    for name in toolbox.REGISTRY:
        assert name not in toolbox.FORBIDDEN
        assert "order" not in name or name == "order_flow"


def test_tools_never_import_the_router():
    source = (REPO / "ee_agent/conversation/tools.py").read_text(encoding="utf-8")
    assert "execution.router" not in source
    assert "OrderRouter" not in source


# ================================================================ no-key path
def test_works_with_no_model_key(monkeypatch):
    import ee_agent.secrets.vault as vault_module

    monkeypatch.setattr(vault_module, "_VAULT", vault_module.Vault(backends=[vault_module._EnvBackend()]))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    model = get_model()
    assert isinstance(model, NullModel)
    conversation = Conversation(model=model, sink=lambda _m: None)
    answer = conversation.say("what do you think about the market today?")
    assert "no model key" in answer.lower()
    assert "everything else still works" in answer.lower()


def test_model_outage_degrades_rather_than_crashes():
    conversation = Conversation(model=ExplodingModel(), sink=lambda _m: None)
    answer = conversation.say("hello")
    assert "could not reach the model" in answer
    assert "still work" in answer


# ================================================================ tool use
def test_conversation_calls_a_tool_and_reports_the_result():
    model = ScriptedModel(
        script=[
            {"tools": [{"name": "load_data", "arguments": {"symbol": "MNQ", "timeframe": "5m", "fixtures_only": True}}]},
            {"text": "Loaded MNQ. 17,061 bars, quality 1.00."},
        ]
    )
    conversation = Conversation(model=model, sink=lambda _m: None)
    answer = conversation.say("pull me some MNQ data")
    assert "Loaded MNQ" in answer
    assert toolbox.WORKSPACE.bars is not None
    result = json.loads(model.tool_results_seen[0])
    assert result["symbol"] == "MNQ"
    assert result["bars"] > 1000
    assert "quality_score" in result


def test_full_capture_to_parity_through_conversation():
    """The whole product, driven by the model: capture, load, backtest, compile,
    prove parity. No credentials anywhere."""
    transcript = (REPO / "tests/fixtures/transcripts/sweep-return.txt").read_text(encoding="utf-8")
    model = ScriptedModel(
        script=[
            {"tools": [{"name": "describe_strategy", "arguments": {"text": transcript, "strategy_id": "chat-capture"}}]},
            {"tools": [{"name": "show_spec", "arguments": {}}]},
            {"tools": [{"name": "load_data", "arguments": {"symbol": "MNQ", "fixtures_only": True}}]},
            {"tools": [{"name": "compile_indicator", "arguments": {"out_dir": ""}}]},
            {"tools": [{"name": "prove_parity", "arguments": {}}]},
            {"text": "Captured, compiled and proven identical across all four targets."},
        ]
    )
    conversation = Conversation(model=model, sink=lambda _m: None)
    answer = conversation.say(transcript)
    assert "proven identical" in answer

    results = [json.loads(r) for r in model.tool_results_seen]
    captured, shown, loaded, compiled, parity = results
    assert "MNQ" in captured["understood"]["instruments"]
    assert captured["understood"]["stop"] == "25.0 points"
    assert captured["understood"]["target"] == "50.0 points"
    assert "Stop" in shown["plain_english"]
    assert loaded["bars"] > 1000
    assert compiled["indicator_pine"].startswith("//@version=5")
    assert "plotshape(" in compiled["indicator_pine"]
    assert parity["agreed"] is True, parity.get("divergences")


def test_interrogation_surfaces_what_to_ask():
    model = ScriptedModel(
        script=[
            {"tools": [{"name": "describe_strategy", "arguments": {"text": "I buy when the 9 ema crosses the 21 on MNQ"}}]},
            {"text": "What's your stop?"},
        ]
    )
    conversation = Conversation(model=model, sink=lambda _m: None)
    conversation.say("I buy when the 9 ema crosses the 21 on MNQ")
    captured = json.loads(model.tool_results_seen[0])
    topics = {q["topic"] for q in captured["still_to_ask"]}
    assert "stop_placement" in topics
    assert "target_logic" in topics
    assert "the client owns the strategy and the risk" in captured["note"].lower()


def test_answering_a_question_updates_the_spec():
    toolbox.describe_strategy("I trade MNQ on a 5 minute chart")
    result = toolbox.answer_question("stop_placement", "1.5 ATR")
    assert result["applied"]
    assert toolbox.WORKSPACE.spec.risk.stop.type == "atr"
    assert toolbox.WORKSPACE.spec.risk.stop.value == 1.5


def test_the_agent_never_answers_its_own_questions():
    """Silence must not become an answer: unanswered items stay unresolved."""
    toolbox.describe_strategy("I trade MNQ when it breaks out")
    before = toolbox.WORKSPACE.spec.risk.stop
    result = toolbox.answer_question("stop_placement", "whatever you think is best")
    assert not result["applied"], "the agent accepted a non-answer as a risk rule"
    assert toolbox.WORKSPACE.spec.risk.stop == before


def test_tool_failure_is_reported_not_hidden():
    result = json.loads(toolbox.call("backtest", {}))
    assert "error" in result
    assert "No strategy captured" in result["error"]


def test_unknown_tool_is_reported():
    result = json.loads(toolbox.call("delete_everything", {}))
    assert "error" in result and "available" in result


def test_every_tool_has_a_schema():
    for schema in toolbox.schemas():
        assert schema["name"] and schema["description"]
        assert schema["parameters"]["type"] == "object"
        assert "properties" in schema["parameters"]


# ==================================================================== cost
def test_conversation_records_what_it_spent():
    from ee_agent.cost.notifier import CostLedger, set_ledger

    book = CostLedger(notify_threshold_usd=10**9, sink=lambda _m: None)
    set_ledger(book)
    model = ScriptedModel(script=[{"text": "hello", "input_tokens": 2000, "output_tokens": 800}])
    conversation = Conversation(model=model, sink=lambda _m: None)
    conversation.say("hi")
    assert book.total_usd > 0
    assert any(r.operation == "conversation" for r in book.records)
    assert conversation.turns[-1].cost_usd > 0


def test_cost_layer_never_blocks_without_a_client_ceiling():
    from ee_agent.cost.notifier import CostLedger, set_ledger

    book = CostLedger(ceiling_usd=None, notify_threshold_usd=0.0, sink=lambda _m: None)
    set_ledger(book)
    model = ScriptedModel(script=[{"text": "ok", "input_tokens": 10**6, "output_tokens": 10**6}])
    conversation = Conversation(model=model, sink=lambda _m: None)
    assert conversation.say("spend a fortune") == "ok"


def test_client_ceiling_asks_rather_than_stopping_silently():
    from ee_agent.cost.notifier import CostLedger, set_ledger

    book = CostLedger(ceiling_usd=0.001, notify_threshold_usd=10**9, sink=lambda _m: None)
    set_ledger(book)
    model = ScriptedModel(script=[{"text": "ok", "input_tokens": 10**6, "output_tokens": 10**6}])
    conversation = Conversation(model=model, sink=lambda _m: None)
    answer = conversation.say("go")
    assert "ceiling" in answer.lower()
    assert "have not stopped" in answer.lower()


# ============================================================== transcript
def test_transcript_is_written(tmp_path):
    model = ScriptedModel(script=[{"text": "noted"}])
    path = tmp_path / "c.jsonl"
    conversation = Conversation(model=model, sink=lambda _m: None, transcript_path=path)
    conversation.say("remember this")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [line["role"] for line in lines] == ["client", "agent"]
    assert lines[0]["text"] == "remember this"


def test_repl_runs_and_exits(capsys):
    answers = iter(["what can you do?", "exit"])
    model = ScriptedModel(script=[{"text": "I can capture your strategy and backtest it."}])
    import ee_agent.conversation.agent as agent_module

    original = agent_module.get_model
    agent_module.get_model = lambda *_a, **_kw: model
    try:
        code = run_repl(voice=False, input_fn=lambda _p: next(answers), sink=lambda _m: None)
    finally:
        agent_module.get_model = original
    assert code == 0


# ============================================================== discovery
def test_discovery_finds_sources_offline():
    from ee_agent.data.discovery import discover_sources

    result = discover_sources("1-minute copper futures history", use_web=False)
    assert result.candidates
    assert all(c.found_by == "catalogue" for c in result.candidates)


def test_discovery_prefers_order_flow_when_asked_for_it():
    from ee_agent.data.discovery import discover_sources

    result = discover_sources("order flow and footprint data for NQ futures", use_web=False)
    assert result.candidates[0].has_order_flow, result.candidates[0].name


def test_discovery_prefers_keyless_for_free_requests():
    from ee_agent.data.discovery import discover_sources

    result = discover_sources("free tick level forex data", use_web=False)
    assert any(not c.needs_key for c in result.candidates[:3])


def test_discovery_infers_asset_class():
    from ee_agent.data.discovery import infer_asset_class

    assert infer_asset_class("bitcoin minute bars") == "crypto"
    assert infer_asset_class("MNQ futures tick data") == "future"
    assert infer_asset_class("eurusd pairs") == "forex"


def test_discovery_says_why_it_did_not_search_the_web(monkeypatch):
    import ee_agent.secrets.vault as vault_module
    from ee_agent.data.discovery import discover_sources

    monkeypatch.setattr(vault_module, "_VAULT", vault_module.Vault(backends=[vault_module._EnvBackend()]))
    for key in ("BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    result = discover_sources("obscure exotic data", use_web=True)
    assert not result.searched_web
    assert "no web search key" in result.search_note


def test_discovery_does_not_offer_scraping_as_a_first_answer():
    """Section 9 limit 2: scraping arbitrary sites will not hold."""
    from ee_agent.data.discovery import discover_sources

    for query in ("minute data for SPY", "futures history", "crypto bars"):
        result = discover_sources(query, use_web=False)
        if result.candidates:
            assert result.candidates[0].access != "scrape"


def test_catalogue_entries_are_well_formed():
    from ee_agent.data.discovery import _catalogue

    entries = _catalogue()
    assert len(entries) >= 10
    for entry in entries:
        assert entry.name and entry.url.startswith("http")
        assert entry.asset_classes and entry.resolutions
        assert entry.cost, f"{entry.name} does not say what it costs"
        if entry.needs_key:
            assert entry.key_url, f"{entry.name} needs a key but does not say where to get one"


def test_discovery_tool_is_reachable_from_conversation():
    result = json.loads(toolbox.call("find_data_sources", {"query": "tick data for gold futures"}))
    assert "candidates" in result
    assert result["candidates"]
