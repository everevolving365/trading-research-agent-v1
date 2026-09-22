"""Screenshot intake (ability 15) and export portals (ability 31).

Both run with zero credentials: the vision model is faked, the portals are mock
pages that write real files to disk.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ee_agent.capture import vision
from ee_agent.operator import portals
from ee_agent.operator.browser import AuditTrail
from tests.portal_driver import MockPortalDriver

REPO = Path(__file__).resolve().parent.parent
PORTAL_PAGES = REPO / "tests/fixtures/portals"


# =========================================================== fake vision model
class FakeVisionReader(vision.VisionReader):
    """Returns a fixed reading, so the whole intake path is testable with no key."""

    def __init__(self, payload: dict | None = None, fail: bool = False):
        self.provider = "fake"
        self.model = "fake-vision"
        self.payload = payload if payload is not None else _CHART_PAYLOAD
        self.fail = fail
        self.read_paths: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def read(self, path: Path) -> dict:
        self.read_paths.append(str(path))
        if self.fail:
            raise RuntimeError("simulated vision outage")
        return self.payload


_CHART_PAYLOAD = {
    "symbol": "MNQ",
    "timeframe": "5m",
    "session_times": ["08:30", "09:30", "15:00"],
    "horizontal_levels": [
        {"label": "OR high", "price": "20125.25", "note": "top of the first hour"},
        {"label": "OR low", "price": "20080.50", "note": "bottom of the first hour"},
    ],
    "arrows": [
        {"direction": "down", "where": "just after price poked above OR high", "label": "short"},
        {"direction": "up", "where": "after a dip under OR low", "label": "long"},
    ],
    "zones": [{"label": "opening range", "note": "shaded box over the first hour"}],
    "text_annotations": ["sweep then back inside", "wait for the close", "25 pt stop"],
    "drawn_tools": ["rectangle", "horizontal line", "arrow"],
    "what_the_markup_seems_to_mark": "entries taken when price leaves a session range and closes back inside it",
    "unreadable": ["a small note in the corner"],
}


@pytest.fixture
def screenshot(tmp_path) -> Path:
    path = tmp_path / "MNQ_5m_2026-03-02.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)  # a real PNG header, enough to encode
    return path


# ================================================================== vision
def test_no_vision_model_says_so_and_still_extracts_what_it_can(screenshot, monkeypatch):
    import ee_agent.secrets.vault as vault_module

    monkeypatch.setattr(vault_module, "_VAULT", vault_module.Vault(backends=[vault_module._EnvBackend()]))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    intake = vision.read_charts([screenshot])
    assert intake.vision_model == "none"
    assert not intake.seen, "claimed to have read an image with no vision model"
    observation = intake.observations[0]
    assert observation.symbol == "MNQ", "did not extract the symbol from the filename"
    assert observation.timeframe == "5m"
    assert "2026-03-02" in observation.session_times
    assert "vision model" in observation.note
    assert "not what is drawn" in intake.summary()


def test_vision_model_reads_the_markup(screenshot):
    reader = FakeVisionReader()
    intake = vision.read_charts([screenshot], reader=reader)
    assert reader.read_paths == [str(screenshot)]
    assert len(intake.seen) == 1
    observation = intake.observations[0]
    assert observation.read_by == "fake:fake-vision"
    assert len(observation.horizontal_levels) == 2
    assert len(observation.arrows) == 2
    assert "sweep then back inside" in observation.text_annotations
    assert "closes back inside" in observation.summary
    assert observation.unreadable, "did not report what it could not read"
    text = intake.summary()
    assert "level 20125.25" in text
    assert "arrow down" in text
    assert "not a strategy" in text


def test_vision_failure_is_reported_not_hidden(screenshot):
    intake = vision.read_charts([screenshot], reader=FakeVisionReader(fail=True))
    observation = intake.observations[0]
    assert observation.read_by == "failed"
    assert "simulated vision outage" in observation.error
    assert "FAILED" in intake.summary()


def test_non_chart_image_is_recognised(screenshot):
    reader = FakeVisionReader(payload={"not_a_chart": True, "what_it_is": "a photo of a dog"})
    intake = vision.read_charts([screenshot], reader=reader)
    assert "not a trading chart" in intake.observations[0].note
    assert "dog" in intake.observations[0].note


def test_missing_and_unsupported_files_are_reported(tmp_path):
    missing = tmp_path / "nope.png"
    wrong = tmp_path / "notes.txt"
    wrong.write_text("hello", encoding="utf-8")
    intake = vision.read_charts([missing, wrong], reader=FakeVisionReader())
    assert intake.observations[0].error == "file not found"
    assert ".txt is not an image" in intake.observations[1].error


def test_everything_inferred_from_an_image_is_unapproved(screenshot):
    """The agent must not turn a picture into a rule. Hard rule 1."""
    intake = vision.read_charts([screenshot], reader=FakeVisionReader())
    spec = vision.spec_from_screenshots(intake)
    assert spec.assumptions, "read an image and proposed nothing to confirm"
    assert all(not a.approved for a in spec.assumptions), "an image-derived guess was pre-approved"

    from ee_agent.spec.validator import validate

    report = validate(spec)
    assert not report.live_ready, "a spec inferred from screenshots was declared live-ready"
    assert any(f.code == "AMB001" for f in report.blocking)


def test_spec_proposal_asks_the_right_questions(screenshot):
    intake = vision.read_charts([screenshot], reader=FakeVisionReader())
    spec = vision.spec_from_screenshots(intake)
    ids = {a.id for a in spec.assumptions}
    assert "screenshot:entry_rule" in ids, "did not ask WHY, only where"
    assert "screenshot:levels" in ids
    assert "screenshot:direction" in ids
    assert "screenshot:annotations" in ids
    assert "screenshot:risk" in ids
    entry = next(a for a in spec.assumptions if a.id == "screenshot:entry_rule")
    assert "not WHY" in entry.question or "not why" in entry.question.lower()
    assert spec.universe.instruments == ["MNQ"]
    assert all(a.sensitivity.get("if_wrong") for a in spec.assumptions)


def test_questions_for_is_client_ready(screenshot):
    intake = vision.read_charts([screenshot], reader=FakeVisionReader())
    questions = vision.questions_for(intake)
    assert len(questions) >= 5
    assert all(q.startswith("[screenshot:") for q in questions)


def test_mixed_timeframes_are_questioned(tmp_path):
    first = tmp_path / "MNQ_5m_a.png"
    second = tmp_path / "MNQ_15m_b.png"
    for path in (first, second):
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    reader = FakeVisionReader(payload={**_CHART_PAYLOAD, "timeframe": None})
    intake = vision.read_charts([first, second], reader=reader)
    spec = vision.spec_from_screenshots(intake)
    assert any(a.id == "screenshot:timeframe" for a in spec.assumptions)


def test_vision_cost_is_recorded(screenshot):
    from ee_agent.cost.notifier import CostLedger, ledger, set_ledger

    previous = ledger()
    try:
        book = CostLedger(notify_threshold_usd=10**9, sink=lambda _m: None)
        set_ledger(book)
        vision.read_charts([screenshot], reader=FakeVisionReader())
        assert book.total_usd > 0
        assert any(r.operation == "visual_confirmation" for r in book.records)
    finally:
        set_ledger(previous)


def test_extraction_prompt_forbids_inventing_a_strategy():
    prompt = vision.EXTRACTION_PROMPT
    assert "do not infer a trading strategy" in prompt.lower()
    assert "only what is actually drawn" in prompt.lower()


def test_json_parsing_survives_fences_and_prose():
    assert vision._parse_json('```json\n{"symbol": "ES"}\n```')["symbol"] == "ES"
    assert vision._parse_json('Here you go:\n{"symbol": "NQ"}\nhope that helps')["symbol"] == "NQ"
    with pytest.raises(ValueError):
        vision._parse_json("I could not read the image.")


# ================================================================== portals
def test_every_portal_is_well_formed():
    for portal_id, portal in portals.PORTALS.items():
        assert portal.id == portal_id
        assert portal.name
        assert portal.produces in ("bars", "fills", "statement")
        assert "export_open" in portal.selectors or portal_id == "broker_generic"
        assert "download_link" in portal.selectors
        if portal.needs_login:
            assert portal.password_secret
            assert "username" in portal.selectors and "login_submit" in portal.selectors


def test_portals_cannot_place_an_order():
    """Hard rule 4 applies here too: this module downloads, it does not trade."""
    source = (REPO / "ee_agent/operator/portals.py").read_text(encoding="utf-8")
    order_call = re.compile(r"\b(?:place_order|submit_order|send_order|strategy\.entry|\.buy\(|\.sell\()")
    assert "OrderIntent" not in source
    assert "execution.router" not in source
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        assert not order_call.search(line), f"portals.py contains an order call: {stripped[:70]}"


def test_bar_export_is_retrieved_and_ingested(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_USERNAME", "client")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "pw")
    downloads = tmp_path / "dl"
    driver = MockPortalDriver(PORTAL_PAGES, downloads, produces="bars")
    operator = portals.PortalOperator(driver, AuditTrail(root=tmp_path / "audit"), download_dir=downloads)
    result = operator.retrieve(
        "tradingview_export", symbol="MNQ", start="2026-03-01", end="2026-03-31"
    )
    assert result.ok, result.errors
    assert result.downloaded
    assert result.ingested_rows == 240
    assert result.ingested_symbol == "MNQ"
    assert result.kind == "OHLCV bars"
    assert any("signed in" in s for s in result.steps)
    assert "cache age" in result.summary() or "bars" in result.summary()


def test_fill_export_is_recognised_as_fills(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEPX_USERNAME", "client")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "key")
    downloads = tmp_path / "dl"
    driver = MockPortalDriver(PORTAL_PAGES, downloads, produces="fills")
    operator = portals.PortalOperator(driver, AuditTrail(root=tmp_path / "audit"), download_dir=downloads)
    result = operator.retrieve("topstepx_statements", start="2026-03-01", end="2026-03-31")
    assert result.ok, result.errors
    assert result.kind == "broker fill history"
    assert result.ingested_rows == 240


def test_retrieved_fills_reconstruct_behaviour(tmp_path, monkeypatch):
    """The point of pulling a statement: measure what the client actually does."""
    monkeypatch.setenv("TOPSTEPX_USERNAME", "client")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "key")
    downloads = tmp_path / "dl"
    driver = MockPortalDriver(PORTAL_PAGES, downloads, produces="fills")
    operator = portals.PortalOperator(driver, AuditTrail(root=tmp_path / "audit"), download_dir=downloads)
    operator.retrieve("topstepx_statements")

    from ee_agent.capture.intake import reconstruct_from_fills, spec_from_fills
    from ee_agent.data.ingest import ingest_any

    ingested = ingest_any(Path(result_path) if (result_path := driver.downloads[0]) else None)
    behaviour = reconstruct_from_fills(ingested.fills, tick_size=0.25)
    assert behaviour.n_round_turns > 0
    spec = spec_from_fills(behaviour)
    assert spec.unresolved_assumptions(), "reconstructed rules were treated as approved"


def test_missing_credentials_are_reported_with_the_fix(tmp_path, monkeypatch):
    import ee_agent.secrets.vault as vault_module

    monkeypatch.setattr(vault_module, "_VAULT", vault_module.Vault(backends=[vault_module._EnvBackend()]))
    monkeypatch.delenv("TRADINGVIEW_USERNAME", raising=False)
    monkeypatch.delenv("TRADINGVIEW_PASSWORD", raising=False)
    downloads = tmp_path / "dl"
    driver = MockPortalDriver(PORTAL_PAGES, downloads)
    operator = portals.PortalOperator(driver, AuditTrail(root=tmp_path / "audit"), download_dir=downloads)
    result = operator.retrieve("tradingview_export")
    assert not result.ok
    assert "ee-agent secrets set TRADINGVIEW_USERNAME" in result.errors[0]
    assert "never logged" in result.errors[0]


def test_paywall_brings_the_page_to_the_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CME_DATAMINE_USERNAME", "client")
    monkeypatch.setenv("CME_DATAMINE_PASSWORD", "pw")
    downloads = tmp_path / "dl"
    driver = MockPortalDriver(PORTAL_PAGES, downloads, page="paywalled.html")
    operator = portals.PortalOperator(driver, AuditTrail(root=tmp_path / "audit"), download_dir=downloads)
    result = operator.retrieve("cme_datamine", symbol="MNQ")
    assert result.paywalled
    assert not result.ok
    assert "paywall" in result.paywall_message.lower()
    assert "datamine.cmegroup.com" in result.paywall_message
    assert "brought it to you" in " ".join(result.steps)


def test_unknown_portal_raises_with_the_known_list(tmp_path):
    driver = MockPortalDriver(PORTAL_PAGES, tmp_path / "dl")
    operator = portals.PortalOperator(driver, AuditTrail(root=tmp_path / "audit"))
    with pytest.raises(KeyError) as exc:
        operator.retrieve("some_broker_nobody_has_heard_of")
    assert "tradingview_export" in str(exc.value)


def test_audit_trail_records_every_portal_action(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_USERNAME", "client")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "pw")
    downloads = tmp_path / "dl"
    driver = MockPortalDriver(PORTAL_PAGES, downloads)
    audit = AuditTrail(root=tmp_path / "audit")
    operator = portals.PortalOperator(driver, audit, download_dir=downloads)
    operator.retrieve("tradingview_export", symbol="MNQ")
    kinds = [a.kind for a in audit.actions]
    assert "portal_login" in kinds
    assert "portal_open" in kinds
    assert "portal_download" in kinds
    assert (audit.dir / "audit.json").exists()


def test_catalogue_is_readable():
    text = portals.catalogue()
    assert "export portal(s)" in text
    assert "tradingview_export" in text
    assert "None of these can place an order" in text
