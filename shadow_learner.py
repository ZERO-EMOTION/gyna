"""
Gyna — shadow_learner.py
Learning from the trades we DIDN'T take.

JP's principle: "skipping just means we haven't figured out whether it's a
BUY or SELL. We can always look back and figure things out — that's how we
learn to deal with the SKIP next time."

Every time the pipeline produces a real candidate signal that gets skipped
downstream (brain veto, LLM said FLAT, risk-engine rejection, failed order),
the full context is recorded as a SHADOW TRADE: features, direction, and the
virtual SL/TP it would have carried. Later, price history answers what would
have happened — and that hindsight label is fed to GynaBrain at reduced
weight (counterfactuals carry no slippage/execution reality, so they must
never outvote real fills).

Result: the brain learns from every skip — either the skip was wisdom
(reinforced) or money left on the table (the rising P(win) for that state
eventually overrides the hesitation). Gyna skips far more signals than it
takes, so this multiplies the learning rate severalfold.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("Gyna.Shadow")

SHADOW_WEIGHT      = 0.3    # counterfactual lessons vs 1.0 for real fills
TIMEOUT_BARS       = 240    # unresolved after 4h of M1 -> ambiguous, no label
MIN_AGE_S          = 600    # wait >=10 min before trying to resolve
RESOLVE_BATCH      = 50


class ShadowLearner:
    def __init__(self, db_path: str, brain):
        self.db_path = db_path
        self.brain   = brain
        self._init_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_table(self) -> None:
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS shadow_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      REAL NOT NULL,
                style          TEXT,
                direction      INTEGER NOT NULL,
                entry_price    REAL NOT NULL,
                sl_price       REAL NOT NULL,
                tp_price       REAL NOT NULL,
                brain_features TEXT NOT NULL,
                skip_reason    TEXT NOT NULL,
                resolved       INTEGER DEFAULT 0,
                outcome        TEXT
            );""")
            conn.commit()

    # ── Recording (called at every downstream skip of a real signal) ───────

    def record(self, masked: Dict[str, Any], features_vec,
               skip_reason: str) -> None:
        """Store the road not taken. masked = post-EdgeEngine snapshot."""
        try:
            direction = int(masked.get("permitted_direction", 0))
            if direction == 0:
                return
            atr    = float(masked.get("atr_14") or 0.0)
            price  = float(masked.get("price") or 0.0)
            if atr <= 0 or price <= 0:
                return
            sl_mid = sum(masked.get("allowed_sl_atr_range", [1, 2])) / 2 * atr
            tp_mid = sum(masked.get("allowed_tp_atr_range", [2, 4])) / 2 * atr
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO shadow_trades (timestamp, style, direction, "
                    "entry_price, sl_price, tp_price, brain_features, "
                    "skip_reason) VALUES (?,?,?,?,?,?,?,?);",
                    (time.time(), masked.get("style"), direction, price,
                     price - sl_mid * direction, price + tp_mid * direction,
                     json.dumps(list(features_vec)), skip_reason))
                conn.commit()
            log.info(f"[SHADOW] Recorded skipped {masked.get('style')} "
                     f"{'BUY' if direction == 1 else 'SELL'} "
                     f"({skip_reason}) — hindsight will label it")
        except Exception as e:
            log.warning(f"[SHADOW] Record failed: {e}")

    # ── Resolution (hindsight labeling) ────────────────────────────────────

    def resolve_due(self, fetch_bars: Callable[[float], Optional[Any]],
                    now: Optional[float] = None) -> int:
        """
        Label pending shadows old enough to judge. fetch_bars(since_ts) must
        return a DataFrame-like with high/low columns of M1 bars from that
        time onward (or None if history is unavailable right now).
        Conservative simulation: if SL and TP fall in the same bar, the
        LOSS wins. Returns the number of shadows learned from.
        """
        now = now or time.time()
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM shadow_trades WHERE resolved=0 "
                "AND timestamp <= ? ORDER BY timestamp LIMIT ?;",
                (now - MIN_AGE_S, RESOLVE_BATCH)).fetchall()
        if not rows:
            return 0

        learned = 0
        for row in rows:
            bars = fetch_bars(row["timestamp"])
            if bars is None or len(bars) == 0:
                continue     # feed unavailable — retry next round
            outcome = self._simulate(row, bars, now)
            if outcome is None:
                continue     # still in flight — leave pending
            with self._conn() as conn:
                conn.execute("UPDATE shadow_trades SET resolved=1, outcome=? "
                             "WHERE id=?;", (outcome, row["id"]))
                conn.commit()
            if outcome in ("win", "loss"):
                try:
                    self.brain.update(json.loads(row["brain_features"]),
                                      won=(outcome == "win"),
                                      weight=SHADOW_WEIGHT)
                    learned += 1
                except Exception as e:
                    log.warning(f"[SHADOW] Brain update failed: {e}")
        if learned:
            log.info(f"[SHADOW] Hindsight labeled {learned} skipped "
                     f"signal(s) — the brain learned from roads not taken")
        return learned

    @staticmethod
    def _simulate(row, bars, now: float) -> Optional[str]:
        """Walk M1 bars forward from the shadow's timestamp."""
        direction = int(row["direction"])
        sl, tp    = float(row["sl_price"]), float(row["tp_price"])
        n = 0
        for i in range(len(bars)):
            hi = float(bars["high"].iloc[i])
            lo = float(bars["low"].iloc[i])
            n += 1
            if direction == 1:
                if lo <= sl:
                    return "loss"
                if hi >= tp:
                    return "win"
            else:
                if hi >= sl:
                    return "loss"
                if lo <= tp:
                    return "win"
            if n >= TIMEOUT_BARS:
                return "timeout"    # ambiguous — never fed to the brain
        # ran out of bars: timeout only if enough wall-clock has passed
        if now - float(row["timestamp"]) >= TIMEOUT_BARS * 60:
            return "timeout"
        return None

    # ── Introspection ──────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Was skipping wise? Win rate of the roads not taken, per reason."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT skip_reason, outcome, COUNT(*) AS n FROM shadow_trades "
                "WHERE resolved=1 GROUP BY skip_reason, outcome;").fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["skip_reason"], {})[r["outcome"] or "?"] = r["n"]
        return out
