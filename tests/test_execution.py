"""Execution: the position ledger, the router, autonomy, kill switches, adapters.

The position ledger gets the most thorough tests in the codebase alongside the
parity harness (Section 2.4): a silent failure here is the one that costs the
owner an account.
"""
from __future__ import annotations

import pytest

from ee_agent.errors import AutonomyViolation, HedgeRefused, KillSwitchTripped
from ee_agent.execution.adapters import MockBroker, TopstepXAdapter
from ee_agent.execution.autonomy import (
    AutonomyLadder, Caps, KillSwitchConfig, KillSwitches, Level, PromotionEvidence,
)
from ee_agent.execution.ledger import OrderIntent, PositionLedger
from ee_agent.execution.router import OrderRouter
from tests.mock_topstepx import MockTopstepXServer


@pytest.fixture
def ledger():
    return PositionLedger(path=None)


@pytest.fixture
def router(ledger):
    broker = MockBroker(account_id="acct-B")
    broker.connect()
    broker.set_price("ES", 5700.0)
    broker.set_price("MNQ", 20000.0)
    return OrderRouter(
        adapter=broker,
        ladder=AutonomyLadder(strategy_id="t", level=Level.UNATTENDED),
        position_ledger=ledger,
        account_id="acct-B",
        sink=lambda _m: None,
    )


# ============================================================ hedge refusal
def test_same_symbol_opposing_across_accounts_is_refused(ledger):
    ledger.apply_fill("acct-A", "MNQ", +2, 20000.0)
    check = ledger.check(OrderIntent(account="acct-B", symbol="MNQ", side="sell", size=1))
    assert not check.allowed
    assert "REFUSED at the order layer" in check.reason


def test_correlated_symbols_are_a_hedge_even_though_they_differ(ledger):
    """Long NQ on one account and short ES on another IS a hedge (hard rule 3)."""
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    check = ledger.check(OrderIntent(account="acct-B", symbol="ES", side="sell", size=1))
    assert not check.allowed
    assert check.conflicts[0]["symbol"] == "NQ"
    assert check.conflicts[0]["correlation"] >= 0.6


def test_uncorrelated_symbols_are_not_a_hedge(ledger):
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    assert ledger.check(OrderIntent(account="acct-B", symbol="CL", side="sell", size=1)).allowed


def test_same_direction_across_accounts_is_allowed(ledger):
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    assert ledger.check(OrderIntent(account="acct-B", symbol="ES", side="buy", size=1)).allowed


def test_closing_your_own_position_is_never_refused(ledger):
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    ledger.apply_fill("acct-B", "ES", -1, 5700.0)  # a hedge already exists
    check = ledger.check(OrderIntent(account="acct-A", symbol="NQ", side="sell", size=2))
    assert check.allowed, "the client was trapped in a position they were trying to exit"


def test_reducing_a_position_is_never_refused(ledger):
    ledger.apply_fill("acct-A", "NQ", +4, 20000.0)
    ledger.apply_fill("acct-B", "ES", -1, 5700.0)
    assert ledger.check(OrderIntent(account="acct-A", symbol="NQ", side="sell", size=2)).allowed


def test_flipping_through_flat_into_a_hedge_is_refused(ledger):
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    ledger.apply_fill("acct-B", "ES", +1, 5700.0)
    check = ledger.check(OrderIntent(account="acct-B", symbol="ES", side="sell", size=3))
    assert not check.allowed, "flipping a position through flat created a hedge"


def test_there_is_no_override_flag():
    import inspect

    source = inspect.getsource(PositionLedger.check)
    for word in ("force", "override", "bypass", "skip_check", "allow_hedge"):
        assert word not in source, f"the hedge check has an escape hatch: {word}"


def test_refusal_happens_before_any_api_call(router, ledger):
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    result = router.submit(OrderIntent(account="acct-B", symbol="ES", side="sell", size=1))
    assert not result.accepted
    assert result.stage == "hedge"
    assert not result.reached_api
    assert router.adapter.order_log == []


def test_refusals_are_logged(ledger):
    ledger.apply_fill("acct-A", "NQ", +2, 20000.0)
    ledger.check(OrderIntent(account="acct-B", symbol="ES", side="sell", size=1))
    assert ledger.refusals
    assert ledger.refusals[0]["event"] == "hedge_refused"


def test_position_accounting_is_correct(ledger):
    ledger.apply_fill("a", "MNQ", +2, 100.0)
    ledger.apply_fill("a", "MNQ", +2, 102.0)
    position = ledger.get("a", "MNQ")
    assert position.size == 4
    assert position.avg_price == pytest.approx(101.0)
    ledger.apply_fill("a", "MNQ", -4, 105.0)
    assert ledger.get("a", "MNQ").flat


# =============================================================== the router
def test_router_order_of_gates(router, ledger):
    """Kill switch first, then autonomy, then prop limit, then hedge."""
    router.kill.trip("global", "global", "test")
    result = router.submit(OrderIntent(account="acct-B", symbol="MNQ", side="buy", size=1))
    assert result.stage == "kill_switch"
    assert not result.reached_api


def test_accepted_order_reaches_the_broker(router):
    result = router.submit(OrderIntent(account="acct-B", symbol="MNQ", side="buy", size=1))
    assert result.accepted and result.reached_api
    assert router.adapter.order_log


def test_prop_position_limit_is_enforced(ledger):
    broker = MockBroker(account_id="acct-C")
    broker.connect()
    router = OrderRouter(
        adapter=broker,
        ladder=AutonomyLadder(strategy_id="t", level=Level.UNATTENDED),
        position_ledger=ledger,
        prop_firm="topstep",
        prop_account="combine-50k",
        account_id="acct-C",
        sink=lambda _m: None,
    )
    result = router.submit(OrderIntent(account="acct-C", symbol="NQ", side="buy", size=1))
    assert not result.accepted, "1 NQ is 10 micro-equivalents against a 5-contract limit"
    assert result.stage == "prop_limit"


def test_paper_level_never_reaches_the_broker(ledger):
    broker = MockBroker(account_id="acct-P")
    broker.connect()
    broker.set_price("MNQ", 20000.0)
    router = OrderRouter(
        adapter=broker,
        ladder=AutonomyLadder(strategy_id="t", level=Level.PAPER),
        position_ledger=ledger,
        account_id="acct-P",
        sink=lambda _m: None,
    )
    result = router.submit(OrderIntent(account="acct-P", symbol="MNQ", side="buy", size=1))
    assert result.accepted and result.stage == "paper"
    assert broker.order_log == []
    assert router.shadow_fills


def test_daily_loss_cap_blocks_entries_but_not_exits(ledger):
    broker = MockBroker(account_id="acct-D")
    broker.connect()
    broker.set_price("MNQ", 20000.0)
    ladder = AutonomyLadder(
        strategy_id="t", level=Level.CAPPED, caps=Caps(max_contracts=5, max_trades_per_day=10, max_daily_loss_usd=100)
    )
    router = OrderRouter(adapter=broker, ladder=ladder, position_ledger=ledger, account_id="acct-D",
                         sink=lambda _m: None)
    router.submit(OrderIntent(account="acct-D", symbol="MNQ", side="buy", size=1))
    router.ingest_fills()
    router.day.pnl = -500.0
    blocked = router.submit(OrderIntent(account="acct-D", symbol="MNQ", side="buy", size=1))
    assert not blocked.accepted and blocked.stage == "autonomy"
    exit_order = router.submit(OrderIntent(account="acct-D", symbol="MNQ", side="sell", size=1))
    assert exit_order.accepted, "the daily loss cap trapped the client in a position"


# ============================================================== autonomy
def test_levels_below_four_cannot_place_real_orders():
    for level in (Level.WATCH, Level.PROPOSE, Level.PAPER):
        ladder = AutonomyLadder(strategy_id="t", level=level)
        with pytest.raises(AutonomyViolation):
            ladder.authorize(OrderIntent(account="a", symbol="MNQ", side="buy", size=1))


def test_level_four_enforces_size_caps():
    ladder = AutonomyLadder(strategy_id="t", level=Level.CAPPED, caps=Caps(max_contracts=2))
    ladder.authorize(OrderIntent(account="a", symbol="MNQ", side="buy", size=2))
    with pytest.raises(AutonomyViolation):
        ladder.authorize(OrderIntent(account="a", symbol="MNQ", side="buy", size=3))


def test_promotion_requires_evidence_and_an_approver():
    ladder = AutonomyLadder(strategy_id="t", level=Level.PAPER)
    with pytest.raises(AutonomyViolation):
        ladder.promote(approved_by="someone")
    ladder.evidence = PromotionEvidence(live_or_paper_sessions=25, trades=40, drift_flags=0)
    with pytest.raises(AutonomyViolation):
        ladder.promote(approved_by="")
    assert ladder.promote(approved_by="owner") == Level.CAPPED


def test_demotion_is_automatic_and_needs_no_approval():
    ladder = AutonomyLadder(strategy_id="t", level=Level.CAPPED)
    assert ladder.demote("drift detected") == Level.PAPER
    assert ladder.history[-1]["reason"].startswith("AUTOMATIC DEMOTION")


# ========================================================== kill switches
@pytest.mark.parametrize(
    "kwargs,scope",
    [
        ({"daily_pnl": -2000}, "account"),
        ({"consecutive_losses": 9}, "strategy"),
        ({"drift_sigma": 5.0}, "strategy"),
        ({"latency_ms": 9000}, "global"),
        ({"data_staleness_s": 500}, "global"),
        ({"api_errors": 20}, "account"),
    ],
)
def test_every_documented_trigger_trips(kwargs, scope):
    switches = KillSwitches(KillSwitchConfig())
    tripped = switches.evaluate(strategy_id="s", account="a", **kwargs)
    assert any(state.scope == scope for state in tripped), f"{kwargs} did not trip a {scope} switch"


def test_tripped_switch_blocks_trading_until_a_human_resets():
    switches = KillSwitches()
    switches.trip("strategy", "s", "because")
    with pytest.raises(KillSwitchTripped):
        switches.check(strategy_id="s")
    switches.reset("strategy", "s")
    switches.check(strategy_id="s")


def test_global_switch_covers_every_strategy():
    switches = KillSwitches()
    switches.trip("global", "global", "market data is down")
    with pytest.raises(KillSwitchTripped):
        switches.check(strategy_id="anything", account="anything")


# ================================================================ adapters
def test_mock_broker_contract():
    broker = MockBroker()
    assert broker.connect()
    broker.set_price("MNQ", 20000.0)
    ack = broker.place(OrderIntent(account="MOCK-1", symbol="MNQ", side="buy", size=2))
    assert ack.accepted and ack.broker_order_id
    fills = broker.poll_fills()
    assert fills and fills[0].signed_size == 2
    positions = broker.positions()
    assert positions and positions[0]["size"] == 2
    broker.flatten()
    assert broker.positions() == []


def test_mock_broker_rejection_does_not_raise():
    broker = MockBroker(reject_rate=1.0)
    broker.connect()
    ack = broker.place(OrderIntent(account="MOCK-1", symbol="MNQ", side="buy", size=1))
    assert not ack.accepted and ack.status == "rejected"


def test_topstepx_contract_against_the_mock_server(monkeypatch):
    monkeypatch.setenv("TOPSTEPX_USERNAME", "u")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "k")
    server = MockTopstepXServer()
    adapter = TopstepXAdapter(account_id="900001", session=server, base_url="https://mock/api")
    assert adapter.connect()
    assert adapter.account().balance > 0
    ack = adapter.place(OrderIntent(account="900001", symbol="MNQ", side="buy", size=1))
    assert ack.accepted
    fills = adapter.poll_fills()
    assert fills and fills[0].signed_size > 0, "a buy produced a non-positive signed size"
    assert adapter.positions()
    assert adapter.flatten()
    assert adapter.health()["connected"]


def test_topstepx_needs_credentials(monkeypatch):
    monkeypatch.delenv("TOPSTEPX_USERNAME", raising=False)
    monkeypatch.delenv("TOPSTEPX_API_KEY", raising=False)
    from ee_agent.secrets.vault import Vault
    import ee_agent.secrets.vault as vault_module

    monkeypatch.setattr(vault_module, "_VAULT", Vault(backends=[vault_module._EnvBackend()]))
    adapter = TopstepXAdapter(session=MockTopstepXServer(), base_url="https://mock/api")
    with pytest.raises(RuntimeError) as exc:
        adapter.connect()
    assert "keychain" in str(exc.value)


def test_topstepx_surfaces_server_errors(monkeypatch):
    monkeypatch.setenv("TOPSTEPX_USERNAME", "u")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "k")
    server = MockTopstepXServer()
    adapter = TopstepXAdapter(account_id="900001", session=server, base_url="https://mock/api")
    adapter.connect()
    server.fail_next = "/Order/place"
    ack = adapter.place(OrderIntent(account="900001", symbol="MNQ", side="buy", size=1))
    assert not ack.accepted and "500" in ack.message


def test_owner_robot_adapter_says_what_it_needs():
    from ee_agent.execution.adapters import OwnerRobotAdapter

    with pytest.raises(NotImplementedError) as exc:
        OwnerRobotAdapter().connect()
    assert "ADAPTER-INTERFACE.md" in str(exc.value)


# ================================================================= replay
def test_replay_runs_five_sessions_through_the_live_path(spec, bars):
    from ee_agent.execution.replay import ReplayHarness

    harness = ReplayHarness(
        spec, kill_config=KillSwitchConfig(daily_loss_usd=10000, consecutive_losses=50, max_drift_sigma=99)
    )
    harness.ladder.caps = Caps(max_contracts=5, max_trades_per_day=50, max_daily_loss_usd=10000)
    result = harness.run(bars, max_sessions=5)
    assert len(result.sessions) == 5
    assert result.closed_trades
    assert result.orders_accepted > 0


def test_replay_uses_the_live_config_not_the_spec(spec, bars):
    from ee_agent.execution.replay import ReplayHarness

    harness = ReplayHarness(spec)
    assert harness.plan.spec_hash == spec.hash
    assert harness.plan.plan_hash == harness.live_config.plan_hash


def test_drift_monitor_flags_a_shifted_distribution(spec, bars):
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.execution.drift import Baseline, DriftMonitor

    report = TruthEngine(spec, monte_carlo_paths=50, synthetic_paths=0).analyze(bars.slice(0, 4000), pin=False)
    monitor = DriftMonitor(Baseline.from_report(report), sigma_threshold=3.0, window=20)
    for _ in range(20):
        monitor.observe_trade(-5000.0, tick_value=0.5)
    assert monitor.flags, "a wildly different live distribution raised no flag"

    ladder = AutonomyLadder(strategy_id=spec.id, level=Level.CAPPED)
    monitor.apply_to(ladder)
    assert ladder.level == Level.PAPER, "drift did not demote automatically"


def test_execution_scorecard_grades_fills(spec, bars):
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.execution.drift import Baseline, DriftMonitor

    report = TruthEngine(spec, monte_carlo_paths=50, synthetic_paths=0).analyze(bars.slice(0, 4000), pin=False)
    monitor = DriftMonitor(Baseline.from_report(report))
    for _ in range(10):
        monitor.observe_trade(10.0, actual_slippage_ticks=4.0, expected_slippage_ticks=1.0, tick_value=0.5)
    assert monitor.score.excess_cost > 0
    assert monitor.score.grade.startswith("F")


def test_shadow_mode_answers_what_would_it_have_done():
    from ee_agent.execution.drift import ShadowBook

    book = ShadowBook("s")
    book.record_paper(pnl=100.0)
    book.record_live(pnl=60.0)
    divergence = book.divergence()
    assert divergence["gap"] == -40.0
    assert "execution" in divergence["note"]
