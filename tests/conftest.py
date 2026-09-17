"""Test fixtures. Everything here runs with zero credentials and no network."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture(scope="session", autouse=True)
def isolated_home(tmp_path_factory):
    """Never write into the developer's real EE_HOME during tests."""
    home = tmp_path_factory.mktemp("ee-home")
    os.environ["EE_HOME"] = str(home)
    import ee_agent.paths as paths

    paths.ee_home.cache_clear() if hasattr(paths.ee_home, "cache_clear") else None
    yield home


@pytest.fixture(scope="session", autouse=True)
def silent_cost_ledger():
    from ee_agent.cost.notifier import CostLedger, set_ledger

    set_ledger(CostLedger(notify_threshold_usd=10**9, sink=lambda _m: None))


@pytest.fixture
def spec():
    from ee_agent.spec.model import StrategySpec

    return StrategySpec.load(REPO / "tests/fixtures/specs/sweep-return-atr.yaml")


@pytest.fixture
def owner_spec():
    from ee_agent.spec.model import StrategySpec

    return StrategySpec.load(REPO / "library/sweep-return-v1/spec.yaml")


@pytest.fixture
def bars():
    from ee_agent.data.loader import load_bars

    return load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None)


@pytest.fixture
def small_bars(bars):
    return bars.slice(0, 3000)


@pytest.fixture
def crypto_bars():
    from ee_agent.data.loader import load_bars

    return load_bars("BTCUSDT", "15m", fixtures_only=True, use_cache=False, sink=lambda _m: None)
