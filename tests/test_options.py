"""Options as an asset class (ability 16, the last gap against the owner's list).

Everything here runs with zero credentials: the chain is synthetic and option
candles are repriced from the committed underlying fixtures.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from ee_agent.data.options import (
    _next_monthly_expiries, load_chain, load_option_bars, synthetic_chain, synthetic_option_bars,
)
from ee_agent.instruments.options import (
    OptionContract, black_scholes, implied_volatility, is_option_symbol, option_instrument,
    parse_option, third_friday,
)
from ee_agent.instruments.registry import registry

AS_OF = date(2026, 9, 21)


# ================================================================== parsing
@pytest.mark.parametrize(
    "text,root,strike,right,expiry",
    [
        ("SPY241220C00580000", "SPY", 580.0, "call", date(2024, 12, 20)),
        ("O:SPY241220C00580000", "SPY", 580.0, "call", date(2024, 12, 20)),
        ("SPY 580 call 2024-12-20", "SPY", 580.0, "call", date(2024, 12, 20)),
        ("SPY 580C 241220", "SPY", 580.0, "call", date(2024, 12, 20)),
        ("AAPL 200 put 20 Dec 24", "AAPL", 200.0, "put", date(2024, 12, 20)),
        ("spy 575.5 put 2025-01-17", "SPY", 575.5, "put", date(2025, 1, 17)),
    ],
)
def test_parses_every_form_a_client_might_type(text, root, strike, right, expiry):
    contract = parse_option(text)
    assert contract.root == root
    assert contract.strike == strike
    assert contract.right == right
    assert contract.expiry == expiry


def test_occ_round_trips():
    contract = parse_option("SPY 580 call 2024-12-20")
    assert contract.occ == "SPY241220C00580000"
    assert parse_option(contract.occ) == contract


def test_fractional_strikes_survive_the_round_trip():
    contract = parse_option("SPY 575.5 put 2025-01-17")
    assert parse_option(contract.occ).strike == 575.5


def test_missing_right_or_expiry_is_refused_with_guidance():
    with pytest.raises(ValueError) as exc:
        parse_option("SPY 580 2024-12-20")
    assert "call or a put" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        parse_option("SPY 580 call")
    assert "no expiry" in str(exc.value)


def test_nonsense_is_refused_with_the_accepted_forms():
    with pytest.raises(ValueError) as exc:
        parse_option("buy me something good")
    assert "OCC" in str(exc.value)


def test_is_option_symbol_does_not_misfire_on_ordinary_tickers():
    assert is_option_symbol("SPY241220C00580000")
    for ordinary in ("SPY", "MNQ", "BTCUSDT", "EURUSD", "ES", "MNQZ5"):
        assert not is_option_symbol(ordinary), ordinary


# ============================================================== instrument
def test_option_resolves_to_an_ordinary_instrument():
    instrument = option_instrument("SPY 580 call 2026-12-18")
    assert instrument.asset_class == "option"
    assert instrument.multiplier == 100.0
    assert instrument.tick_size == 0.01
    # One tick on one contract is 0.01 * 100 = $1.00. Getting this wrong
    # misprices every option position by 100x.
    assert instrument.tick_value == pytest.approx(1.00)
    assert instrument.ticks_to_currency(100, 1) == pytest.approx(100.0)


def test_index_options_get_their_own_facts():
    spx = option_instrument("SPX 5800 call 2026-12-18")
    assert spx.tick_size == 0.05
    assert spx.extra["settlement"] == "cash"
    assert spx.timezone == "America/Chicago"


def test_option_carries_the_underlyings_correlation_group():
    """So the hedge check sees a long SPY call against a short ES as the hedge
    it is (hard rule 3)."""
    reg = registry()
    option_instrument("SPY 580 call 2026-12-18")
    assert reg.is_hedge("SPY261218C00580000", "ES")
    assert reg.is_hedge("SPY261218C00580000", "SPY")
    assert not reg.is_hedge("SPY261218C00580000", "CL")


def test_a_long_call_and_a_short_future_are_refused_across_accounts():
    from ee_agent.execution.ledger import OrderIntent, PositionLedger

    ledger = PositionLedger(path=None)
    option_instrument("SPY 580 call 2026-12-18")
    ledger.apply_fill("acct-A", "SPY261218C00580000", +5, 23.25)
    check = ledger.check(OrderIntent(account="acct-B", symbol="ES", side="sell", size=1))
    assert not check.allowed, "a long call plus a short index future was not seen as a hedge"


def test_moneyness_and_intrinsic():
    call = parse_option("SPY 580 call 2026-12-18")
    put = parse_option("SPY 580 put 2026-12-18")
    assert call.moneyness(600) > 1 and call.moneyness(560) < 1
    assert put.moneyness(560) > 1 and put.moneyness(600) < 1
    assert call.intrinsic(600) == pytest.approx(2000.0)  # 20 points x 100
    assert call.intrinsic(560) == 0.0
    assert put.intrinsic(560) == pytest.approx(2000.0)


def test_expiry_arithmetic():
    contract = OptionContract("SPY", AS_OF + timedelta(days=30), 580.0, "call")
    assert contract.days_to_expiry(AS_OF) == 30
    assert not contract.is_expired(AS_OF)
    assert contract.is_expired(AS_OF + timedelta(days=31))


# ================================================================== greeks
def test_black_scholes_is_sane():
    call = parse_option("SPY 580 call 2026-12-18")
    greeks = black_scholes(call, 580.0, 0.18, as_of=AS_OF)
    assert greeks.price > 0
    assert 0.4 < greeks.delta < 0.7, greeks.delta  # ATM call
    assert greeks.gamma > 0
    assert greeks.theta < 0, "a long option must lose value with time"
    assert greeks.vega > 0


def test_put_call_parity_holds():
    """C - P = S*e^-qT - K*e^-rT. If this fails the pricer is wrong."""
    call = parse_option("SPY 580 call 2026-12-18")
    put = parse_option("SPY 580 put 2026-12-18")
    spot, rate = 580.0, 0.04
    days = (call.expiry - AS_OF).days
    time_to_expiry = days / 365.0
    left = black_scholes(call, spot, 0.18, AS_OF, rate).price - black_scholes(put, spot, 0.18, AS_OF, rate).price
    right = spot - call.strike * math.exp(-rate * time_to_expiry)
    assert left == pytest.approx(right, abs=0.01)


def test_deep_itm_call_has_delta_near_one():
    call = parse_option("SPY 400 call 2026-12-18")
    assert black_scholes(call, 580.0, 0.18, AS_OF).delta > 0.95


def test_deep_otm_put_has_delta_near_zero():
    put = parse_option("SPY 400 put 2026-12-18")
    assert abs(black_scholes(put, 580.0, 0.18, AS_OF).delta) < 0.05


def test_expired_option_prices_at_intrinsic():
    call = OptionContract("SPY", AS_OF, 560.0, "call")
    greeks = black_scholes(call, 580.0, 0.18, as_of=AS_OF)
    assert greeks.price == pytest.approx(20.0)
    assert greeks.theta == 0.0
    assert "expired" in greeks.source


def test_implied_vol_round_trips():
    call = parse_option("SPY 580 call 2026-12-18")
    for vol in (0.10, 0.18, 0.35, 0.80):
        price = black_scholes(call, 580.0, vol, AS_OF).price
        assert implied_volatility(call, 580.0, price, AS_OF) == pytest.approx(vol, abs=1e-3)


def test_impossible_price_returns_nan_rather_than_a_wrong_number():
    call = parse_option("SPY 580 call 2026-12-18")
    assert math.isnan(implied_volatility(call, 580.0, 0.0001, AS_OF))
    assert math.isnan(implied_volatility(call, 580.0, 10_000.0, AS_OF))


# =================================================================== chain
def test_synthetic_chain_has_no_duplicate_contracts():
    chain = synthetic_chain("SPY", spot=580.0, as_of=AS_OF)
    occs = [row.contract.occ for row in chain.rows]
    assert len(occs) == len(set(occs)), "the chain contains the same contract twice"


def test_expiries_are_distinct_and_in_the_future():
    expiries = _next_monthly_expiries(AS_OF, 4)
    assert len(expiries) == len(set(expiries)) == 4
    assert all(e > AS_OF for e in expiries)
    assert expiries == sorted(expiries)
    assert all(e.weekday() == 4 for e in expiries), "monthly expiry is a Friday"


def test_third_friday_is_right():
    assert third_friday(2026, 10) == date(2026, 10, 16)
    assert third_friday(2026, 1) == date(2026, 1, 16)


def test_chain_has_both_rights_at_every_strike():
    chain = synthetic_chain("SPY", spot=580.0, as_of=AS_OF)
    expiry = chain.expiries[0]
    rows = chain.for_expiry(expiry)
    calls = {r.contract.strike for r in rows if r.contract.right == "call"}
    puts = {r.contract.strike for r in rows if r.contract.right == "put"}
    assert calls == puts and calls


def test_chain_shows_a_volatility_smile():
    """The wings must be dearer in IV terms than the money, or the chain is not
    realistic enough to test strike selection against."""
    chain = synthetic_chain("SPY", spot=580.0, as_of=AS_OF)
    rows = [r for r in chain.for_expiry(chain.expiries[0]) if r.contract.right == "call"]
    atm = min(rows, key=lambda r: abs(r.contract.strike - 580))
    wing = max(rows, key=lambda r: abs(r.contract.strike - 580))
    assert wing.greeks.implied_vol > atm.greeks.implied_vol


def test_at_the_money_selection():
    chain = synthetic_chain("SPY", spot=580.0, as_of=AS_OF)
    atm = chain.at_the_money(right="call")
    assert atm is not None
    assert abs(atm.contract.strike - 580.0) <= 5.0
    assert 0.4 < atm.greeks.delta < 0.65


def test_delta_selection_is_how_traders_actually_choose():
    chain = synthetic_chain("SPY", spot=580.0, as_of=AS_OF)
    row = chain.by_delta(0.25, expiry=chain.expiries[0], right="call")
    assert row is not None
    assert abs(row.greeks.delta - 0.25) < 0.12, row.greeks.delta
    assert row.contract.strike > 580.0, "a 25-delta call should be out of the money"


def test_liquidity_filter_drops_untradeable_contracts():
    from ee_agent.instruments.options import ChainRow, OptionChain

    chain = OptionChain(underlying="SPY", underlying_price=580.0, as_of="now")
    good = ChainRow(parse_option("SPY 580 call 2026-12-18"), bid=10.0, ask=10.2, volume=500, open_interest=5000)
    thin = ChainRow(parse_option("SPY 700 call 2026-12-18"), bid=0.05, ask=0.40, volume=0, open_interest=3)
    chain.rows = [good, thin]
    assert good.tradeable
    assert not thin.tradeable, "a contract with 3 open interest and a 140% spread is not tradeable"
    filtered = chain.tradeable_only()
    assert len(filtered.rows) == 1
    assert "dropped as untradeable" in filtered.notes[-1]


def test_spread_and_mid():
    from ee_agent.instruments.options import ChainRow

    row = ChainRow(parse_option("SPY 580 call 2026-12-18"), bid=10.0, ask=11.0)
    assert row.mid == pytest.approx(10.5)
    assert row.spread_pct == pytest.approx(1.0 / 10.5 * 100)


def test_chain_summary_shows_the_client_the_data():
    chain = synthetic_chain("SPY", spot=580.0, as_of=AS_OF)
    text = chain.summary()
    assert "strike" in text and "delta" in text and "tradeable" in text
    assert "not real data" in text, "a synthetic chain must never read as a market quote"


def test_load_chain_offline_needs_no_key():
    chain = load_chain("SPY", fixtures_only=True, sink=lambda _m: None)
    assert chain.source == "synthetic"
    assert chain.rows


# =========================================================== option candles
def test_option_bars_are_repriced_from_the_underlying():
    from ee_agent.data.loader import load_bars

    spy = load_bars("SPY", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 1500)
    expiry = _next_monthly_expiries(spy.df["ts"].iloc[0].date(), 2)[1]
    contract = parse_option(f"SPY {round(float(spy.close[0]))} call {expiry.isoformat()}")
    bars = synthetic_option_bars(contract, spy, volatility=0.18)

    assert len(bars) > 0
    assert bars.symbol == contract.occ
    assert "repriced-from-SPY" in bars.source, "the source must say these are modelled prices"
    assert (bars.high >= bars.low).all()
    assert (bars.high >= bars.close).all() and (bars.low <= bars.close).all()
    assert (bars.close > 0).all(), "an option premium can never be negative"


def test_a_call_gains_when_the_underlying_rises():
    from ee_agent.data.bars import Bars
    import pandas as pd

    stamps = pd.date_range("2026-03-02 09:30", periods=50, freq="5min", tz="UTC")
    rising = pd.DataFrame(
        {
            "ts": stamps,
            "open": np.linspace(580, 600, 50),
            "high": np.linspace(581, 601, 50),
            "low": np.linspace(579, 599, 50),
            "close": np.linspace(580, 600, 50),
            "volume": [1000.0] * 50,
        }
    )
    underlying = Bars(symbol="SPY", timeframe="5m", df=rising, tz="America/New_York", source="test")
    call = parse_option("SPY 590 call 2026-12-18")
    put = parse_option("SPY 590 put 2026-12-18")
    call_bars = synthetic_option_bars(call, underlying)
    put_bars = synthetic_option_bars(put, underlying)
    assert call_bars.close[-1] > call_bars.close[0], "a call did not gain on a rising underlying"
    assert put_bars.close[-1] < put_bars.close[0], "a put did not lose on a rising underlying"


def test_expired_contract_is_refused_with_a_reason():
    from ee_agent.data.loader import load_bars
    from ee_agent.errors import DataError

    spy = load_bars("SPY", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 100)
    long_gone = parse_option("SPY 580 call 2020-01-17")
    with pytest.raises(DataError) as exc:
        synthetic_option_bars(long_gone, spy)
    assert "expired" in str(exc.value)


def test_loader_routes_an_option_symbol():
    """`load_bars` on an OCC symbol just works -- no caller special-cases it."""
    from ee_agent.data.loader import asset_class_of, load_bars

    assert asset_class_of("SPY261016C00575000") == "option"
    bars = load_bars("SPY261016C00575000", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None)
    assert len(bars) > 0
    assert bars.symbol == "SPY261016C00575000"


def test_load_option_bars_says_the_prices_are_modelled():
    lines: list[str] = []
    load_option_bars("SPY261016C00575000", "5m", fixtures_only=True, sink=lines.append)
    joined = "\n".join(lines)
    assert "MODELLED prices" in joined
    assert "not traded option prices" in joined


# ================================================================ end to end
def test_a_strategy_backtests_on_an_option_contract():
    """The whole point of hard rule 5: the engine does not know it is an option."""
    from ee_agent.data.loader import load_bars
    from ee_agent.engine.backtester import Backtester
    from ee_agent.spec.model import (
        Condition, Costs, Execution, Filters, Risk, SignalRule, Signals, Sizing, StrategySpec, Universe,
    )

    bars = load_bars(
        "SPY261016C00575000", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None
    ).slice(0, 3000)
    spec = StrategySpec(
        id="option-ma-cross",
        name="MA cross on an option contract",
        universe=Universe(instruments=["SPY261016C00575000"], timezone="America/New_York"),
        signals=Signals(
            entry=[
                SignalRule(
                    id="long", side="long", timeframe="5m",
                    all_of=[Condition("ma_cross", {"fast": 9, "slow": 21, "direction": "up"})],
                )
            ]
        ),
        filters=Filters(),
        risk=Risk(
            stop=__import__("ee_agent.spec.model", fromlist=["Distance"]).Distance("percent", 15.0),
            target=__import__("ee_agent.spec.model", fromlist=["Distance"]).Distance("percent", 30.0),
            size=Sizing("fixed_contracts", 1),
        ),
        execution=Execution(),
        costs=Costs(commission_per_side=0.65, slippage_ticks=2.0, spread_ticks=4.0),
    )
    result = Backtester(spec).run(bars)
    assert result.metrics.n_trades > 0, "no trades on an option contract"
    assert result.costs_total > 0, "an option backtest ran without costs"
    for trade in result.trades:
        if trade.closed:
            assert trade.commission > 0


def test_parity_holds_on_an_option_contract():
    """All four targets must agree on an option, same as any other instrument."""
    from ee_agent.data.loader import load_bars
    from ee_agent.parity.harness import run_parity
    from ee_agent.spec.model import (
        Condition, Costs, Distance, Execution, Filters, Risk, SignalRule, Signals, Sizing,
        StrategySpec, Universe,
    )

    bars = load_bars(
        "SPY261016C00575000", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None
    ).slice(0, 2000)
    spec = StrategySpec(
        id="option-parity",
        name="Option parity check",
        universe=Universe(instruments=["SPY261016C00575000"], timezone="America/New_York"),
        signals=Signals(
            entry=[
                SignalRule(
                    id="long", side="long", timeframe="5m",
                    all_of=[Condition("ma_cross", {"fast": 9, "slow": 21, "direction": "up"})],
                )
            ]
        ),
        filters=Filters(),
        risk=Risk(stop=Distance("percent", 15.0), target=Distance("percent", 30.0),
                  size=Sizing("fixed_contracts", 1)),
        execution=Execution(),
        costs=Costs(commission_per_side=0.65, slippage_ticks=2.0, spread_ticks=4.0),
    )
    result = run_parity(spec, bars)
    assert not result.errors, result.errors
    assert result.agreed, "\n".join(d.explain() for d in result.divergences[:5])


def test_option_chain_is_reachable_from_the_conversation():
    import json

    from ee_agent.conversation import tools as toolbox

    result = json.loads(toolbox.call("option_chain", {"underlying": "SPY", "fixtures_only": True}))
    assert "expiries" in result
    assert result["n_contracts"] > 0
