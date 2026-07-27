"""
Gyna — directional_calibration.py
A read-only mirror: is any signal pointing the WRONG way?

For each market state (regime x session x style), it compares how BUYs have
performed vs SELLs — pooling both real taken trades (closed_trades_ledger)
and hindsight-labeled skipped signals (shadow_trades) for a fuller sample.

If, in the same state, one direction reliably wins and the other reliably
loses (by more than EDGE_GAP, with at least MIN_SAMPLES each side), that
state is FLAGGED: the signal logic for the losing direction is probably
inverted there. This never flips a trade live — it is a diagnostic that
tells us to fix the signal at the source, and it is surfaced to the weekly
self-reflection so Gyna is aware of it.

Rationale (JP's insight): "if it buys and fails, maybe next time it should
sell." True ONLY when a state shows persistent, large, opposite-direction
edge across many samples — not after a single loss (noise), and not when
the loss is just spread on a coin flip. This module finds exactly the rare
case where reversing is justified, without ever trading against ourselves
on a hunch.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from learning_brain import FEATURE_NAMES, REGIMES, SESSIONS, STYLES

MIN_SAMPLES = 8      # per direction, per state, before a flag is trustworthy
EDGE_GAP    = 0.25   # win-rate gap between directions to call it an inversion


def _decode_shadow_state(features_json: str) -> Dict[str, Any]:
    """Recover regime/session/style/direction from a stored feature vector."""
    try:
        vec = json.loads(features_json)
        f = dict(zip(FEATURE_NAMES, vec))
    except Exception:
        return {}
    regime  = next((r for r in REGIMES  if f.get(f"regime_{r}")  == 1.0), None)
    session = next((s for s in SESSIONS if f.get(f"session_{s}") == 1.0), None)
    style   = next((s for s in STYLES   if f.get(f"style_{s}")   == 1.0), None)
    return {"regime": regime, "session": session, "style": style,
            "direction": int(f.get("direction", 0))}


def _collect(state_db_path: str) -> Dict[tuple, Dict[int, List[int]]]:
    """
    Build {(regime, session, style): {direction: [1/0 win flags]}}
    from both taken trades and resolved shadow trades.
    """
    buckets: Dict[tuple, Dict[int, List[int]]] = {}

    def add(regime, session, style, direction, is_win):
        if direction not in (1, -1) or not regime:
            return
        key = (regime, session, style)
        buckets.setdefault(key, {}).setdefault(direction, []).append(int(is_win))

    conn = sqlite3.connect(state_db_path)
    conn.row_factory = sqlite3.Row

    # Real taken trades
    try:
        for r in conn.execute(
                "SELECT regime, session, style, direction, realized_pnl_points "
                "FROM closed_trades_ledger WHERE direction IS NOT NULL"):
            add(r["regime"], r["session"], r["style"],
                int(r["direction"]), (r["realized_pnl_points"] or 0) > 0)
    except sqlite3.OperationalError:
        pass

    # Hindsight-labeled skipped signals
    try:
        for r in conn.execute(
                "SELECT brain_features, outcome FROM shadow_trades "
                "WHERE resolved=1 AND outcome IN ('win','loss')"):
            st = _decode_shadow_state(r["brain_features"])
            add(st.get("regime"), st.get("session"), st.get("style"),
                st.get("direction", 0), r["outcome"] == "win")
    except sqlite3.OperationalError:
        pass

    conn.close()
    return buckets


def calibration_report(state_db_path: str,
                       min_samples: int = MIN_SAMPLES,
                       edge_gap: float = EDGE_GAP) -> Dict[str, Any]:
    """
    Returns {"flags": [...], "buckets": [...]} where each flag is a state in
    which one direction reliably beats the other by >= edge_gap with enough
    sample on both sides — i.e. the losing direction's signal is likely
    inverted there.
    """
    buckets = _collect(state_db_path)
    flags: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []

    for (regime, session, style), by_dir in sorted(buckets.items()):
        buys  = by_dir.get(1, [])
        sells = by_dir.get(-1, [])
        row = {
            "regime": regime, "session": session, "style": style,
            "buy_n": len(buys), "buy_wr": round(sum(buys)/len(buys), 3) if buys else None,
            "sell_n": len(sells), "sell_wr": round(sum(sells)/len(sells), 3) if sells else None,
        }
        summary.append(row)
        if len(buys) >= min_samples and len(sells) >= min_samples:
            gap = row["buy_wr"] - row["sell_wr"]
            if abs(gap) >= edge_gap:
                # gap = buy_wr - sell_wr; positive => BUY is the better side
                better, worse = ("BUY", "SELL") if gap > 0 else ("SELL", "BUY")
                flags.append({
                    **row,
                    "winning_direction": better,
                    "losing_direction":  worse,
                    "gap": round(abs(gap), 3),
                    "note": (f"In {regime}/{session}/{style}, {better} wins "
                             f"{max(row['buy_wr'], row['sell_wr']):.0%} but {worse} wins "
                             f"{min(row['buy_wr'], row['sell_wr']):.0%} "
                             f"({row['buy_n']}B/{row['sell_n']}S) — {worse} signal "
                             f"likely inverted here."),
                })
    return {"flags": flags, "buckets": summary}


def reflection_note(state_db_path: str) -> str:
    """One compact block for the weekly reflection prompt (empty if nothing)."""
    rep = calibration_report(state_db_path)
    if not rep["flags"]:
        return ""
    lines = ["DIRECTIONAL CALIBRATION — states where one side is likely inverted:"]
    for f in rep["flags"][:6]:
        lines.append("- " + f["note"])
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "memory/system_state.db"
    rep = calibration_report(db)
    print(f"States analyzed: {len(rep['buckets'])} | flags: {len(rep['flags'])}\n")
    for f in rep["flags"]:
        print(" ", f["note"])
    if not rep["flags"]:
        print("  No directional inversions detected (need >= "
              f"{MIN_SAMPLES}/side and >= {EDGE_GAP:.0%} gap).")
