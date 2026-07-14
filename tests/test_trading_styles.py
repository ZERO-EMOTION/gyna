"""
Gyna — tests/test_trading_styles.py
Scalper (pure price action), Runner (trend rider), learned arbitration.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from trading_styles import (
    evaluate_scalper, evaluate_runner, style_weight,
    SCALPER, RUNNER, SCALPER_TP_RANGE, RUNNER_TP_RANGE,
)
from edge_engine import EdgeEngine


# ── Snapshot builders ──────────────────────────────────────────────────────

def _pa(consec=0, burst=1.0, close_pos=0.5, swept_low=False, swept_high=False):
    """Price-action-only snapshot slice (what the scalper sees)."""
    return {"consec_dir_closes": consec, "burst_range_ratio": burst,
            "last_close_pos": close_pos, "swept_low": swept_low,
            "swept_high": swept_high, "avg_range_14": 100.0}


def _trend(direction="BULLISH", session="LONDON"):
    """Snapshot slice satisfying the runner's gates."""
    return {"regime": "TREND", "session": session,
            "hma_trend": direction, "hhll_bias": direction,
            "structure_strength": 0.8,
            "macd_hist_z": 1.0 if direction == "BULLISH" else -1.0,
            "rsi_dist_50": 15.0 if direction == "BULLISH" else -15.0}


def _perf(losses=0):
    return {"current_drawdown_pct": 0.0, "consecutive_losses": losses,
            "win_rate_calibrated": 0.55, "eqd_coefficient": 0.0}


# ── Scalper: pure price action, no lagging indicators ─────────────────────

def test_scalper_burst_long():
    sig = evaluate_scalper(_pa(consec=4, burst=1.5, close_pos=0.85))
    assert sig["style"] == SCALPER and sig["direction"] == 1
    assert sig["setup"] == "burst_long"
    assert sig["tp_range"] == SCALPER_TP_RANGE   # quick targets


def test_scalper_burst_short():
    sig = evaluate_scalper(_pa(consec=-4, burst=1.5, close_pos=0.15))
    assert sig["direction"] == -1 and sig["setup"] == "burst_short"


def test_scalper_sweep_reclaim_beats_burst():
    sig = evaluate_scalper(_pa(consec=-4, burst=1.5, close_pos=0.15,
                               swept_low=True))
    assert sig["direction"] == 1 and sig["setup"] == "sweep_reclaim_long"


def test_scalper_weak_burst_is_flat():
    assert evaluate_scalper(_pa(consec=2, burst=1.5, close_pos=0.9)) is None
    assert evaluate_scalper(_pa(consec=4, burst=0.9, close_pos=0.9)) is None
    assert evaluate_scalper(_pa(consec=4, burst=1.5, close_pos=0.5)) is None


def test_scalper_ignores_lagging_indicators():
    """The scalper must produce the SAME signal whether or not RSI/MACD/BB
    exist in the snapshot — proof it uses zero lagging indicators."""
    bare  = _pa(consec=4, burst=1.5, close_pos=0.85)
    laggy = dict(bare, rsi_14=5.0, macd_hist_z=-3.0, bb_position=0.99,
                 hma_trend="BEARISH", regime="RANGE")
    assert evaluate_scalper(bare) == evaluate_scalper(laggy)


# ── Runner: trend rider ────────────────────────────────────────────────────

def test_runner_rides_confirmed_trend():
    sig = evaluate_runner(_trend("BULLISH"))
    assert sig["style"] == RUNNER and sig["direction"] == 1
    assert sig["tp_range"] == RUNNER_TP_RANGE    # 3R+ asymmetric


def test_runner_bearish():
    assert evaluate_runner(_trend("BEARISH"))["direction"] == -1


def test_runner_requires_trend_regime():
    snap = dict(_trend("BULLISH"), regime="RANGE")
    assert evaluate_runner(snap) is None


def test_runner_requires_liquid_session():
    assert evaluate_runner(_trend("BULLISH", session="ASIA")) is None


def test_runner_requires_structure_agreement():
    snap = dict(_trend("BULLISH"), hhll_bias="BEARISH")
    assert evaluate_runner(snap) is None


# ── Learned arbitration ────────────────────────────────────────────────────

def test_style_weight_neutral_until_ten_trades():
    stats = {SCALPER: {"total_trades": 5, "profit_factor": 3.0}}
    assert style_weight(stats, SCALPER) == 1.0
    assert style_weight(None, SCALPER) == 1.0


def test_style_weight_scales_with_live_pf():
    good = {RUNNER: {"total_trades": 30, "profit_factor": 2.0}}
    bad  = {RUNNER: {"total_trades": 30, "profit_factor": 0.6}}
    assert style_weight(good, RUNNER) > 1.0
    assert style_weight(bad, RUNNER) < 1.0
    assert 0.6 <= style_weight(bad, RUNNER) <= 1.4


def test_edge_learned_stats_flip_style_choice():
    """Both styles signal long; the one with the better LIVE record wins."""
    eng = EdgeEngine()
    both = {**_trend("BULLISH"), **_pa(consec=4, burst=1.6, close_pos=0.9),
            "regime_duration": 5, "atr_percentile": 50.0, "volume_z": 1.0,
            "allowed_sl_atr_range": [1.0, 2.0],
            "allowed_tp_atr_range": [2.0, 4.0]}

    runner_hot = {RUNNER:  {"total_trades": 30, "profit_factor": 2.5},
                  SCALPER: {"total_trades": 30, "profit_factor": 0.5}}
    scalper_hot = {RUNNER:  {"total_trades": 30, "profit_factor": 0.5},
                   SCALPER: {"total_trades": 30, "profit_factor": 2.5}}

    assert eng.process_state(dict(both), _perf(),
                             style_stats=runner_hot)["style"] == RUNNER
    assert eng.process_state(dict(both), _perf(),
                             style_stats=scalper_hot)["style"] == SCALPER


def test_edge_style_envelope_overrides_regime_default():
    eng = EdgeEngine()
    snap = {**_trend("BULLISH"), "regime_duration": 5,
            "atr_percentile": 50.0, "volume_z": 1.0,
            "allowed_sl_atr_range": [9.0, 9.9],   # regime default — must be replaced
            "allowed_tp_atr_range": [9.0, 9.9]}
    out = eng.process_state(snap, _perf())
    assert out["style"] == RUNNER
    assert out["allowed_tp_atr_range"] == RUNNER_TP_RANGE


def test_edge_three_losses_flatten_all_styles():
    eng = EdgeEngine()
    snap = {**_trend("BULLISH"), **_pa(consec=4, burst=1.6, close_pos=0.9),
            "regime_duration": 5, "atr_percentile": 50.0, "volume_z": 1.0}
    out = eng.process_state(snap, _perf(losses=3))
    assert out["permitted_direction"] == 0
    assert out["style"] is None
