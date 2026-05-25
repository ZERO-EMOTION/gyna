"""
Gyna — memory/trade_log.py
SQLite persistent trade memory — full schema with all columns from blueprint.
Never deletes. Compounding intelligence over time.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional


class TradeMemory:
    def __init__(self, db_path: str = "memory/gyna_trades.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # dict-like rows
        self._create_tables()
        self._migrate()

    def _create_tables(self):
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                direction     TEXT NOT NULL,    -- BUY | SELL | HOLD
                entry_price   REAL,
                exit_price    REAL,
                volume        REAL,
                sl            REAL,
                tp            REAL,
                sl_atr_mult   REAL,
                tp_atr_mult   REAL,
                pnl_pips      REAL DEFAULT 0,
                pnl_usd       REAL DEFAULT 0,
                rationale     TEXT,             -- Claude plain-English reason
                regime        TEXT,             -- trend | range | volatile
                session       TEXT,             -- london | ny | asia | overlap
                rsi           REAL,
                macd_hist     REAL,
                bb_position   REAL,             -- 0.0=lower band, 1.0=upper band
                hma_trend     TEXT,             -- bullish | bearish | flat
                atr           REAL,
                hhll_bias     TEXT,             -- bullish | bearish | neutral
                confidence    REAL,             -- Claude confidence 0.0-1.0
                key_risk      TEXT,             -- what would invalidate this trade
                risk_tier     INTEGER,          -- 1-5
                risk_pct      REAL,             -- actual risk used
                outcome       TEXT DEFAULT 'open', -- win | loss | be | open
                mt5_ticket    INTEGER,          -- broker ticket number
                reflection    TEXT,             -- added by weekly reflection engine
                closed_at     TEXT              -- timestamp when position closed
            );

            CREATE TABLE IF NOT EXISTS reflections (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                period_start  TEXT NOT NULL,
                period_end    TEXT NOT NULL,
                n_trades      INTEGER,
                win_rate      REAL,
                profit_factor REAL,
                risk_tier     INTEGER,
                content       TEXT NOT NULL     -- Claude's reflection text
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date          TEXT PRIMARY KEY,
                n_trades      INTEGER DEFAULT 0,
                pnl_usd       REAL DEFAULT 0,
                halted        INTEGER DEFAULT 0  -- 1 if daily loss limit hit
            );
        ''')
        self.conn.commit()

    def _migrate(self):
        """Add any missing columns to existing DB (safe for upgrades)."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)")}
        new_cols = {
            "sl_atr_mult":  "REAL",
            "tp_atr_mult":  "REAL",
            "bb_position":  "REAL",
            "hma_trend":    "TEXT",
            "hhll_bias":    "TEXT",
            "key_risk":     "TEXT",
            "risk_tier":    "INTEGER",
            "risk_pct":     "REAL",
            "mt5_ticket":   "INTEGER",
            "closed_at":    "TEXT",
            "macd_hist":    "REAL",
            "atr":          "REAL",
        }
        for col, dtype in new_cols.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
        self.conn.commit()

    # ── Write ──────────────────────────────────────────────────────────────

    def log_trade(self, data: dict) -> int:
        """Insert a new trade record. Returns the new trade id."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute('''
            INSERT INTO trades (
                timestamp, symbol, direction, entry_price, volume,
                sl, tp, sl_atr_mult, tp_atr_mult,
                rationale, regime, session,
                rsi, macd_hist, bb_position, hma_trend, atr, hhll_bias,
                confidence, key_risk, risk_tier, risk_pct, outcome, mt5_ticket
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            now,
            data.get("symbol", "BTCUSD"),
            data.get("direction", "HOLD"),
            data.get("entry_price"),
            data.get("volume"),
            data.get("sl"),
            data.get("tp"),
            data.get("sl_atr_mult"),
            data.get("tp_atr_mult"),
            data.get("rationale"),
            data.get("regime"),
            data.get("session"),
            data.get("rsi"),
            data.get("macd_hist"),
            data.get("bb_position"),
            data.get("hma_trend"),
            data.get("atr"),
            data.get("hhll_bias"),
            data.get("confidence"),
            data.get("key_risk"),
            data.get("risk_tier"),
            data.get("risk_pct"),
            data.get("outcome", "open"),
            data.get("mt5_ticket"),
        ))
        self.conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float,
                    pnl_pips: float, pnl_usd: float, outcome: str):
        """Mark a trade as closed with final PnL."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute('''
            UPDATE trades
            SET exit_price=?, pnl_pips=?, pnl_usd=?, outcome=?, closed_at=?
            WHERE id=?
        ''', (exit_price, pnl_pips, pnl_usd, outcome, now, trade_id))
        self.conn.commit()
        self._update_daily_stats(pnl_usd)

    def _update_daily_stats(self, pnl_usd: float):
        today = datetime.now(timezone.utc).date().isoformat()
        self.conn.execute('''
            INSERT INTO daily_stats (date, n_trades, pnl_usd)
            VALUES (?, 1, ?)
            ON CONFLICT(date) DO UPDATE SET
                n_trades = n_trades + 1,
                pnl_usd  = pnl_usd + excluded.pnl_usd
        ''', (today, pnl_usd))
        self.conn.commit()

    def log_reflection(self, data: dict):
        self.conn.execute('''
            INSERT INTO reflections
                (timestamp, period_start, period_end, n_trades,
                 win_rate, profit_factor, risk_tier, content)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            data["period_start"], data["period_end"],
            data.get("n_trades"), data.get("win_rate"),
            data.get("profit_factor"), data.get("risk_tier"),
            data["content"],
        ))
        self.conn.commit()

    # ── Read ───────────────────────────────────────────────────────────────

    def get_recent_losses(self, n: int = 10) -> list[dict]:
        """Return last N losing trades with key fields for Claude context."""
        cur = self.conn.execute('''
            SELECT timestamp, symbol, direction, entry_price, exit_price,
                   pnl_usd, rationale, regime, session, rsi, hma_trend,
                   hhll_bias, confidence, key_risk
            FROM trades
            WHERE outcome = 'loss'
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (n,))
        return [dict(row) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        """Compute current win rate, profit factor, trade count for risk tier."""
        cur = self.conn.execute('''
            SELECT
                COUNT(*)                                          AS total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END)   AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)  AS losses,
                SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END) AS gross_profit,
                SUM(CASE WHEN pnl_usd < 0 THEN ABS(pnl_usd) ELSE 0 END) AS gross_loss,
                SUM(pnl_usd)                                      AS net_pnl
            FROM trades
            WHERE outcome IN ('win','loss','be')
        ''')
        row = dict(cur.fetchone())
        total  = row["total"] or 0
        wins   = row["wins"]  or 0
        gp     = row["gross_profit"] or 0
        gl     = row["gross_loss"]   or 1  # avoid div/0
        return {
            "total_trades":  total,
            "win_rate":      wins / total if total > 0 else 0.0,
            "profit_factor": gp / gl,
            "net_pnl":       row["net_pnl"] or 0.0,
        }

    def get_daily_pnl(self, date: Optional[str] = None) -> float:
        """Return today's PnL in USD."""
        d = date or datetime.now(timezone.utc).date().isoformat()
        cur = self.conn.execute(
            "SELECT pnl_usd FROM daily_stats WHERE date=?", (d,))
        row = cur.fetchone()
        return row["pnl_usd"] if row else 0.0

    def is_daily_halted(self, date: Optional[str] = None) -> bool:
        d = date or datetime.now(timezone.utc).date().isoformat()
        cur = self.conn.execute(
            "SELECT halted FROM daily_stats WHERE date=?", (d,))
        row = cur.fetchone()
        return bool(row["halted"]) if row else False

    def set_daily_halt(self, date: Optional[str] = None):
        d = date or datetime.now(timezone.utc).date().isoformat()
        self.conn.execute('''
            INSERT INTO daily_stats (date, halted)
            VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET halted=1
        ''', (d,))
        self.conn.commit()

    def get_open_trades(self) -> list[dict]:
        cur = self.conn.execute('''
            SELECT * FROM trades WHERE outcome='open'
            ORDER BY timestamp DESC
        ''')
        return [dict(row) for row in cur.fetchall()]

    def get_recent_reflections(self, n: int = 3) -> list[dict]:
        cur = self.conn.execute('''
            SELECT * FROM reflections ORDER BY timestamp DESC LIMIT ?
        ''', (n,))
        return [dict(row) for row in cur.fetchall()]

    def get_trades_for_period(self, start: str, end: str) -> list[dict]:
        cur = self.conn.execute('''
            SELECT * FROM trades
            WHERE timestamp >= ? AND timestamp <= ?
              AND outcome IN ('win','loss','be')
            ORDER BY timestamp ASC
        ''', (start, end))
        return [dict(row) for row in cur.fetchall()]
