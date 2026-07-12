"""
Gyna — tests/test_state_manager.py
StateManager: stealth positions, closure locking, closed-trades ledger.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from state_manager import StateManager


POS = {
    "symbol": "BTCUSD", "direction": 1, "volume": 0.01,
    "entry_price": 60000.0, "virtual_sl_points": 500,
    "virtual_tp_points": 1000, "snapshot_hash": "abc123",
    "state_signature": "sig456", "regime": "TREND", "session": "NY",
    "execution_profile": "CONSERVATIVE", "entry_spread_points": 1200.0,
    "timestamp_opened": 1700000000.0,
}


@pytest.fixture
def db(tmp_path):
    return StateManager(str(tmp_path / "state.db"))


def test_register_and_read_back(db):
    db.register_stealth_position(1, POS)
    positions = db.get_all_active_stealth_positions()
    assert 1 in positions
    p = positions[1]
    assert p["state_signature"] == "sig456"
    assert p["regime"] == "TREND"
    assert p["operational_state"] == "OPEN"


def test_closure_lock_is_exclusive(db):
    db.register_stealth_position(2, POS)
    assert db.lock_position_for_closure(2) is True
    assert db.lock_position_for_closure(2) is False   # already locked
    db.release_position_lock(2)
    assert db.lock_position_for_closure(2) is True    # lock released
    db.remove_stealth_position(2)
    assert db.lock_position_for_closure(2) is False   # gone


def test_record_closed_trade_feeds_ledger(db):
    db.register_stealth_position(3, POS)
    db.record_closed_trade(3, POS, realized_pnl_points=250.0,
                           avg_latency_ms=80.0, avg_slippage_points=1.5,
                           final_eqd=0.1)
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT state_signature, regime, realized_pnl_points "
            "FROM closed_trades_ledger WHERE ticket_id=3").fetchone()
    assert row == ("sig456", "TREND", 250.0)


def test_reconcile_detects_orphan_and_unknown(db):
    db.register_stealth_position(10, POS)          # in DB, not in MT5
    mt5_positions = {20: {"volume": 0.02}}          # in MT5, not in DB
    report = db.reconcile_state_matrices(mt5_positions)
    assert report["status"] == "DRIFT_DETECTED"
    actions = {a["ticket_id"]: a["action"] for a in report["actions_required"]}
    assert actions[10] == "PURGE_STALE_RECORD"
    assert actions[20] == "FORCE_IMPORT_REBUILD"


def test_schema_migration_from_v1(tmp_path):
    """A pre-1.1 DB (no entry-context columns) must open and upgrade cleanly."""
    import sqlite3
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE active_stealth_positions (
            ticket_id INTEGER PRIMARY KEY, symbol TEXT NOT NULL,
            direction INTEGER NOT NULL, volume REAL NOT NULL,
            entry_price REAL NOT NULL, virtual_sl_points INTEGER NOT NULL,
            virtual_tp_points INTEGER NOT NULL, snapshot_hash TEXT NOT NULL,
            timestamp_opened REAL NOT NULL,
            operational_state TEXT DEFAULT 'OPEN');""")
        conn.execute("INSERT INTO active_stealth_positions VALUES "
                     "(7,'BTCUSD',1,0.01,60000.0,500,1000,'h',0,'OPEN')")
        conn.commit()
    db = StateManager(path)
    positions = db.get_all_active_stealth_positions()
    assert positions[7]["state_signature"] is None   # migrated column exists
    db.register_stealth_position(8, POS)             # new-format insert works
    assert db.get_all_active_stealth_positions()[8]["regime"] == "TREND"
