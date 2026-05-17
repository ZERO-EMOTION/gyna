"""
Gyna — edge_engine.py
Deterministic pre-filter layer. Computes permitted_direction and edge_quality_score
BEFORE Claude sees anything. Claude cannot reverse or override the directional mask.

Architecture contract:
  Features = facts → EdgeEngine = directional gate → Claude = allocation only
  RiskEngine = law

Signal logic:
  - HMA + HHLL consensus for trend direction
  - BB mean-reversion for range entries
  - Regime fatigue factor (long regimes lose edge)
  - Performance memory injection (consecutive losses, EQD)

Output feeds directly into claude_allocator.py as masked_snapshot.

AURELIA EMPIRE | ZEROEMOTIONS | CLAUDE inside™
"""
from __future__ import annotations
from typing import Any, Dict


class EdgeEngine:
    """
    Deterministic edge evaluation. No randomness, no ML, no API calls.
    Pure rule-based directional mask + orthogonal quality scoring.
    """

    def __init__(self, fatigue_threshold_bars: int = 48):
        self.fatigue_threshold = fatigue_threshold_bars

    def process_state(self,
                      feature_snapshot: Dict[str, Any],
                      performance_memory: Dict[str, Any]) -> Dict[str, Any]:
        """
        Applies directional mask and quality scoring to feature snapshot.

        Returns enriched snapshot with:
          permitted_direction:   1=BUY, -1=SELL, 0=FLAT (Claude cannot change this)
          edge_quality_score:    0.0-1.0 (caps Claude's aggression_multiplier)
          regime_fatigue_factor: 0.0-1.0 (reduces aggression in tired regimes)
          trade_memory:          performance context for Claude's reasoning
        """
        snap = feature_snapshot.copy()
        regime         = snap.get("regime", "RANGE")
        hma_trend      = snap.get("hma_trend", "NEUTRAL")
        hhll_bias      = snap.get("hhll_bias", "NEUTRAL")
        struct_str     = float(snap.get("structure_strength", 0.0))
        bb_pos         = float(snap.get("bb_position", 0.5))
        bb_width_z     = float(snap.get("bb_width_z", 0.0))
        atr_pct        = float(snap.get("atr_percentile", 50.0)) / 100.0
        session        = snap.get("session", "ASIA")
        vol_z          = float(snap.get("volume_z", 0.0))
        regime_dur     = int(snap.get("regime_duration", 1))
        macd_hist_z    = float(snap.get("macd_hist_z", 0.0))
        rsi_dist_50    = float(snap.get("rsi_dist_50", 0.0))

        # ── Directional mask ───────────────────────────────────────────────
        permitted_direction = 0

        if regime in ("TREND", "VOLATILE"):
            # Trend gate: HMA + HHLL must agree, structure must be clear
            if (hma_trend == "BULLISH" and hhll_bias == "BULLISH"
                    and struct_str >= 0.5):
                permitted_direction = 1
            elif (hma_trend == "BEARISH" and hhll_bias == "BEARISH"
                    and struct_str >= 0.5):
                permitted_direction = -1

        elif regime == "RANGE":
            # Mean-reversion gate: BB squeeze + price at extremes
            if bb_width_z < -0.5:              # contracted bands (squeeze)
                if bb_pos <= 0.15:             # near lower band → long
                    permitted_direction = 1
                elif bb_pos >= 0.85:           # near upper band → short
                    permitted_direction = -1
            # Soft trend confirmation inside range (MACD + RSI agreement)
            elif macd_hist_z > 0.5 and rsi_dist_50 > 10:
                if hma_trend == "BULLISH":
                    permitted_direction = 1
            elif macd_hist_z < -0.5 and rsi_dist_50 < -10:
                if hma_trend == "BEARISH":
                    permitted_direction = -1

        # VOLATILE with no trend agreement → FLAT (too dangerous)
        if regime == "VOLATILE" and permitted_direction == 0:
            pass  # stays 0

        # ── Regime fatigue ─────────────────────────────────────────────────
        if regime_dur > self.fatigue_threshold:
            fatigue = min(1.0, (regime_dur - self.fatigue_threshold) /
                          self.fatigue_threshold)
        else:
            fatigue = 0.0

        # ── Orthogonal quality score (caps Claude's aggression) ────────────
        score = 0.0

        # Session liquidity (LONDON/NY_OVERLAP/NY = high quality)
        if session in ("LONDON", "NY_OVERLAP", "NY"):
            score += 0.20
        elif session == "ASIA":
            score += 0.10   # BTCUSD trades Asia but lower quality

        # Volume expansion
        if vol_z > 1.5:
            score += 0.20
        elif vol_z > 0.5:
            score += 0.10

        # ATR in sweet spot (not too low = fake breakout, not too high = chaos)
        if 0.25 <= atr_pct <= 0.75:
            score += 0.15

        # Structure strength
        score += struct_str * 0.20

        # Momentum alignment (MACD z + RSI direction match permitted_direction)
        if permitted_direction == 1 and macd_hist_z > 0 and rsi_dist_50 > 0:
            score += 0.15
        elif permitted_direction == -1 and macd_hist_z < 0 and rsi_dist_50 < 0:
            score += 0.15
        elif permitted_direction == 0:
            score = 0.0

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

        # If consecutive losses ≥ 3 → force FLAT regardless of signal
        if trade_memory["consecutive_losses"] >= 3:
            permitted_direction = 0
            score = 0.0

        snap["permitted_direction"]   = permitted_direction
        snap["edge_quality_score"]    = score
        snap["regime_fatigue_factor"] = round(fatigue, 3)
        snap["trade_memory"]          = trade_memory
        return snap
