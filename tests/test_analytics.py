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
