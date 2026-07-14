"""
Gyna — edge_engine.py
Deterministic pre-filter layer. Runs BOTH trading styles against the
snapshot, arbitrates between them using each style's LEARNED live
expectancy, and outputs the directional mask Claude cannot override.

Architecture contract:
  Features = facts → styles = candidate signals → EdgeEngine = arbiter
  → Claude = allocation only → RiskEngine = law

Styles (see trading_styles.py):
  scalper — raw price action only, tight stops, quick targets, any regime
  runner  — confirmed TREND rider, wide stops, 3R+ targets, liquid sessions

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations
from typing import Any, Dict, Optional

from trading_styles import evaluate_scalper, evaluate_runner, style_weight


class EdgeEngine:
    """
    Deterministic edge evaluation. No randomness, no API calls.
    Style signals + learned weighting + global quality modifiers + vetoes.
    """

    def __init__(self, fatigue_threshold_bars: int = 48):
        self.fatigue_threshold = fatigue_threshold_bars

    def process_state(self,
                      feature_snapshot: Dict[str, Any],
                      performance_memory: Dict[str, Any],
                      style_stats: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
        """
        Returns enriched snapshot with:
          permitted_direction:   1=BUY, -1=SELL, 0=FLAT (Claude cannot change)
          edge_quality_score:    0.0-1.0 (caps Claude's aggression_multiplier)
          style:                 which style produced the signal
          allowed_sl/tp_atr_range: overridden with the STYLE's envelope
          regime_fatigue_factor, trade_memory: context for Claude
        """
        snap = feature_snapshot.copy()
        session     = snap.get("session", "ASIA")
        vol_z       = float(snap.get("volume_z", 0.0))
        atr_pct     = float(snap.get("atr_percentile", 50.0)) / 100.0
        regime_dur  = int(snap.get("regime_duration", 1))

        # ── Candidate signals from both styles ─────────────────────────────
        candidates = [c for c in (evaluate_scalper(snap), evaluate_runner(snap))
                      if c is not None]

        # ── Learned arbitration: quality × live-expectancy weight ──────────
        chosen = None
        if candidates:
            for c in candidates:
                c["weighted"] = c["quality"] * style_weight(style_stats, c["style"])
            chosen = max(candidates, key=lambda c: c["weighted"])

        permitted_direction = chosen["direction"] if chosen else 0

        # ── Regime fatigue ─────────────────────────────────────────────────
        if regime_dur > self.fatigue_threshold:
            fatigue = min(1.0, (regime_dur - self.fatigue_threshold) /
                          self.fatigue_threshold)
        else:
            fatigue = 0.0

        # ── Quality: style base + orthogonal global modifiers ──────────────
        score = 0.0
        if chosen:
            score = chosen["quality"]
            # Session liquidity
            if session in ("LONDON", "NY_OVERLAP", "NY"):
                score += 0.10
            elif session == "ASIA":
                score += 0.02
            # Volume expansion
            if vol_z > 1.5:
                score += 0.10
            elif vol_z > 0.5:
                score += 0.05
            # ATR sweet spot (not dead, not chaos)
            if 0.25 <= atr_pct <= 0.75:
                score += 0.05
            # Fatigue penalty
            score = max(0.0, score * (1.0 - fatigue * 0.5))
        score = round(min(1.0, score), 3)

        # ── Performance memory ─────────────────────────────────────────────
        trade_memory = {
            "current_drawdown_pct":          round(float(performance_memory.get(
                                                "current_drawdown_pct", 0.0)), 4),
            "consecutive_losses":            int(performance_memory.get(
                                                "consecutive_losses", 0)),
            "win_rate_calibrated":           round(float(performance_memory.get(
                                                "win_rate_calibrated", 0.50)), 3),
            "execution_quality_degradation": round(float(performance_memory.get(
                                                "eqd_coefficient", 0.0)), 3),
        }

        # ── Global veto: 3 consecutive losses → FLAT no matter what ────────
        if trade_memory["consecutive_losses"] >= 3:
            permitted_direction = 0
            score = 0.0
            chosen = None

        snap["permitted_direction"]   = permitted_direction
        snap["edge_quality_score"]    = score
        snap["regime_fatigue_factor"] = round(fatigue, 3)
        snap["trade_memory"]          = trade_memory
        snap["style"]                 = chosen["style"] if chosen else None
        snap["style_setup"]           = chosen["setup"] if chosen else None
        if chosen:
            # The style's risk envelope replaces the regime default —
            # Claude's SL/TP targets are validated against THIS range.
            snap["allowed_sl_atr_range"] = chosen["sl_range"]
            snap["allowed_tp_atr_range"] = chosen["tp_range"]
        return snap
