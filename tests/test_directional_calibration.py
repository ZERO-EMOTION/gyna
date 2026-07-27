"""
Gyna — tests/test_directional_calibration.py
Read-only directional mirror: flags states where one side is inverted.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from state_manager import StateManager
from learning_brain import featurize
from directional_calibration import calibration_report, reflection_note


def _pos(direction, regime="TREND", session="NY", style="runner", sig="s"):
    return {"symbol": "XAUUSD", "direction": direction, "volume": 0.01,
            "entry_price": 3000.0, "virtual_sl_points": 500,
            "virtual_tp_points": 1000, "snapshot_hash": "h",
            "state_signature": sig, "regime": regime, "session": session,
            "execution_profile": "CONSERVATIVE", "entry_spread_points": 20.0,
            "style": style}


@pytest.fixture
def db(tmp_path):
    return StateManager(str(tmp_path / "state.db"))


def _add_taken(db, ticket, direction, pnl, **kw):
    db.record_closed_trade(ticket, _pos(direction, **kw),
                           realized_pnl_points=pnl, avg_latency_ms=50.0,
                           avg_slippage_points=1.0, final_eqd=0.1)


def test_flags_inverted_direction(db):
    # Same state: SELL wins consistently, BUY loses consistently
    t = 0
    for _ in range(10):
        t += 1; _add_taken(db, t, -1, +30.0)   # SELL wins
    for _ in range(10):
        t += 1; _add_taken(db, t, 1, -30.0)    # BUY loses
    rep = calibration_report(db.db_path)
    assert len(rep["flags"]) == 1
    f = rep["flags"][0]
    assert f["winning_direction"] == "SELL"
    assert f["losing_direction"] == "BUY"
    assert "inverted" in f["note"]


def test_no_flag_when_both_sides_similar(db):
    t = 0
    for i in range(12):
        t += 1; _add_taken(db, t, 1, 10.0 if i % 2 else -10.0)   # BUY ~50%
    for i in range(12):
        t += 1; _add_taken(db, t, -1, 10.0 if i % 2 else -10.0)  # SELL ~50%
    assert calibration_report(db.db_path)["flags"] == []


def test_no_flag_below_min_samples(db):
    # Strong inversion but tiny sample -> not trustworthy, no flag
    _add_taken(db, 1, -1, +30.0)
    _add_taken(db, 2, 1, -30.0)
    assert calibration_report(db.db_path)["flags"] == []


def test_shadow_trades_included(db, tmp_path):
    # Ensure the shadow_trades table exists (created by ShadowLearner)
    from shadow_learner import ShadowLearner
    from learning_brain import GynaBrain
    ShadowLearner(db.db_path, GynaBrain(path=str(tmp_path / "b.json")))
    # Only shadow (skipped) data, decoded from brain_features
    import sqlite3, time
    snap = {"regime": "RANGE", "session": "LONDON", "style": "scalper",
            "permitted_direction": 1, "rsi_dist_50": 5.0, "macd_hist_z": 0.5,
            "bb_position": 0.6, "atr_percentile": 50.0,
            "structure_strength": 0.7, "volume_z": 1.0,
            "regime_fatigue_factor": 0.0, "consec_dir_closes": 3,
            "burst_range_ratio": 1.3, "last_close_pos": 0.8,
            "edge_quality_score": 0.6, "transition_frequency": 0.05,
            "swept_low": False, "swept_high": False}
    conn = sqlite3.connect(db.db_path)
    def ins(direction, outcome):
        s = dict(snap, permitted_direction=direction)
        conn.execute("INSERT INTO shadow_trades (timestamp, style, direction, "
                     "entry_price, sl_price, tp_price, brain_features, "
                     "skip_reason, resolved, outcome) VALUES (?,?,?,?,?,?,?,?,1,?)",
                     (time.time(), "scalper", direction, 3000.0, 2990.0, 3010.0,
                      json.dumps(featurize(s).tolist()), "brain_veto", outcome))
    for _ in range(9): ins(1, "win")     # BUY wins in RANGE/LONDON/scalper
    for _ in range(9): ins(-1, "loss")   # SELL loses
    conn.commit(); conn.close()
    rep = calibration_report(db.db_path)
    flg = [f for f in rep["flags"] if f["regime"] == "RANGE"]
    assert flg and flg[0]["winning_direction"] == "BUY"


def test_reflection_note_empty_when_clean(db):
    _add_taken(db, 1, 1, 10.0)
    assert reflection_note(db.db_path) == ""


def test_reflection_note_populated_when_flagged(db):
    t = 0
    for _ in range(10):
        t += 1; _add_taken(db, t, -1, +30.0)
    for _ in range(10):
        t += 1; _add_taken(db, t, 1, -30.0)
    note = reflection_note(db.db_path)
    assert "DIRECTIONAL CALIBRATION" in note and "inverted" in note
