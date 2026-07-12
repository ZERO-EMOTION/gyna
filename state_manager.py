"""
Gyna — state_manager.py
SQLite WAL engine for live stealth position state, atomic mutex locking,
state reconciliation against MT5 terminal, and telemetry buffering.

Separate from trade_log.py (historical memory) — this tracks LIVE positions
and survives process crashes via synchronous WAL writes.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Tuple

log = logging.getLogger("Gyna.StateManager")


class StateManager:
    def __init__(self, db_path: str = "memory/system_state.db"):
        self.db_path = db_path
        self._telemetry_buffer: List[Tuple[float, float, float, float]] = []
        self._buffer_flush_threshold = 10
        self._initialize_database()

    # ── Connection ─────────────────────────────────────────────────────────

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    # ── Schema ─────────────────────────────────────────────────────────────

    def _initialize_database(self) -> None:
        import os
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        statements = [
            """CREATE TABLE IF NOT EXISTS system_metadata (
                key TEXT PRIMARY KEY,
                value_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""",
            """CREATE TABLE IF NOT EXISTS active_stealth_positions (
                ticket_id           INTEGER PRIMARY KEY,
                symbol              TEXT NOT NULL,
                direction           INTEGER NOT NULL,
                volume              REAL NOT NULL,
                entry_price         REAL NOT NULL,
                virtual_sl_points   INTEGER NOT NULL,
                virtual_tp_points   INTEGER NOT NULL,
                snapshot_hash       TEXT NOT NULL,
                timestamp_opened    REAL NOT NULL,
                operational_state   TEXT DEFAULT 'OPEN',
                regime              TEXT,
                session             TEXT,
                execution_profile   TEXT,
                entry_spread_points REAL,
                state_signature     TEXT
            );""",
            """CREATE TABLE IF NOT EXISTS telemetry_ledger (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       REAL NOT NULL,
                latency_ms      REAL NOT NULL,
                slippage_points REAL NOT NULL,
                eqd_coefficient REAL NOT NULL
            );""",
            """CREATE TABLE IF NOT EXISTS closed_trades_ledger (
                ticket_id           INTEGER PRIMARY KEY,
                snapshot_hash       TEXT,
                state_signature     TEXT,
                regime              TEXT,
                session             TEXT,
                direction           INTEGER,
                execution_profile   TEXT,
                entry_spread_points REAL,
                realized_pnl_points REAL,
                avg_latency_ms      REAL,
                avg_slippage_points REAL,
                final_eqd           REAL,
                timestamp_closed    REAL
            );""",
        ]
        with self._get_connection() as conn:
            for stmt in statements:
                conn.execute(stmt)
            # Migrate pre-1.1 schemas: add entry-context columns if missing
            existing = {row[1] for row in
                        conn.execute("PRAGMA table_info(active_stealth_positions)")}
            for col, dtype in (("regime", "TEXT"), ("session", "TEXT"),
                               ("execution_profile", "TEXT"),
                               ("entry_spread_points", "REAL"),
                               ("state_signature", "TEXT")):
                if existing and col not in existing:
                    conn.execute(f"ALTER TABLE active_stealth_positions "
                                 f"ADD COLUMN {col} {dtype}")
            ledger_cols = {row[1] for row in
                           conn.execute("PRAGMA table_info(closed_trades_ledger)")}
            if ledger_cols and "state_signature" not in ledger_cols:
                conn.execute("ALTER TABLE closed_trades_ledger "
                             "ADD COLUMN state_signature TEXT")
            conn.commit()

    # ── System context ─────────────────────────────────────────────────────

    def save_system_context(self, consecutive_losses: int,
                            daily_loss_pct: float) -> None:
        data = {"consecutive_losses": consecutive_losses,
                "daily_realized_loss_pct": daily_loss_pct}
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO system_metadata (key, value_json, updated_at) "
                "VALUES ('risk_context', ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=CURRENT_TIMESTAMP;",
                (json.dumps(data),)
            )
            conn.commit()

    def load_system_context(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT value_json FROM system_metadata WHERE key='risk_context';")
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
        return {"consecutive_losses": 0, "daily_realized_loss_pct": 0.0}

    # ── Stealth positions ──────────────────────────────────────────────────

    def register_stealth_position(self, ticket_id: int,
                                  pos: Dict[str, Any]) -> None:
        """Synchronous write — crash-safe. Called immediately after MT5 fill."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO active_stealth_positions "
                "(ticket_id, symbol, direction, volume, entry_price, "
                "virtual_sl_points, virtual_tp_points, snapshot_hash, "
                "timestamp_opened, operational_state, regime, session, "
                "execution_profile, entry_spread_points, state_signature) "
                "VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?);",
                (ticket_id, pos["symbol"], pos["direction"], pos["volume"],
                 pos["entry_price"], pos["virtual_sl_points"],
                 pos["virtual_tp_points"], pos["snapshot_hash"],
                 pos.get("timestamp_opened", time.time()),
                 pos.get("regime"), pos.get("session"),
                 pos.get("execution_profile"),
                 pos.get("entry_spread_points"),
                 pos.get("state_signature"))
            )
            conn.commit()

    def lock_position_for_closure(self, ticket_id: int) -> bool:
        """
        Atomic state transition OPEN → PENDING_CLOSE.
        Returns False if already locked (prevents duplicate close race conditions).
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT operational_state FROM active_stealth_positions "
                "WHERE ticket_id=?;", (ticket_id,))
            row = cur.fetchone()
            if not row or row[0] == "PENDING_CLOSE":
                return False
            conn.execute(
                "UPDATE active_stealth_positions SET operational_state='PENDING_CLOSE' "
                "WHERE ticket_id=?;", (ticket_id,))
            conn.commit()
        return True

    def release_position_lock(self, ticket_id: int) -> None:
        """Revert PENDING_CLOSE → OPEN if broker close fails."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE active_stealth_positions SET operational_state='OPEN' "
                "WHERE ticket_id=?;", (ticket_id,))
            conn.commit()

    def remove_stealth_position(self, ticket_id: int) -> None:
        """Synchronous purge after confirmed close."""
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM active_stealth_positions WHERE ticket_id=?;",
                (ticket_id,))
            conn.commit()

    def get_all_active_stealth_positions(self) -> Dict[int, Dict[str, Any]]:
        positions: Dict[int, Dict[str, Any]] = {}
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM active_stealth_positions;")
            for row in cur.fetchall():
                positions[row["ticket_id"]] = dict(row)
        return positions

    # ── Closed trades ledger (feeds post_trade_analytics) ──────────────────

    def record_closed_trade(self, ticket_id: int, pos: Dict[str, Any],
                            realized_pnl_points: float,
                            avg_latency_ms: float,
                            avg_slippage_points: float,
                            final_eqd: float) -> None:
        """Persist a closed trade so the post-trade audit has data to profile."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO closed_trades_ledger "
                "(ticket_id, snapshot_hash, state_signature, regime, session, "
                "direction, execution_profile, entry_spread_points, "
                "realized_pnl_points, avg_latency_ms, avg_slippage_points, "
                "final_eqd, timestamp_closed) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?);",
                (ticket_id, pos.get("snapshot_hash"),
                 pos.get("state_signature"), pos.get("regime"),
                 pos.get("session"), pos.get("direction"),
                 pos.get("execution_profile"),
                 pos.get("entry_spread_points"),
                 realized_pnl_points, avg_latency_ms, avg_slippage_points,
                 final_eqd, time.time())
            )
            conn.commit()

    def reconcile_state_matrices(
            self, mt5_positions: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Synchronization barrier: compares MT5 terminal reality vs local SQLite.
        Returns reconciliation report with required actions.
        Run on every bootstrap_system() call.
        """
        db_positions = self.get_all_active_stealth_positions()
        report: Dict[str, Any] = {"status": "SYNCHRONIZED", "actions_required": []}
        all_tickets = set(mt5_positions.keys()) | set(db_positions.keys())

        for ticket in all_tickets:
            in_mt5 = ticket in mt5_positions
            in_db  = ticket in db_positions

            if in_mt5 and in_db:
                # Volume drift check
                vol_drift = abs(mt5_positions[ticket]["volume"] -
                                db_positions[ticket]["volume"])
                if vol_drift > 1e-5:
                    report["status"] = "DRIFT_DETECTED"
                    report["actions_required"].append({
                        "ticket_id": ticket,
                        "anomaly":   "VOLUME_MISMATCH",
                        "action":    "REPAIR_DB_METRICS",
                        "target_volume": mt5_positions[ticket]["volume"],
                    })
            elif in_mt5 and not in_db:
                report["status"] = "DRIFT_DETECTED"
                report["actions_required"].append({
                    "ticket_id": ticket,
                    "anomaly":   "UNKNOWN_TERMINAL_POSITION",
                    "action":    "FORCE_IMPORT_REBUILD",
                    "payload":   mt5_positions[ticket],
                })
            elif in_db and not in_mt5:
                report["status"] = "DRIFT_DETECTED"
                report["actions_required"].append({
                    "ticket_id": ticket,
                    "anomaly":   "ORPHANED_DATABASE_RECORD",
                    "action":    "PURGE_STALE_RECORD",
                })
        return report

    # ── Telemetry ──────────────────────────────────────────────────────────

    def buffer_telemetry_metric(self, latency_ms: float,
                                slippage_points: float, eqd: float) -> None:
        """Non-critical path: batch writes to avoid high-frequency file locks."""
        self._telemetry_buffer.append(
            (time.time(), latency_ms, slippage_points, eqd))
        if len(self._telemetry_buffer) >= self._buffer_flush_threshold:
            self.flush_telemetry_buffer()

    def flush_telemetry_buffer(self) -> None:
        if not self._telemetry_buffer:
            return
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT INTO telemetry_ledger "
                "(timestamp, latency_ms, slippage_points, eqd_coefficient) "
                "VALUES (?,?,?,?);",
                self._telemetry_buffer
            )
            conn.commit()
        self._telemetry_buffer.clear()

    def load_historical_telemetry(self, limit: int = 20) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT latency_ms, slippage_points, eqd_coefficient "
                "FROM telemetry_ledger ORDER BY id DESC LIMIT ?;", (limit,))
            rows = cur.fetchall()
        if not rows:
            return {"latency": [], "slippage": [], "avg_eqd": 0.0}
        return {
            "latency":  [r[0] for r in rows][::-1],
            "slippage": [r[1] for r in rows][::-1],
            "avg_eqd":  float(rows[0][2]),
        }
