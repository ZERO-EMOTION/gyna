"""
Gyna — feature_engine.py
Canonical market state generator for BTCUSD M15.

Produces a single normalized FeatureSnapshot dict consumed by:
  - claude_brain.py  (contextual interpretation)
  - risk_engine.py   (execution normalization + hard safety)
  - memory/vector_store.py (embedding + similarity search)

Architecture contract:
  Features = FACTS. Claude = interpretation. RiskEngine = law.

Rules (non-negotiable):
  1. Every feature is causal — computed from bar[0:t] only
  2. No repainting pivots, no centered indicators, no lookahead
  3. All features normalized (percentile / z-score / ratio)
     so Claude receives consistent scales
  4. feature_version stamped on every snapshot for replay/audit
  5. Minimum 200 bars for stable output

AURELIA EMPIRE | ZEROEMOTIONS | CLAUDE inside™
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from regime_engine import classify_regime, RegimeResult

# ── Version ────────────────────────────────────────────────────────────────
FEATURE_VERSION = "v1.0"

# ── Lookback periods ───────────────────────────────────────────────────────
RSI_PERIOD      = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
ATR_PERIOD      = 14
BB_PERIOD       = 20
HMA_PERIOD      = 21        # Hull MA
SLOPE_BARS      = 5         # bars used for slope calculations
ATR_PCT_WINDOW  = 100       # ATR percentile lookback
VOL_Z_WINDOW    = 50        # volume z-score window
HHLL_LOOKBACK   = 30        # bars for HH/HL/LH/LL structure
REGIME_DUR_MAX  = 200       # cap regime duration counter
TRANSITION_WIN  = 50        # bars to measure transition frequency
MIN_BARS        = 200       # minimum bars required

# ── Session boundaries (UTC hours) ────────────────────────────────────────
SESSIONS = {
    "london":  (7,  12),
    "overlap": (12, 17),
    "ny":      (17, 22),
    "asia":    (22, 7),     # wraps midnight
}


# ── Output dataclass ───────────────────────────────────────────────────────
@dataclass
class FeatureSnapshot:
    # Meta
    feature_version:        str
    timestamp:              str
    symbol:                 str
    tf:                     str

    # Price
    price:                  float

    # Regime
    regime:                 str    # trend | range | volatile
    regime_confidence:      float
    regime_duration:        int    # bars since last regime change
    transition_frequency:   float  # regime changes per bar (last 50 bars)

    # Momentum
    rsi_14:                 float  # 0-100
    rsi_slope:              float  # change over last SLOPE_BARS bars
    rsi_distance_50:        float  # signed distance from 50 (normalized -1 to +1)

    # MACD
    macd_hist:              float  # raw histogram value
    macd_hist_slope:        float  # change over last SLOPE_BARS bars
    macd_hist_z:            float  # z-scored histogram (50-bar window)

    # Volatility
    atr_14:                 float  # absolute ATR in price units
    atr_percentile:         int    # 0-100, where current ATR sits historically
    atr_slope:              float  # ATR change direction (positive = expanding)

    # Bollinger Bands
    bb_width_z:             float  # z-scored BB width (from regime_engine)
    bb_position:            float  # 0.0 = lower band, 1.0 = upper band
    bb_distance_mid:        float  # signed distance from mid (normalized by ATR)

    # HMA trend
    hma_trend:              str    # bullish | bearish | flat
    hma_slope:              float  # normalized slope (price-delta / ATR)

    # Market structure (HHLL)
    hhll_bias:              str    # bullish | bearish | neutral
    structure_strength:     float  # 0.0-1.0

    # Session
    session:                str    # london | overlap | ny | asia

    # Volume
    volume_z:               float  # z-scored volume vs 50-bar mean

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def prompt_block(self) -> str:
        """Compact representation for Claude's prompt."""
        return (
            f"MARKET STATE [{self.timestamp}] {self.symbol} {self.tf}\n"
            f"Price: {self.price:.2f} | Session: {self.session.upper()}\n"
            f"Regime: {self.regime.upper()} (conf={self.regime_confidence:.0%}, "
            f"duration={self.regime_duration}bars, transitions={self.transition_frequency:.2f}/bar)\n"
            f"RSI({RSI_PERIOD}): {self.rsi_14:.1f} | slope={self.rsi_slope:+.2f} | "
            f"dist50={self.rsi_distance_50:+.2f}\n"
            f"MACD hist: {self.macd_hist:.2f} | slope={self.macd_hist_slope:+.2f} | "
            f"z={self.macd_hist_z:+.2f}\n"
            f"ATR({ATR_PERIOD}): {self.atr_14:.2f} | pct={self.atr_percentile}th | "
            f"slope={self.atr_slope:+.3f}\n"
            f"BB: width_z={self.bb_width_z:+.2f} | pos={self.bb_position:.2f} | "
            f"dist_mid={self.bb_distance_mid:+.2f}ATR\n"
            f"HMA: {self.hma_trend.upper()} | slope={self.hma_slope:+.3f}\n"
            f"Structure: {self.hhll_bias.upper()} | strength={self.structure_strength:.2f}\n"
            f"Volume z-score: {self.volume_z:+.2f}\n"
            f"[feature_version={self.feature_version}]"
        )


# ── Helper functions ───────────────────────────────────────────────────────

def _safe_slope(series: np.ndarray, n: int = SLOPE_BARS) -> float:
    """Linear slope of last n values, normalized to avoid scale issues."""
    if len(series) < n + 1:
        return 0.0
    y = series[-n:]
    valid = y[~np.isnan(y)]
    if len(valid) < 2:
        return 0.0
    x = np.arange(len(valid), dtype=float)
    slope = np.polyfit(x, valid, 1)[0]
    return float(np.clip(slope, -1e6, 1e6))


def _percentile_rank(series: np.ndarray, value: float) -> int:
    """Where does value sit in series? Returns 0-100."""
    valid = series[~np.isnan(series)]
    if len(valid) == 0:
        return 50
    return int(np.searchsorted(np.sort(valid), value) / len(valid) * 100)


def _hma(close: pd.Series, period: int = HMA_PERIOD) -> pd.Series:
    """
    Hull Moving Average — causal, zero-lag trend filter.
    HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))
    """
    half = max(1, period // 2)
    sqrt_p = max(1, int(np.sqrt(period)))

    wma_half = close.rolling(half).apply(
        lambda x: np.dot(x, np.arange(1, len(x) + 1)) / np.arange(1, len(x) + 1).sum(),
        raw=True)
    wma_full = close.rolling(period).apply(
        lambda x: np.dot(x, np.arange(1, len(x) + 1)) / np.arange(1, len(x) + 1).sum(),
        raw=True)
    raw_hma = 2 * wma_half - wma_full
    hma = raw_hma.rolling(sqrt_p).apply(
        lambda x: np.dot(x, np.arange(1, len(x) + 1)) / np.arange(1, len(x) + 1).sum(),
        raw=True)
    return hma


def _hhll_structure(high: np.ndarray, low: np.ndarray,
                    lookback: int = HHLL_LOOKBACK) -> tuple[str, float]:
    """
    Causal HHLL market structure classifier.
    Scans last `lookback` bars for pivot highs/lows (non-repainting).
    Returns (bias, strength) where bias ∈ {bullish, bearish, neutral}.
    """
    if len(high) < lookback + 2:
        return "neutral", 0.0

    h = high[-lookback:]
    l = low[-lookback:]
    n = len(h)

    # Pivot high: bar[i] > bar[i-1] and bar[i] > bar[i+1]
    # Only look at i up to n-2 to avoid repainting last bar
    pivot_highs = []
    pivot_lows  = []
    for i in range(1, n - 1):
        if h[i] > h[i - 1] and h[i] > h[i + 1]:
            pivot_highs.append((i, h[i]))
        if l[i] < l[i - 1] and l[i] < l[i + 1]:
            pivot_lows.append((i, l[i]))

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "neutral", 0.0

    # Last two pivots only
    ph_last, ph_prev = pivot_highs[-1][1], pivot_highs[-2][1]
    pl_last, pl_prev = pivot_lows[-1][1],  pivot_lows[-2][1]

    hh = ph_last > ph_prev   # higher high
    hl = pl_last > pl_prev   # higher low
    lh = ph_last < ph_prev   # lower high
    ll = pl_last < pl_prev   # lower low

    if hh and hl:
        bias = "bullish"
        # Strength = how decisive the swings are relative to range
        rng = max(h) - min(l)
        strength = min(1.0, ((ph_last - ph_prev) + (pl_last - pl_prev)) / (rng + 1e-9))
    elif lh and ll:
        bias = "bearish"
        rng = max(h) - min(l)
        strength = min(1.0, ((ph_prev - ph_last) + (pl_prev - pl_last)) / (rng + 1e-9))
    elif hh and ll:
        bias, strength = "neutral", 0.2   # expanding range
    elif lh and hl:
        bias, strength = "neutral", 0.4   # contracting range (potential breakout)
    else:
        bias, strength = "neutral", 0.0

    return bias, float(np.clip(strength, 0.0, 1.0))


def _get_session(dt: datetime) -> str:
    h = dt.hour
    if 7 <= h < 12:  return "london"
    if 12 <= h < 17: return "overlap"
    if 17 <= h < 22: return "ny"
    return "asia"


def _regime_history(df: pd.DataFrame,
                    window: int = TRANSITION_WIN) -> tuple[int, float]:
    """
    Computes regime_duration and transition_frequency from last `window` bars.
    Returns (duration_bars, transitions_per_bar).
    Runs classify_regime on rolling sub-windows — expensive, use sparingly.
    Only called once per cycle on the full df.
    """
    if len(df) < window + 40:
        return 1, 0.0

    recent = df.iloc[-(window + 40):]
    regimes = []
    for i in range(40, len(recent)):
        r = classify_regime(recent.iloc[:i + 1]).regime
        regimes.append(r)

    if not regimes:
        return 1, 0.0

    # Duration: bars since last change
    current = regimes[-1]
    duration = 1
    for r in reversed(regimes[:-1]):
        if r == current:
            duration += 1
        else:
            break
    duration = min(duration, REGIME_DUR_MAX)

    # Transition frequency
    transitions = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i - 1])
    freq = transitions / len(regimes)

    return duration, round(freq, 3)


# ── Main function ──────────────────────────────────────────────────────────

def compute(df: pd.DataFrame,
            symbol: str = "BTCUSD",
            tf: str = "M15") -> FeatureSnapshot:
    """
    Compute full FeatureSnapshot from OHLCV DataFrame.

    df columns required: open, high, low, close, volume (lowercase)
    df index: DatetimeIndex (UTC preferred) or integer.
    Minimum MIN_BARS (200) bars for stable output.

    All computations causal — only bar[0:t] used.
    """
    if len(df) < 50:
        raise ValueError(f"Need at least 50 bars, got {len(df)}")

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"] if "volume" in df.columns else pd.Series(
        np.ones(len(df)), index=df.index)

    n = len(df)

    # ── Timestamp ──────────────────────────────────────────────────────────
    if isinstance(df.index, pd.DatetimeIndex):
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        ts_str  = last_ts.isoformat()
        session = _get_session(last_ts)
    else:
        ts_str  = datetime.now(timezone.utc).isoformat()
        session = _get_session(datetime.now(timezone.utc))

    price = float(close.iloc[-1])

    # ── Regime ─────────────────────────────────────────────────────────────
    regime_result: RegimeResult = classify_regime(df)
    regime_dur, transition_freq = _regime_history(df)

    # ── RSI ────────────────────────────────────────────────────────────────
    rsi_s  = RSIIndicator(close, window=RSI_PERIOD).rsi()
    rsi_v  = float(rsi_s.iloc[-1]) if not rsi_s.dropna().empty else 50.0
    rsi_sl = _safe_slope(rsi_s.values)
    rsi_d50 = float(np.clip((rsi_v - 50.0) / 50.0, -1.0, 1.0))

    # ── MACD ───────────────────────────────────────────────────────────────
    macd_ind  = MACD(close, window_fast=MACD_FAST, window_slow=MACD_SLOW,
                     window_sign=MACD_SIGNAL)
    macd_h    = macd_ind.macd_diff()
    macd_hv   = float(macd_h.iloc[-1]) if not macd_h.dropna().empty else 0.0
    macd_sl   = _safe_slope(macd_h.values)
    # Z-score MACD hist (50-bar rolling)
    macd_roll = macd_h.rolling(50)
    macd_mu   = float(macd_roll.mean().iloc[-1]) if not macd_roll.mean().dropna().empty else 0.0
    macd_sig  = float(macd_roll.std().iloc[-1])  if not macd_roll.std().dropna().empty  else 1.0
    macd_z    = float((macd_hv - macd_mu) / macd_sig) if macd_sig > 0 else 0.0
    macd_z    = float(np.clip(macd_z, -4.0, 4.0))

    # ── ATR ────────────────────────────────────────────────────────────────
    atr_s    = AverageTrueRange(high, low, close, window=ATR_PERIOD).average_true_range()
    atr_v    = float(atr_s.iloc[-1]) if not atr_s.dropna().empty else price * 0.01
    atr_pct  = _percentile_rank(atr_s.values[-ATR_PCT_WINDOW:], atr_v)
    atr_sl   = float(np.clip(_safe_slope(atr_s.values) / (atr_v + 1e-9), -1.0, 1.0))

    # ── Bollinger Bands ────────────────────────────────────────────────────
    bb_ind   = BollingerBands(close, window=BB_PERIOD, window_dev=2)
    bb_up    = bb_ind.bollinger_hband()
    bb_lo    = bb_ind.bollinger_lband()
    bb_mid   = bb_ind.bollinger_mavg()
    bb_up_v  = float(bb_up.iloc[-1])  if not bb_up.dropna().empty  else price * 1.02
    bb_lo_v  = float(bb_lo.iloc[-1])  if not bb_lo.dropna().empty  else price * 0.98
    bb_mid_v = float(bb_mid.iloc[-1]) if not bb_mid.dropna().empty else price
    bb_rng   = bb_up_v - bb_lo_v
    bb_pos   = float(np.clip((price - bb_lo_v) / (bb_rng + 1e-9), 0.0, 1.0))
    bb_dist_mid = float((price - bb_mid_v) / (atr_v + 1e-9))   # in ATR units

    # BB width z-score from regime_engine (already computed)
    bb_width_z = regime_result.bb_width_z

    # ── HMA ────────────────────────────────────────────────────────────────
    hma_s  = _hma(close, HMA_PERIOD)
    hma_v  = float(hma_s.iloc[-1])  if not hma_s.dropna().empty else price
    hma_pv = float(hma_s.iloc[-2])  if len(hma_s.dropna()) >= 2 else hma_v
    hma_sl = float(np.clip((hma_v - hma_pv) / (atr_v + 1e-9), -3.0, 3.0))
    if hma_sl > 0.05:
        hma_trend = "bullish"
    elif hma_sl < -0.05:
        hma_trend = "bearish"
    else:
        hma_trend = "flat"

    # ── HHLL Structure ─────────────────────────────────────────────────────
    hhll_bias, struct_strength = _hhll_structure(high.values, low.values, HHLL_LOOKBACK)

    # ── Volume z-score ─────────────────────────────────────────────────────
    vol_arr  = volume.values.astype(float)
    vol_win  = vol_arr[-VOL_Z_WINDOW:] if len(vol_arr) >= VOL_Z_WINDOW else vol_arr
    vol_mu   = vol_win.mean()
    vol_sig  = vol_win.std(ddof=1)
    vol_z    = float((vol_arr[-1] - vol_mu) / (vol_sig + 1e-9))
    vol_z    = float(np.clip(vol_z, -4.0, 4.0))

    return FeatureSnapshot(
        feature_version      = FEATURE_VERSION,
        timestamp            = ts_str,
        symbol               = symbol,
        tf                   = tf,
        price                = price,
        regime               = regime_result.regime,
        regime_confidence    = regime_result.confidence,
        regime_duration      = regime_dur,
        transition_frequency = transition_freq,
        rsi_14               = round(rsi_v, 2),
        rsi_slope            = round(rsi_sl, 3),
        rsi_distance_50      = round(rsi_d50, 3),
        macd_hist            = round(macd_hv, 4),
        macd_hist_slope      = round(macd_sl, 4),
        macd_hist_z          = round(macd_z, 3),
        atr_14               = round(atr_v, 2),
        atr_percentile       = atr_pct,
        atr_slope            = round(atr_sl, 4),
        bb_width_z           = round(bb_width_z, 3),
        bb_position          = round(bb_pos, 3),
        bb_distance_mid      = round(bb_dist_mid, 3),
        hma_trend            = hma_trend,
        hma_slope            = round(hma_sl, 4),
        hhll_bias            = hhll_bias,
        structure_strength   = round(struct_strength, 3),
        session              = session,
        volume_z             = round(vol_z, 3),
    )
