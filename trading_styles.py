"""
Gyna — trading_styles.py
Two complementary trading styles. Each evaluates the FeatureSnapshot and
returns a StyleSignal (or None). The EdgeEngine arbitrates between them,
weighted by each style's LEARNED live expectancy.

STYLE 1 — SCALPER (zero lagging indicators):
  Inputs are raw price action only: consecutive directional closes, range
  expansion (burst), where the last bar closed in its range, and liquidity
  sweep-reclaims of recent extremes. No moving averages, no oscillators,
  no bands. Tight stops, quick ~1.5R targets, trades any regime.

STYLE 2 — TREND RUNNER (lag is acceptable — it rides, it doesn't react):
  Only fires in a confirmed TREND regime during liquid sessions
  (LONDON / NY_OVERLAP / NY), requires Hull MA + market structure agreement
  with momentum confirmation. Wide stops, asymmetric 3R+ targets, few
  trades. Modeled on the fleet's validated runner profile.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

SCALPER = "scalper"
RUNNER  = "runner"

# Risk envelopes per style (ATR multiples — ATR is used ONLY to size risk,
# never as an entry signal)
SCALPER_SL_RANGE = [0.5, 0.9]
SCALPER_TP_RANGE = [0.7, 1.4]     # quick ~1.5R
RUNNER_SL_RANGE  = [1.2, 2.0]
RUNNER_TP_RANGE  = [3.0, 4.5]     # let winners run

RUNNER_SESSIONS  = ("LONDON", "NY_OVERLAP", "NY")

# Scalper thresholds
BURST_MIN_CONSEC = 3       # >= 3 same-direction closes
BURST_MIN_RATIO  = 1.2     # last-3-bar range >= 1.2x normal
BURST_CLOSE_POS  = 0.70    # last close in the top/bottom 30% of its bar


def evaluate_scalper(snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Momentum-burst continuation or sweep-reclaim reversal. Price action only."""
    consec = int(snap.get("consec_dir_closes", 0))
    burst  = float(snap.get("burst_range_ratio", 0.0))
    pos    = float(snap.get("last_close_pos", 0.5))

    direction, quality, setup = 0, 0.0, ""

    # Sweep-reclaim reversal takes precedence — the cleanest PA signal
    if snap.get("swept_low"):
        direction, quality, setup = 1, 0.55, "sweep_reclaim_long"
    elif snap.get("swept_high"):
        direction, quality, setup = -1, 0.55, "sweep_reclaim_short"
    # Momentum-burst continuation
    elif consec >= BURST_MIN_CONSEC and burst >= BURST_MIN_RATIO and pos >= BURST_CLOSE_POS:
        direction = 1
        quality   = 0.45 + min(0.15, (burst - BURST_MIN_RATIO) * 0.25) \
                         + min(0.10, (consec - BURST_MIN_CONSEC) * 0.05)
        setup     = "burst_long"
    elif consec <= -BURST_MIN_CONSEC and burst >= BURST_MIN_RATIO and pos <= (1.0 - BURST_CLOSE_POS):
        direction = -1
        quality   = 0.45 + min(0.15, (burst - BURST_MIN_RATIO) * 0.25) \
                         + min(0.10, (abs(consec) - BURST_MIN_CONSEC) * 0.05)
        setup     = "burst_short"

    if direction == 0:
        return None
    return {
        "style":     SCALPER,
        "direction": direction,
        "quality":   round(min(quality, 0.80), 3),
        "sl_range":  list(SCALPER_SL_RANGE),
        "tp_range":  list(SCALPER_TP_RANGE),
        "setup":     setup,
    }


def evaluate_runner(snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Trend rider: confirmed TREND + liquid session + structure agreement."""
    if snap.get("regime") != "TREND":
        return None
    if snap.get("session") not in RUNNER_SESSIONS:
        return None

    hma        = snap.get("hma_trend", "NEUTRAL")
    hhll       = snap.get("hhll_bias", "NEUTRAL")
    struct_str = float(snap.get("structure_strength", 0.0))
    macd_z     = float(snap.get("macd_hist_z", 0.0))
    rsi_d50    = float(snap.get("rsi_dist_50", 0.0))

    if not (hma == hhll and struct_str >= 0.5):
        return None
    if hma == "BULLISH":
        direction = 1
    elif hma == "BEARISH":
        direction = -1
    else:
        return None

    quality = 0.50 + struct_str * 0.20
    # Momentum confirmation (lagging is fine here — we ride, we don't react)
    if (direction == 1 and macd_z > 0 and rsi_d50 > 0) or \
       (direction == -1 and macd_z < 0 and rsi_d50 < 0):
        quality += 0.15
    if snap.get("session") == "NY_OVERLAP":
        quality += 0.05

    return {
        "style":     RUNNER,
        "direction": direction,
        "quality":   round(min(quality, 0.90), 3),
        "sl_range":  list(RUNNER_SL_RANGE),
        "tp_range":  list(RUNNER_TP_RANGE),
        "setup":     "trend_ride",
    }


def style_weight(style_stats: Optional[Dict[str, Dict[str, Any]]],
                 style: str,
                 min_trades: int = 10) -> float:
    """
    Learned multiplier from LIVE per-style performance. Below min_trades the
    style trades at neutral weight (it must be allowed to build a record);
    after that its profit factor scales its voice between 0.6x and 1.4x.
    """
    if not style_stats or style not in style_stats:
        return 1.0
    s = style_stats[style]
    if int(s.get("total_trades", 0)) < min_trades:
        return 1.0
    pf = float(s.get("profit_factor", 1.3))
    return max(0.6, min(1.4, pf / 1.3))
