"""
Gyna — tests/test_analytics.py
PostTradeValidationEngine: report generation from a populated ledger,
toxic state-signature detection.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import pytest
from state_manager import StateManager
from post_trade_analytics import PostTradeValidationEngine


def _pos(sig, regime="TREND", profile="CONSERVATIVE"):
    return {"symbol": "BTCUSD", "direction": 1, "volume": 0.01,
            "entry_price": 60000.0, "virtual_sl_points": 500,
            "virtual_tp_points": 1000, "snapshot_hash": f"hash-{time.time()}",
            "state_signature": sig, "regime": regime, "session": "NY",
            "execution_profile": profile, "entry_spread_points": 1000.0,
            "timestamp_opened": time.time()}


@pytest.fixture
def populated(tmp_path):
    path = str(tmp_path / "state.db")
    db = StateManager(path)
    ticket = 0
    # A recurring toxic state: 4 trades, net negative
    for pnl in (-100.0, -80.0, 30.0, -60.0):
        ticket += 1
        db.record_closed_trade(ticket, _pos("toxic-sig"), pnl, 90.0, 2.0, 0.15)
    # A recurring profitable state: 3 trades, net positive
    for pnl in (120.0, 90.0, -40.0):
        ticket += 1
        db.record_closed_trade(ticket, _pos("good-sig"), pnl, 70.0, 1.0, 0.05)
    # A one-off state (below the >=3 recurrence threshold)
    ticket += 1
    db.record_closed_trade(ticket, _pos("rare-sig"), -500.0, 70.0, 1.0, 0.05)
    return PostTradeValidationEngine(path)


def test_report_profiles_populated_ledger(populated):
    report = populated.generate_report()
    assert report["status"] == "PROFILED"
    assert report["global_metrics"]["total_trades"] == 8


def test_toxic_signature_detected(populated):
    report = populated.generate_report()
    assert "toxic-sig" in report["toxic_state_signatures"]
    assert "good-sig" not in report["toxic_state_signatures"]
    # one-off loser must not qualify (needs >= 3 recurrences)
    assert "rare-sig" not in report["toxic_state_signatures"]


def test_legacy_alias_key_present(populated):
    report = populated.generate_report()
    assert report["toxic_snapshot_hashes"] == report["toxic_state_signatures"]


def test_empty_ledger_reports_insufficient_data(tmp_path):
    path = str(tmp_path / "empty.db")
    StateManager(path)
    engine = PostTradeValidationEngine(path)
    assert engine.generate_report()["status"] == "INSUFFICIENT_DATA"


def test_empirical_kill_hours_guards():
    from post_trade_analytics import empirical_kill_hours
    report = {"hourly_pnl_distribution": {
        3:  {"sum": -500.0, "count": 15, "mean": -33.3},   # qualifies
        7:  {"sum": -900.0, "count": 12, "mean": -75.0},   # qualifies (worst)
        9:  {"sum": -50.0,  "count": 4,  "mean": -12.5},   # too few trades
        14: {"sum": +300.0, "count": 30, "mean": 10.0},    # profitable
        21: {"sum": -100.0, "count": 11, "mean": -9.1},    # qualifies
        22: {"sum": -80.0,  "count": 10, "mean": -8.0},    # qualifies
        23: {"sum": -60.0,  "count": 10, "mean": -6.0},    # over max_hours cap
    }}
    hours = empirical_kill_hours(report, min_trades=10, max_hours=4)
    assert hours == [7, 3, 21, 22]        # worst-first, capped at 4
    assert 9 not in hours and 14 not in hours and 23 not in hours


def test_empirical_kill_hours_empty_report():
    from post_trade_analytics import empirical_kill_hours
    assert empirical_kill_hours({}) == []
    assert empirical_kill_hours({"status": "INSUFFICIENT_DATA"}) == []
