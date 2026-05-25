"""
Gyna — feature_engine.py
Canonical causal feature generation and state serialization.

Input:  OHLCV DataFrame (minimum 200 bars)
Output: Deterministic, validated, hashed JSON-serializable FeatureSnapshot

Architecture contract:
  Features = FACTS. Claude = interpretation. RiskEngine = law.

Key design decisions:
  - idx_0 = -2 (last CLOSED bar, not forming bar — no repaint)
  - allowed_sl_atr_range + allowed_tp_atr_range embedded in snapshot
    so risk envelope travels with the data to claude_brain
  - snapshot_hash (SHA-256) for audit trail + vector store integrity
  - RISK_ENVELOPES defined here, enforced by RiskEngine (final authority)

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from regime_engine import RegimeResult, classify_regime, rolling_regimes

FEATURE_VERSION = "v1.1"

# ── Risk envelopes (Claude suggests inside, RiskEngine clamps) ─────────────
# BTCUSD M1 Production Risk Envelopes
# SL: structure + ATR-relative (never fixed pip stops)
# TP: asymmetric RR 1.5R minimum
# ATR buffer: 0.50 x ATR14
# BTC noise is too aggressive for tight stops
RISK_ENVELOPES = {
    "TREND":    {"sl_min": 0.5, "sl_max": 1.0, "tp_min": 0.75, "tp_max": 2.0},  # 1.5R target
    "VOLATILE": {"sl_min": 0.8, "sl_max": 1.5, "tp_min": 1.2,  "tp_max": 3.0},  # wider — BTC bursts
    "RANGE":    {"sl_min": 0.5, "sl_max": 1.0, "tp_min": 0.75, "tp_max": 1.5},  # disabled later
}

# ── Sessions (UTC) ─────────────────────────────────────────────────────────
def _determine_session(dt: datetime) -> str:
    dt_utc = dt.astimezone(timezone.utc)
    dow    = dt_utc.weekday()
    hour   = dt_utc.hour
    if dow >= 5:                    return "ASIA"    # BTC 24/7 — weekend = Asia session
    if 0  <= hour <  7:             return "ASIA"
    if 7  <= hour < 12:             return "LONDON"
    if 12 <= hour < 17:             return "NY_OVERLAP"
    if 17 <= hour < 22:             return "NY"
    return "LATE_PACIFIC"


# ── ATR (simple rolling, avoids ta dependency for this class) ──────────────
def _compute_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"]  - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


# ── HMA (Hull Moving Average) ──────────────────────────────────────────────
def _compute_hma(close: pd.Series, period: int = 20) -> pd.Series:
    half = max(1, period // 2)
    sqrp = max(1, int(np.sqrt(period)))
    def _wma(s, p):
        w = np.arange(1, p + 1, dtype=float)
        return s.rolling(p).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
    return _wma(2 * _wma(close, half) - _wma(close, period), sqrp)


# ── Market structure (half/half HHLL — no repainting pivots) ──────────────
def _market_structure(df: pd.DataFrame,
                      lookback: int = 30) -> Tuple[str, float, int, int]:
    """
    Splits lookback window into two halves, compares highs/lows.
    Causal: uses bars [-(lookback+1) : -1] to exclude forming bar.
    Returns (bias, strength, abs_high_idx, abs_low_idx).
    """
    sub = df.iloc[-(lookback + 1):-1]  # exclude bar[-1] (forming)
    if len(sub) < lookback:
        return "NEUTRAL", 0.0, -2, -2

    highs = sub["high"].values
    lows  = sub["low"].values
    half  = lookback // 2

    hh = highs[half:].max() > highs[:half].max()
    lh = highs[half:].max() < highs[:half].max()
    hl = lows[half:].min()  > lows[:half].min()
    ll = lows[half:].min()  < lows[:half].min()

    bull = (1.0 if hh else 0.0) + (1.0 if hl else 0.0)
    bear = (1.0 if ll else 0.0) + (1.0 if lh else 0.0)
    strength = abs(bull - bear) / 2.0

    abs_hi = int(sub["high"].idxmax()) if isinstance(sub.index[0], (int, np.integer)) \
             else sub["high"].values.argmax() - (lookback + 1)
    abs_lo = int(sub["low"].idxmin())  if isinstance(sub.index[0], (int, np.integer)) \
             else sub["low"].values.argmin()  - (lookback + 1)

    if bull > bear:   return "BULLISH", strength, abs_hi, abs_lo
    if bear > bull:   return "BEARISH", strength, abs_hi, abs_lo
    return "NEUTRAL", 0.0, abs_hi, abs_lo


# ── Regime duration + transition frequency ─────────────────────────────────
def _regime_meta(df: pd.DataFrame,
                 current_regime: str,
                 window: int = 50,
                 max_dur: int = 200) -> Tuple[int, float]:
    """
    Computes how long current regime has been active (bars)
    and how often regimes switch (per bar) over last `window` bars.
    Runs classify_regime on rolling sub-windows — called once per cycle.
    """
    if len(df) < window + 40:
        return 1, 0.0

    tail   = df.iloc[-(window + 40):]
    labels = []
    for i in range(40, len(tail)):
        labels.append(classify_regime(tail.iloc[:i + 1]).regime.upper())

    if not labels:
        return 1, 0.0

    duration = 1
    for r in reversed(labels[:-1]):
        if r == current_regime:
            duration += 1
        else:
            break

    transitions = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    freq = round(transitions / len(labels), 4)

    return min(duration, max_dur), freq


# ── Integrity hash ─────────────────────────────────────────────────────────
def _integrity_hash(snapshot: Dict[str, Any]) -> str:
    clean = {k: v for k, v in snapshot.items() if k != "snapshot_hash"}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, default=str).encode()
    ).hexdigest()


# ── Main class ─────────────────────────────────────────────────────────────
class FeatureEngine:
    """
    Canonical market state generator for Gyna.

    Usage:
        engine = FeatureEngine("BTCUSD", "M1")
        snapshot = engine.generate_snapshot(df)   # dict, JSON-serializable
    """

    def __init__(self, symbol: str = "BTCUSD", timeframe: str = "M1",
                 feature_version: str = FEATURE_VERSION):
        self.symbol          = symbol
        self.timeframe       = timeframe
        self.feature_version = feature_version

    def generate_snapshot(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Generate full FeatureSnapshot from OHLCV DataFrame.

        df columns required: open, high, low, close, volume (lowercase)
        df index:            DatetimeIndex (UTC) or integer
        Minimum bars:        200

        Execution anchor: idx_0 = -2 (last CLOSED bar — no repaint/lookahead)
        """
        if len(df) < 200:
            raise ValueError(
                f"Requires >= 200 bars for stable features. Got {len(df)}."
            )

        df = df.copy().sort_index()
        idx_0 = -2   # LAST CLOSED BAR — never -1 (forming)

        current_close = float(df["close"].iloc[idx_0])
        raw_ts        = df.index[idx_0]
        current_time  = (raw_ts if isinstance(raw_ts, datetime)
                         else datetime.now(timezone.utc))
        if hasattr(current_time, "tzinfo") and current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        # ── Regime ────────────────────────────────────────────────────────
        regime_result: RegimeResult = classify_regime(df.iloc[:-1])  # exclude forming bar
        regime_name   = regime_result.regime.upper()
        regime_dur, transition_freq = _regime_meta(df, regime_name)
        envelope      = RISK_ENVELOPES.get(regime_name, RISK_ENVELOPES["RANGE"])

        # ── RSI(14) ───────────────────────────────────────────────────────
        delta       = df["close"].diff()
        gain        = delta.where(delta > 0, 0).rolling(14).mean()
        loss        = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi_s       = 100 - (100 / (1 + gain / (loss + 1e-10)))
        rsi_val     = float(rsi_s.iloc[idx_0])
        rsi_slope   = float(rsi_s.diff(3).iloc[idx_0])

        # ── MACD(12,26,9) ─────────────────────────────────────────────────
        ema12       = df["close"].ewm(span=12, adjust=False).mean()
        ema26       = df["close"].ewm(span=26, adjust=False).mean()
        macd_line   = ema12 - ema26
        macd_sig    = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist   = macd_line - macd_sig
        macd_hist_z = ((macd_hist - macd_hist.rolling(50).mean()) /
                       (macd_hist.rolling(50).std(ddof=1) + 1e-10))

        # ── ATR(14) ───────────────────────────────────────────────────────
        atr_s       = _compute_atr(df, 14)
        current_atr = float(atr_s.iloc[idx_0])
        atr_pct     = float(atr_s.rolling(200).apply(
            lambda x: np.sum(x[-1] >= x) / len(x) * 100, raw=True
        ).iloc[idx_0])
        atr_slope   = float(atr_s.diff(3).iloc[idx_0])

        # ── Bollinger Bands(20,2) ─────────────────────────────────────────
        mid         = df["close"].rolling(20).mean()
        std         = df["close"].rolling(20).std(ddof=0)
        bb_up       = mid + 2 * std
        bb_lo       = mid - 2 * std
        bb_width    = bb_up - bb_lo
        bb_width_z  = ((bb_width - bb_width.rolling(100).mean()) /
                       (bb_width.rolling(100).std(ddof=0) + 1e-10))
        bb_rng      = (bb_up - bb_lo).replace(0, np.nan)
        bb_pos      = ((df["close"] - bb_lo) / bb_rng).clip(0, 1)
        dist_mid    = (df["close"] - mid) / (atr_s + 1e-10)

        # ── HMA(20) ───────────────────────────────────────────────────────
        hma_s       = _compute_hma(df["close"], 20)
        hma_slope   = float(hma_s.diff(3).iloc[idx_0]) / current_close * 10000
        noise_thr   = 0.05 * (current_atr / current_close * 100)
        hma_trend   = ("BULLISH" if hma_slope >  noise_thr else
                       "BEARISH" if hma_slope < -noise_thr else "NEUTRAL")

        # ── Market structure ──────────────────────────────────────────────
        hhll_bias, struct_str, hi_idx, lo_idx = _market_structure(df, lookback=30)
        recent_high  = float(df["high"].iloc[hi_idx])
        recent_low   = float(df["low"].iloc[lo_idx])
        dist_hi_atr  = (recent_high - current_close) / (current_atr + 1e-10)
        dist_lo_atr  = (current_close - recent_low)  / (current_atr + 1e-10)

        # ── Volume z-score ────────────────────────────────────────────────
        vol_z = ((df["volume"] - df["volume"].rolling(50).mean()) /
                 (df["volume"].rolling(50).std(ddof=0) + 1e-10))

        # ── Assemble snapshot ─────────────────────────────────────────────
        snapshot: Dict[str, Any] = {
            # Meta
            "feature_version":    self.feature_version,
            "timestamp":          current_time.isoformat(),
            "symbol":             self.symbol,
            "tf":                 self.timeframe,
            "price":              round(current_close, 2),

            # Regime + risk envelope (travels with the snapshot)
            "regime":             regime_name,
            "regime_confidence":  round(regime_result.confidence, 2),
            "regime_duration":    regime_dur,
            "transition_frequency": transition_freq,
            "allowed_sl_atr_range": [envelope["sl_min"], envelope["sl_max"]],
            "allowed_tp_atr_range": [envelope["tp_min"], envelope["tp_max"]],

            # RSI
            "rsi_14":             round(rsi_val, 2),
            "rsi_slope":          round(rsi_slope, 3),
            "rsi_dist_50":        round(rsi_val - 50.0, 2),

            # MACD
            "macd_line":          round(float(macd_line.iloc[idx_0]), 4),
            "macd_signal":        round(float(macd_sig.iloc[idx_0]), 4),
            "macd_hist":          round(float(macd_hist.iloc[idx_0]), 4),
            "macd_hist_slope":    round(float(macd_hist.diff(3).iloc[idx_0]), 4),
            "macd_hist_z":        round(float(macd_hist_z.iloc[idx_0]), 3),

            # ATR
            "atr_14":             round(current_atr, 2),
            "atr_percentile":     round(atr_pct, 1),
            "atr_slope":          round(atr_slope, 4),

            # Bollinger
            "bb_width_z":         round(float(bb_width_z.iloc[idx_0]), 3),
            "bb_position":        round(float(bb_pos.iloc[idx_0]), 3),
            "dist_from_mid_atr":  round(float(dist_mid.iloc[idx_0]), 3),

            # HMA
            "hma_trend":          hma_trend,
            "hma_slope":          round(hma_slope, 4),

            # Structure
            "hhll_bias":                    hhll_bias,
            "structure_strength":           round(struct_str, 3),
            "distance_from_recent_high_atr": round(dist_hi_atr, 3),
            "distance_from_recent_low_atr":  round(dist_lo_atr, 3),

            # Volume + session
            "volume_z":           round(float(vol_z.iloc[idx_0]), 3),
            "session":            _determine_session(current_time),
        }

        snapshot["snapshot_hash"] = _integrity_hash(snapshot)
        return snapshot

    def prompt_block(self, snap: Dict[str, Any]) -> str:
        """Compact human-readable block for Claude's prompt."""
        return (
            f"MARKET STATE [{snap['timestamp']}] {snap['symbol']} {snap['tf']}\n"
            f"Price: {snap['price']:.2f} | Session: {snap['session']}\n"
            f"Regime: {snap['regime']} (conf={snap['regime_confidence']:.0%}, "
            f"dur={snap['regime_duration']}bars, trans={snap['transition_frequency']:.3f}/bar)\n"
            f"SL envelope: {snap['allowed_sl_atr_range']} ATR | "
            f"TP envelope: {snap['allowed_tp_atr_range']} ATR\n"
            f"RSI(14): {snap['rsi_14']:.1f} | slope={snap['rsi_slope']:+.2f} | "
            f"dist50={snap['rsi_dist_50']:+.2f}\n"
            f"MACD hist: {snap['macd_hist']:.4f} | slope={snap['macd_hist_slope']:+.4f} | "
            f"z={snap['macd_hist_z']:+.3f}\n"
            f"ATR(14): {snap['atr_14']:.2f} | pct={snap['atr_percentile']:.0f}th | "
            f"slope={snap['atr_slope']:+.4f}\n"
            f"BB: width_z={snap['bb_width_z']:+.3f} | pos={snap['bb_position']:.3f} | "
            f"dist_mid={snap['dist_from_mid_atr']:+.3f}ATR\n"
            f"HMA: {snap['hma_trend']} | slope={snap['hma_slope']:+.4f}\n"
            f"Structure: {snap['hhll_bias']} | strength={snap['structure_strength']:.3f} | "
            f"dist_hi={snap['distance_from_recent_high_atr']:+.2f}ATR | "
            f"dist_lo={snap['distance_from_recent_low_atr']:+.2f}ATR\n"
            f"Volume z: {snap['volume_z']:+.3f}\n"
            f"Hash: {snap['snapshot_hash'][:12]}... [{snap['feature_version']}]"
        )
