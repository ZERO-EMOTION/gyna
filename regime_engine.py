"""
Gyna — regime_engine.py
Classifies BTCUSD market regime: TREND | RANGE | VOLATILE

Three-method consensus (via ta library — validated, no lookahead):
  1. ADX(14)          — directional strength
  2. BB Width z-score — volatility expansion/contraction
  3. Choppiness(14)   — trending vs ranging index

All signals computed causally. Returns RegimeResult dataclass
consumed by feature_engine.py and injected into Claude's prompt.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd
from ta.trend import ADXIndicator

# ── Thresholds ─────────────────────────────────────────────────────────────
ADX_TREND_MIN       = 25.0   # ADX ≥ 25 = directional trend
ADX_RANGE_MAX       = 20.0   # ADX ≤ 20 = ranging/weak
BB_EXPAND_Z         = 0.5    # BB width z-score ≥  0.5 = expanding (volatile)
BB_CONTRACT_Z       = -0.3   # BB width z-score ≤ -0.3 = contracting (range)
CHOP_RANGE_MIN      = 61.8   # Choppiness ≥ 61.8 = ranging (golden ratio)
CHOP_TREND_MAX      = 38.2   # Choppiness ≤ 38.2 = strong trend

ADX_PERIOD          = 14
BB_PERIOD           = 20
CHOP_PERIOD         = 14
BB_ZSCORE_WINDOW    = 50     # bars to z-score BB width against


# ── Result dataclass ───────────────────────────────────────────────────────
@dataclass
class RegimeResult:
    regime:         str    # "trend" | "range" | "volatile"
    adx:            float
    adx_signal:     str    # "trend" | "range" | "neutral"
    bb_width:       float  # raw BB width
    bb_width_z:     float  # z-scored
    bb_signal:      str    # "volatile" | "range" | "neutral"
    chop:           float
    chop_signal:    str    # "trend" | "range" | "neutral"
    trend_votes:    int
    range_votes:    int
    volatile_votes: int
    confidence:     float  # 0.0-1.0

    def to_dict(self) -> dict:
        return {
            "regime":         self.regime,
            "adx":            round(self.adx, 2),
            "bb_width_z":     round(self.bb_width_z, 3),
            "chop":           round(self.chop, 2),
            "trend_votes":    self.trend_votes,
            "range_votes":    self.range_votes,
            "volatile_votes": self.volatile_votes,
            "confidence":     round(self.confidence, 3),
        }

    def __str__(self) -> str:
        return (
            f"Regime={self.regime.upper()} (conf={self.confidence:.0%}) | "
            f"ADX={self.adx:.1f}[{self.adx_signal}] "
            f"BBz={self.bb_width_z:+.2f}[{self.bb_signal}] "
            f"Chop={self.chop:.1f}[{self.chop_signal}] "
            f"Votes T={self.trend_votes} R={self.range_votes} V={self.volatile_votes}"
        )


# ── Indicator computations ─────────────────────────────────────────────────

def _adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ADX via ta library. Fully causal, returns full-length Series."""
    return ADXIndicator(df["high"], df["low"], df["close"], window=period).adx()


def _bb_width_z(close: pd.Series, period: int = 20,
                zscore_window: int = 50) -> tuple[pd.Series, pd.Series]:
    """
    Bollinger Band width = (upper - lower) / middle.
    Z-scored over a rolling zscore_window to normalise across regimes.
    Returns (bb_width_raw, bb_width_z).
    """
    mid    = close.rolling(period).mean()
    std    = close.rolling(period).std(ddof=1)
    width  = (4 * std) / mid.replace(0, np.nan)   # 2σ band = 4σ total
    width_z = (width - width.rolling(zscore_window).mean()) / \
               width.rolling(zscore_window).std(ddof=1)
    return width, width_z


def _choppiness(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Choppiness Index = 100 * log10(sum(ATR1) / (HH - LL)) / log10(period)
    100 = max chop (range). 0 = max trend.
    """
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr_sum = tr.rolling(period).sum()
    hh      = high.rolling(period).max()
    ll      = low.rolling(period).min()
    rng     = (hh - ll).replace(0, np.nan)
    chop    = 100 * np.log10(atr_sum / rng) / np.log10(period)
    return chop.clip(0, 100)


# ── Main classifier ────────────────────────────────────────────────────────

def classify_regime(df: pd.DataFrame) -> RegimeResult:
    """
    Classify current (last bar) regime from OHLC DataFrame.
    df must have columns: high, low, close (lowercase).
    Minimum ~80 bars recommended for stable ADX + BB z-score.
    """
    if len(df) < ADX_PERIOD * 2:
        return RegimeResult(
            regime="range", adx=22.0, adx_signal="neutral",
            bb_width=0.02, bb_width_z=0.0, bb_signal="neutral",
            chop=50.0, chop_signal="neutral",
            trend_votes=0, range_votes=1, volatile_votes=0, confidence=0.33,
        )

    adx_s              = _adx_series(df)
    bb_width_s, bb_z_s = _bb_width_z(df["close"])
    chop_s             = _choppiness(df)

    # Last valid bar values
    adx_val  = float(adx_s.dropna().iloc[-1])   if not adx_s.dropna().empty  else 22.0
    bb_w_val = float(bb_width_s.dropna().iloc[-1]) if not bb_width_s.dropna().empty else 0.02
    bb_z_val = float(bb_z_s.dropna().iloc[-1])  if not bb_z_s.dropna().empty  else 0.0
    chop_val = float(chop_s.dropna().iloc[-1])  if not chop_s.dropna().empty  else 50.0

    # ── Signal per method ──────────────────────────────────────────────────
    adx_signal  = ("trend"   if adx_val  >= ADX_TREND_MIN  else
                   "range"   if adx_val  <= ADX_RANGE_MAX  else "neutral")

    bb_signal   = ("volatile" if bb_z_val >= BB_EXPAND_Z   else
                   "range"    if bb_z_val <= BB_CONTRACT_Z  else "neutral")

    chop_signal = ("range"   if chop_val >= CHOP_RANGE_MIN else
                   "trend"   if chop_val <= CHOP_TREND_MAX  else "neutral")

    # ── Vote tally ─────────────────────────────────────────────────────────
    trend_votes    = sum(s == "trend"    for s in [adx_signal, chop_signal])
    range_votes    = sum(s == "range"    for s in [adx_signal, bb_signal, chop_signal])
    volatile_votes = sum(s == "volatile" for s in [bb_signal])  # BB width is the volatile detector

    # ── Regime decision ────────────────────────────────────────────────────
    # Volatile has veto power when BB is clearly expanding — BTC can explode mid-trend
    if bb_z_val >= BB_EXPAND_Z and volatile_votes >= 1:
        regime, winning, possible = "volatile", 1, 1
    elif trend_votes >= 2:
        regime, winning, possible = "trend", trend_votes, 2
    elif range_votes >= 2:
        regime, winning, possible = "range", range_votes, 3
    elif trend_votes == 1 and range_votes <= 1:
        # Soft trend — ADX is tie-breaker
        regime = "trend" if adx_signal == "trend" else "range"
        winning, possible = 1, 2
    else:
        regime, winning, possible = "range", 1, 3   # default — assume choppy

    confidence = round(winning / possible, 3)

    return RegimeResult(
        regime=regime, adx=adx_val, adx_signal=adx_signal,
        bb_width=bb_w_val, bb_width_z=bb_z_val, bb_signal=bb_signal,
        chop=chop_val, chop_signal=chop_signal,
        trend_votes=trend_votes, range_votes=range_votes,
        volatile_votes=volatile_votes, confidence=confidence,
    )


# ── Rolling history (backtesting only) ────────────────────────────────────

def rolling_regimes(df: pd.DataFrame, min_bars: int = 80) -> pd.Series:
    """
    Classify regime at every bar. NOT used in live cycle (only last bar).
    Useful for feature matrix construction in backtesting.
    """
    regimes = pd.Series("", index=df.index, dtype=str)
    for i in range(min_bars, len(df)):
        regimes.iloc[i] = classify_regime(df.iloc[:i + 1]).regime
    return regimes


# ── Standalone test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    np.random.seed(42)
    n = 300

    def _make_ohlc(returns: np.ndarray) -> pd.DataFrame:
        m = len(returns)
        close = 50000 * np.cumprod(1 + returns)
        high  = close * (1 + np.abs(np.random.randn(m) * 0.002))
        low   = close * (1 - np.abs(np.random.randn(m) * 0.002))
        return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})

    print("=== Regime Engine — Tests ===\n")

    # 1. Strong trend
    df_trend = _make_ohlc(np.random.randn(n) * 0.005 + 0.004)
    r = classify_regime(df_trend)
    print(f"Trend:    {r}")
    assert r.regime == "trend", f"Expected trend, got {r.regime}"

    # 2. Range (tiny drift)
    df_range = _make_ohlc(np.random.randn(n) * 0.0008)
    r = classify_regime(df_range)
    print(f"Range:    {r}")
    assert r.regime == "range", f"Expected range, got {r.regime}"

    # 3. Volatile (sudden expansion in last 30 bars)
    rets_vol = np.random.randn(n) * 0.002
    rets_vol[-30:] = np.random.randn(30) * 0.030
    df_vol = _make_ohlc(rets_vol)
    r = classify_regime(df_vol)
    print(f"Volatile: {r}")
    assert r.regime == "volatile", f"Expected volatile, got {r.regime}"

    # 4. Short data — no crash
    df_short = _make_ohlc(np.random.randn(30) * 0.003)
    r = classify_regime(df_short)
    print(f"Short 30: {r}")
    assert r.regime in ("trend", "range", "volatile")

    # 5. Dict output
    d = classify_regime(df_trend).to_dict()
    assert set(d.keys()) >= {"regime", "adx", "bb_width_z", "chop", "confidence"}

    # 6. ADX sanity (0-100)
    assert 0 <= classify_regime(df_trend).adx <= 100, "ADX out of range"

    print(f"\nDict: {d}")
    print("\n✅ All regime tests passed")
