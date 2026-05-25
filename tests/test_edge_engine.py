"""
tests/test_edge_engine.py
Audit priority 7 — tests for EdgeEngine (directional mask + FLAT enforcement).
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from edge_engine import EdgeEngine


def _trend_snap(direction: str = "BULLISH") -> dict:
    """Minimal TREND snapshot that should produce a permitted direction."""
    return {
        "regime":            "TREND",
        "hma_trend":         direction,
        "hhll_bias":         direction,
        "structure_strength": 0.8,
        "bb_position":       0.5,
        "bb_width_z":        0.0,
        "atr_percentile":    50.0,
        "session":           "LONDON",
        "volume_z":          2.0,
        "regime_duration":   5,
        "macd_hist_z":       1.0 if direction == "BULLISH" else -1.0,
        "rsi_dist_50":       15.0 if direction == "BULLISH" else -15.0,
        "snapshot_hash":     "test",
        "price":             50000.0,
        "symbol":            "BTCUSD",
        "atr_14":            500.0,
        "rsi_14":            60.0,
        "macd_hist":         0.5,
        "hma_trend":         direction,
        "hhll_bias":         direction,
        "allowed_sl_atr_range": [1.0, 2.5],
        "allowed_tp_atr_range": [2.0, 4.0],
    }


def _perf_ctx(consecutive_losses: int = 0, eqd: float = 0.0) -> dict:
    return {
        "current_drawdown_pct":  0.0,
        "consecutive_losses":    consecutive_losses,
        "win_rate_calibrated":   0.55,
        "eqd_coefficient":       eqd,
    }


# ── 1. Bullish TREND → permitted_direction = 1 ───────────────────────────

def test_bullish_trend_gives_direction_1():
    eng   = EdgeEngine()
    snap  = _trend_snap("BULLISH")
    result = eng.process_state(snap, _perf_ctx())
    assert result["permitted_direction"] == 1


# ── 2. Bearish TREND → permitted_direction = -1 ──────────────────────────

def test_bearish_trend_gives_direction_minus1():
    eng   = EdgeEngine()
    snap  = _trend_snap("BEARISH")
    result = eng.process_state(snap, _perf_ctx())
    assert result["permitted_direction"] == -1


# ── 3. 3 consecutive losses → FLAT regardless of signal ──────────────────

def test_three_consecutive_losses_forces_flat():
    eng   = EdgeEngine()
    snap  = _trend_snap("BULLISH")
    result = eng.process_state(snap, _perf_ctx(consecutive_losses=3))
    assert result["permitted_direction"] == 0, \
        "3 consecutive losses must force FLAT"
    assert result["edge_quality_score"] == 0.0


# ── 4. edge_quality_score never exceeds 1.0 ──────────────────────────────

def test_edge_quality_score_capped():
    eng   = EdgeEngine()
    snap  = _trend_snap("BULLISH")
    result = eng.process_state(snap, _perf_ctx())
    assert 0.0 <= result["edge_quality_score"] <= 1.0, \
        f"Score out of range: {result['edge_quality_score']}"


# ── 5. Fatigued regime reduces edge score ────────────────────────────────

def test_regime_fatigue_reduces_score():
    eng = EdgeEngine(fatigue_threshold_bars=10)

    fresh_snap   = dict(_trend_snap("BULLISH"), regime_duration=5)
    fatigued_snap = dict(_trend_snap("BULLISH"), regime_duration=60)

    r_fresh    = eng.process_state(fresh_snap,    _perf_ctx())
    r_fatigued = eng.process_state(fatigued_snap, _perf_ctx())

    assert r_fatigued["edge_quality_score"] < r_fresh["edge_quality_score"], \
        "Fatigued regime should reduce edge score"


# ── 6. LLM allocator cannot set aggression_multiplier > edge_quality_score

def test_allocator_cannot_exceed_edge_quality_score():
    """Verify the contract: aggression ≤ edge_quality_score."""
    from claude_allocator import ClaudeAllocator
    eng        = EdgeEngine()
    snap       = _trend_snap("BULLISH")
    masked     = eng.process_state(snap, _perf_ctx())
    edge_score = masked["edge_quality_score"]

    allocator  = ClaudeAllocator()
    # Force local fallback (no API keys in test env)
    result     = allocator._local_fallback(masked)

    assert result["aggression_multiplier"] <= edge_score + 1e-6, \
        (f"Allocator aggression {result['aggression_multiplier']} "
         f"exceeds edge_quality_score {edge_score}")


# ── 7. RANGE regime: no direction when bands not compressed ──────────────

def test_range_no_direction_without_squeeze():
    eng = EdgeEngine()
    snap = {
        "regime":            "RANGE",
        "hma_trend":         "BULLISH",
        "hhll_bias":         "BULLISH",
        "structure_strength": 0.7,
        "bb_position":       0.50,   # middle of bands — no extreme
        "bb_width_z":        0.5,    # not squeezed (need < -0.5)
        "atr_percentile":    50.0,
        "session":           "LONDON",
        "volume_z":          1.0,
        "regime_duration":   5,
        "macd_hist_z":       0.3,    # below 0.5 threshold
        "rsi_dist_50":       5.0,    # below 10 threshold
        "snapshot_hash":     "test",
        "price":             50000.0,
        "symbol":            "BTCUSD",
        "atr_14":            300.0,
        "allowed_sl_atr_range": [1.0, 2.5],
        "allowed_tp_atr_range": [2.0, 4.0],
    }
    result = eng.process_state(snap, _perf_ctx())
    assert result["permitted_direction"] == 0, \
        "RANGE with no squeeze / no momentum alignment should stay FLAT"
