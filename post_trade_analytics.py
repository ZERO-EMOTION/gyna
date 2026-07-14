"""
Gyna — post_trade_analytics.py
Decoupled post-trade statistical validation engine.
Runs at end of session/week — completely isolated from the hot execution loop.

Answers:
  1. Which snapshot_hashes correlate to toxic slippage / high EQD?
  2. What spread percentiles destroy Profit Factor?
  3. Which regimes/sessions see edge decay?
  4. Which allocator profiles (AGGRESSIVE/CONSERVATIVE) outperform?

Reads from system_state.db (closed_trades_ledger table).
Writes nothing — pure analytical read layer.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
import pandas as pd


# ── Schema migration helper ────────────────────────────────────────────────
CLOSED_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS closed_trades_ledger (
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
);
"""


def ensure_schema(db_path: str) -> None:
    """Create closed_trades_ledger if missing — safe to run on every startup."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(CLOSED_TRADES_DDL)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(closed_trades_ledger)")}
        if "state_signature" not in cols:
            conn.execute("ALTER TABLE closed_trades_ledger "
                         "ADD COLUMN state_signature TEXT")
        conn.commit()


class PostTradeValidationEngine:
    """
    Post-session statistical audit. Run after market close or weekly.
    Never called from the hot execution path.
    """

    def __init__(self, db_path: str = "memory/system_state.db"):
        self.db_path = db_path
        ensure_schema(db_path)

    # ── Data loading ───────────────────────────────────────────────────────

    def _load_df(self, days: int = 0) -> pd.DataFrame:
        """
        Load closed trades. days=0 loads all history.
        days=7 loads last 7 days only.
        """
        with sqlite3.connect(self.db_path) as conn:
            if days > 0:
                cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
                df = pd.read_sql_query(
                    "SELECT * FROM closed_trades_ledger WHERE timestamp_closed >= ?",
                    conn, params=(cutoff,))
            else:
                df = pd.read_sql_query(
                    "SELECT * FROM closed_trades_ledger", conn)
        return df

    # ── Main report ────────────────────────────────────────────────────────

    def generate_report(self, days: int = 0) -> Dict[str, Any]:
        """
        Full multi-dimensional statistical audit.
        Returns structured dict — also see print_audit() for terminal output.
        """
        df = self._load_df(days)
        if df.empty:
            return {"status": "INSUFFICIENT_DATA",
                    "message": "closed_trades_ledger is empty."}

        df["is_win"] = df["realized_pnl_points"] > 0

        gross_profit = df.loc[df["realized_pnl_points"] > 0, "realized_pnl_points"].sum()
        gross_loss   = df.loc[df["realized_pnl_points"] < 0, "realized_pnl_points"].abs().sum()
        pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        def _pf(x: pd.Series) -> float:
            gp = x[x > 0].sum()
            gl = x[x < 0].abs().sum()
            return round(float(gp / gl), 3) if gl > 0 else float("inf")

        # ── 1. Spread friction quartiles ───────────────────────────────────
        try:
            df["spread_bin"] = pd.qcut(
                df["entry_spread_points"], q=4,
                labels=["Q1_Low", "Q2_Med", "Q3_High", "Q4_Extreme"],
                duplicates="drop")
        except ValueError:
            df["spread_bin"] = "Q1_Low"

        spread_matrix = (
            df.groupby("spread_bin", observed=False)
            .agg(
                trade_count=("realized_pnl_points", "count"),
                avg_eqd=("final_eqd", "mean"),
                avg_slippage=("avg_slippage_points", "mean"),
                win_rate=("is_win", "mean"),
            )
            .assign(profit_factor=lambda d: d.index.map(
                lambda k: _pf(df.loc[df["spread_bin"] == k, "realized_pnl_points"])))
            .round(3)
            .to_dict(orient="index")
        )

        # ── 2. Regime expectancy ───────────────────────────────────────────
        regime_matrix = (
            df.groupby("regime")
            .agg(
                trade_count=("realized_pnl_points", "count"),
                win_rate=("is_win", "mean"),
                avg_pnl=("realized_pnl_points", "mean"),
            )
            .assign(profit_factor=lambda d: d.index.map(
                lambda k: _pf(df.loc[df["regime"] == k, "realized_pnl_points"])))
            .round(3)
            .to_dict(orient="index")
        )

        # ── 3. Session latency + slippage profiles ─────────────────────────
        session_matrix = (
            df.groupby("session")
            .agg(
                trade_count=("realized_pnl_points", "count"),
                avg_latency=("avg_latency_ms", "mean"),
                avg_slippage=("avg_slippage_points", "mean"),
                net_pnl=("realized_pnl_points", "sum"),
                win_rate=("is_win", "mean"),
            )
            .round(3)
            .to_dict(orient="index")
        )

        # ── 4. Allocator profile drift ─────────────────────────────────────
        def _max_dd(x: pd.Series) -> float:
            cum = x.cumsum()
            return float((cum.cummax() - cum).max())

        allocator_matrix = (
            df.groupby("execution_profile")
            .agg(
                trade_count=("realized_pnl_points", "count"),
                avg_pnl=("realized_pnl_points", "mean"),
                win_rate=("is_win", "mean"),
            )
            .assign(
                profit_factor=lambda d: d.index.map(
                    lambda k: _pf(df.loc[df["execution_profile"] == k,
                                         "realized_pnl_points"])),
                max_drawdown_pts=lambda d: d.index.map(
                    lambda k: _max_dd(df.loc[df["execution_profile"] == k,
                                              "realized_pnl_points"]))
            )
            .round(3)
            .to_dict(orient="index")
        )

        # ── 5. Toxic state detection ──────────────────────────────────────
        # Group by the quantized state_signature (recurring across bars) —
        # snapshot_hash includes the timestamp and is unique per bar, so it
        # can never recur and must not be used for toxicity matching.
        toxic_hashes: List[str] = []
        sig_col = ("state_signature"
                   if "state_signature" in df.columns
                   and df["state_signature"].notna().any()
                   else None)
        if sig_col:
            sig_df = df[df[sig_col].notna()]
            hash_groups = sig_df.groupby(sig_col).filter(lambda x: len(x) >= 3)
            if not hash_groups.empty:
                summary = (
                    hash_groups.groupby(sig_col)
                    .agg(count=("realized_pnl_points", "count"),
                         net_pnl=("realized_pnl_points", "sum"))
                    .sort_values("net_pnl")
                )
                toxic_hashes = (
                    summary[summary["net_pnl"] < 0]
                    .head(10)
                    .index.tolist()
                )

        # ── 6. Kill hour validation (empirical) ────────────────────────────
        if "timestamp_closed" in df.columns:
            df["hour_utc"] = pd.to_datetime(
                df["timestamp_closed"], unit="s", utc=True).dt.hour
            hour_pnl = (
                df.groupby("hour_utc")["realized_pnl_points"]
                .agg(["sum", "count", "mean"])
                .round(3)
                .to_dict(orient="index")
            )
            worst_hours = sorted(
                [(h, v["sum"]) for h, v in hour_pnl.items()],
                key=lambda x: x[1])[:3]
        else:
            hour_pnl    = {}
            worst_hours = []

        return {
            "status": "PROFILED",
            "period_days": days if days > 0 else "ALL",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global_metrics": {
                "total_trades":   len(df),
                "win_rate":       round(float(df["is_win"].mean()), 3),
                "profit_factor":  round(float(pf), 3),
                "avg_pnl_pts":    round(float(df["realized_pnl_points"].mean()), 3),
                "net_pnl_pts":    round(float(df["realized_pnl_points"].sum()), 3),
                "sample_variance":round(float(df["realized_pnl_points"].var()), 4),
                "avg_eqd":        round(float(df["final_eqd"].mean()), 4),
                "avg_slippage":   round(float(df["avg_slippage_points"].mean()), 3),
            },
            "spread_friction_impact":    spread_matrix,
            "regime_expectancy":         regime_matrix,
            "session_latency_profiles":  session_matrix,
            "allocator_profile_efficiency": allocator_matrix,
            "hourly_pnl_distribution":   hour_pnl,
            "empirical_worst_hours_utc": worst_hours,
            "toxic_state_signatures":    toxic_hashes,
            "toxic_snapshot_hashes":     toxic_hashes,  # legacy alias
        }

    # ── Terminal output ────────────────────────────────────────────────────

    def print_audit(self, days: int = 0) -> None:
        """Full audit to stdout. Run at end of session."""
        report = self.generate_report(days)

        if report["status"] == "INSUFFICIENT_DATA":
            print(f"\n[-] Audit halted: {report['message']}\n")
            return

        g = report["global_metrics"]
        sep = "=" * 65

        print(f"\n{sep}")
        print("         GYNA — EXECUTION AUDIT & PERFORMANCE MATRIX")
        print(f"         Generated: {report['generated_at'][:19]} UTC")
        print(sep)
        print(f"Trades: {g['total_trades']} | WR: {g['win_rate']:.2%} | "
              f"PF: {g['profit_factor']:.2f} | Avg PnL: {g['avg_pnl_pts']:+.1f}pts")
        print(f"Net: {g['net_pnl_pts']:+.0f}pts | Avg EQD: {g['avg_eqd']:.3f} | "
              f"Avg Slip: {g['avg_slippage']:+.1f}pts")

        print(f"\n{'─'*65}")
        print("[1] Spread Friction → Expectancy")
        for k, v in report["spread_friction_impact"].items():
            print(f"  {str(k):<12} Trades:{v['trade_count']:<4} "
                  f"EQD:{v['avg_eqd']:.3f}  Slip:{v['avg_slippage']:+.1f}pts  "
                  f"WR:{v['win_rate']:.0%}  PF:{v['profit_factor']:.2f}")

        print(f"\n[2] Regime Structural Expectancy")
        for k, v in report["regime_expectancy"].items():
            print(f"  {str(k):<10} Trades:{v['trade_count']:<4} "
                  f"WR:{v['win_rate']:.0%}  Avg:{v['avg_pnl']:+.1f}pts  "
                  f"PF:{v['profit_factor']:.2f}")

        print(f"\n[3] Session Latency & Slippage")
        for k, v in report["session_latency_profiles"].items():
            print(f"  {str(k):<14} Trades:{v['trade_count']:<4} "
                  f"Lat:{v['avg_latency']:.0f}ms  Slip:{v['avg_slippage']:+.1f}pts  "
                  f"Net:{v['net_pnl']:+.0f}pts")

        print(f"\n[4] Allocator Profile Efficiency")
        for k, v in report["allocator_profile_efficiency"].items():
            print(f"  {str(k):<14} Trades:{v['trade_count']:<4} "
                  f"Avg:{v['avg_pnl']:+.1f}pts  PF:{v['profit_factor']:.2f}  "
                  f"MaxDD:{v['max_drawdown_pts']:.1f}pts")

        print(f"\n[5] Empirical Worst Hours UTC (by net PnL)")
        if report["empirical_worst_hours_utc"]:
            for hour, pnl in report["empirical_worst_hours_utc"]:
                flag = " ← KILL HOUR CANDIDATE" if abs(pnl) > 10 else ""
                print(f"  {hour:02d}:00 UTC  Net:{pnl:+.1f}pts{flag}")
        else:
            print("  Insufficient data")

        print(f"\n[6] Toxic Snapshot Hashes")
        if report["toxic_snapshot_hashes"]:
            for h in report["toxic_snapshot_hashes"]:
                print(f"  [HAZARD] {h}")
        else:
            print("  No recurring toxic state signatures found")

        print(f"{sep}\n")


# ── Empirical kill hours ───────────────────────────────────────────────────

def empirical_kill_hours(report: Dict[str, Any],
                         min_trades: int = 10,
                         max_hours: int = 4) -> List[int]:
    """
    Select UTC hours the system should stop trading, from its own history.

    Guards against learning noise:
      - an hour needs >= min_trades closed trades before it can qualify
      - only net-NEGATIVE hours qualify
      - at most max_hours are ever blocked (never kill the whole day)
    Returns hours sorted worst-first.
    """
    hours = report.get("hourly_pnl_distribution", {}) or {}
    losers = [(int(h), v) for h, v in hours.items()
              if v.get("count", 0) >= min_trades and v.get("sum", 0.0) < 0]
    losers.sort(key=lambda x: x[1]["sum"])
    return [h for h, _ in losers[:max_hours]]


# ── Adaptive exit decay (added to RiskEngine interface) ───────────────────

def compute_adaptive_stealth_decay(open_duration_bars: int,
                                   current_eqd: float) -> float:
    """
    Execution-aware stealth target compression.
    Returns a multiplier (0.50–1.0) applied to virtual SL/TP points.

    As time-in-trade extends OR execution quality degrades:
    → multiplier shrinks → stealth targets pulled inward
    → position exits sooner before structural decay compounds

    Caller (telemetry tick loop) applies:
        effective_sl = virtual_sl_points * decay
        effective_tp = virtual_tp_points * decay
    """
    time_decay   = max(0.60, 1.0 - (open_duration_bars * 0.01))
    latency_pen  = max(0.50, 1.0 - current_eqd)
    return round(float(time_decay * latency_pen), 3)


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "memory/system_state.db"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    engine = PostTradeValidationEngine(db)
    engine.print_audit(days)
