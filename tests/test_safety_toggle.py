"""
Gyna — tests/test_safety_toggle.py
Master safety toggle: every protective gate obeys it; sizing clamps do not.
Plus per-symbol profile selection.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from risk_engine import RiskEngine
from edge_engine import EdgeEngine
from config import MAX_DAILY_LOSS, MAX_RISK_PER_TRADE, SYMBOL_PROFILES


def _allocation():
    return {"permitted_direction": 1, "execution_profile": "CONSERVATIVE",
            "aggression_multiplier": 0.5, "sl_atr_target": 1.5,
            "tp_atr_target": 3.0, "snapshot_hash": "t"}


def _account(daily_loss=0.0):
    return {"balance": 10000.0, "equity": 10000.0, "free_margin": 9000.0,
            "leverage": 100.0, "daily_realized_loss_pct": daily_loss,
            "current_spread_points": 2.0, "min_lot_limit": 0.01,
            "max_lot_limit": 100.0, "SYMBOL_MARGIN_INITIAL": 0.0,
            "SYMBOL_TRADE_CONTRACT_SIZE": 1.0, "SYMBOL_TRADE_TICK_VALUE": 1.0,
            "SYMBOL_TRADE_TICK_SIZE": 0.01, "consecutive_losses": 0,
            "execution_quality_degradation": 0.0}


def _snapshot():
    return {"atr_14": 500.0, "price": 60000.0, "symbol": "BTCUSD",
            "trade_memory": {}}


def test_breaker_blocks_when_safety_on():
    eng = RiskEngine(safety_filters=True)
    out = eng.authorize_execution(_allocation(),
                                  _account(daily_loss=MAX_DAILY_LOSS + 0.01),
                                  _snapshot(), {})
    assert out["status"] == "REJECTED_DRAWDOWN_BREAKER_ACTIVE"


def test_breaker_ignored_when_safety_off():
    eng = RiskEngine(safety_filters=False)
    out = eng.authorize_execution(_allocation(),
                                  _account(daily_loss=MAX_DAILY_LOSS + 0.01),
                                  _snapshot(), {})
    assert out["status"] == "APPROVED"


def test_loss_halving_and_eqd_skipped_when_safety_off():
    acct = _account()
    acct["consecutive_losses"] = 2
    acct["execution_quality_degradation"] = 0.4
    # Full aggression so the 0.1% risk floor doesn't mask the difference
    alloc = dict(_allocation(), aggression_multiplier=1.0)
    on  = RiskEngine(safety_filters=True).authorize_execution(
        dict(alloc), dict(acct), _snapshot(), {})
    off = RiskEngine(safety_filters=False).authorize_execution(
        dict(alloc), dict(acct), _snapshot(), {})
    assert on["status"] == off["status"] == "APPROVED"
    assert off["risk_pct"] > on["risk_pct"]   # no halving, no EQD penalty


def test_max_risk_clamp_survives_safety_off():
    """Sizing clamps are physics, not filters — the toggle must not touch them."""
    alloc = dict(_allocation(), aggression_multiplier=1.0)
    out = RiskEngine(safety_filters=False).authorize_execution(
        alloc, _account(), _snapshot(), {})
    assert out["status"] == "APPROVED"
    assert out["risk_pct"] <= MAX_RISK_PER_TRADE + 1e-9


def test_edge_three_loss_flatten_obeys_toggle():
    snap = {"regime": "TREND", "session": "LONDON", "hma_trend": "BULLISH",
            "hhll_bias": "BULLISH", "structure_strength": 0.8,
            "macd_hist_z": 1.0, "rsi_dist_50": 15.0, "regime_duration": 5,
            "atr_percentile": 50.0, "volume_z": 1.0}
    perf = {"current_drawdown_pct": 0.0, "consecutive_losses": 3,
            "win_rate_calibrated": 0.5, "eqd_coefficient": 0.0}
    assert EdgeEngine(safety_filters=True).process_state(
        dict(snap), perf)["permitted_direction"] == 0
    assert EdgeEngine(safety_filters=False).process_state(
        dict(snap), perf)["permitted_direction"] == 1


def test_symbol_profiles_exist_for_both_targets():
    for sym in ("XAUUSD", "BTCUSD"):
        prof = SYMBOL_PROFILES[sym]
        for key in ("max_spread_points", "cooldown_s", "feed_staleness_s",
                    "always_open", "closed_utc_hours"):
            assert key in prof
    assert SYMBOL_PROFILES["BTCUSD"]["always_open"] is True
    assert SYMBOL_PROFILES["XAUUSD"]["always_open"] is False
    # Gold's spread cap must be gold-sized, not BTC-sized
    assert SYMBOL_PROFILES["XAUUSD"]["max_spread_points"] < \
           SYMBOL_PROFILES["BTCUSD"]["max_spread_points"]
