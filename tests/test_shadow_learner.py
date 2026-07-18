"""
Gyna — tests/test_shadow_learner.py
Shadow learning: skipped signals get recorded, hindsight-labeled from price
history, and taught to the brain at reduced weight.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import pandas as pd
import pytest
from learning_brain import GynaBrain, featurize
from shadow_learner import ShadowLearner, SHADOW_WEIGHT, MIN_AGE_S


def _masked(direction=1, price=3000.0, atr=10.0):
    return {"permitted_direction": direction, "price": price, "atr_14": atr,
            "style": "scalper", "regime": "TREND", "session": "LONDON",
            "allowed_sl_atr_range": [0.5, 0.9],    # SL mid = 0.7*atr = 7.0
            "allowed_tp_atr_range": [0.7, 1.4],    # TP mid = 1.05*atr = 10.5
            "rsi_dist_50": 5.0, "macd_hist_z": 0.5, "bb_position": 0.6,
            "atr_percentile": 50.0, "structure_strength": 0.7,
            "volume_z": 1.0, "regime_fatigue_factor": 0.0,
            "consec_dir_closes": 3, "burst_range_ratio": 1.3,
            "last_close_pos": 0.8, "edge_quality_score": 0.6,
            "transition_frequency": 0.05, "swept_low": False,
            "swept_high": False}


def _bars(highs, lows):
    return pd.DataFrame({"high": highs, "low": lows})


@pytest.fixture
def setup(tmp_path):
    brain  = GynaBrain(path=str(tmp_path / "brain.json"))
    shadow = ShadowLearner(str(tmp_path / "state.db"), brain)
    return brain, shadow


def _age_all(shadow, seconds=MIN_AGE_S + 60):
    """Backdate every pending shadow so it becomes due for resolution."""
    import sqlite3
    with sqlite3.connect(shadow.db_path) as conn:
        conn.execute("UPDATE shadow_trades SET timestamp = timestamp - ?;",
                     (seconds,))
        conn.commit()


def test_skip_becomes_a_win_lesson(setup):
    """JP's principle end-to-end: skip -> look back -> learn it was a BUY."""
    brain, shadow = setup
    m = _masked(direction=1, price=3000.0, atr=10.0)   # TP at 3010.5, SL 2993
    shadow.record(m, featurize(m), "brain_veto")
    _age_all(shadow)
    # Price ran straight up through the TP without touching the SL
    learned = shadow.resolve_due(lambda ts: _bars([3005, 3012], [2999, 3004]))
    assert learned == 1
    assert brain.n_updates == 1
    assert shadow.stats()["brain_veto"]["win"] == 1


def test_skip_confirmed_as_wise(setup):
    brain, shadow = setup
    m = _masked(direction=1, price=3000.0, atr=10.0)
    shadow.record(m, featurize(m), "llm_flat")
    _age_all(shadow)
    learned = shadow.resolve_due(lambda ts: _bars([3002], [2990]))  # SL hit
    assert learned == 1
    assert shadow.stats()["llm_flat"]["loss"] == 1


def test_conservative_same_bar_tie_counts_as_loss(setup):
    brain, shadow = setup
    m = _masked(direction=-1, price=3000.0, atr=10.0)  # short: SL 3007, TP 2989.5
    shadow.record(m, featurize(m), "risk_rejected")
    _age_all(shadow)
    shadow.resolve_due(lambda ts: _bars([3010], [2985]))  # both in one bar
    assert shadow.stats()["risk_rejected"]["loss"] == 1


def test_too_young_shadows_wait(setup):
    brain, shadow = setup
    m = _masked()
    shadow.record(m, featurize(m), "brain_veto")   # fresh — under MIN_AGE_S
    assert shadow.resolve_due(lambda ts: _bars([9999], [0])) == 0
    assert brain.n_updates == 0


def test_unavailable_history_leaves_pending(setup):
    brain, shadow = setup
    m = _masked()
    shadow.record(m, featurize(m), "brain_veto")
    _age_all(shadow)
    assert shadow.resolve_due(lambda ts: None) == 0     # feed down — retry later
    learned = shadow.resolve_due(lambda ts: _bars([3050], [2999]))
    assert learned == 1                                  # resolved on retry


def test_shadow_weight_is_reduced(setup):
    """A hindsight lesson must move the weights LESS than a real fill."""
    brain, shadow = setup
    m = _masked()
    vec = featurize(m)
    import numpy as np
    b_real = GynaBrain(path=brain.path + ".real")
    b_real.update(vec, won=True, weight=1.0)
    real_move = float(np.abs(b_real.w).sum())

    shadow.record(m, vec, "brain_veto")
    _age_all(shadow)
    shadow.resolve_due(lambda ts: _bars([3050], [2999]))
    shadow_move = float(np.abs(brain.w).sum())
    assert 0 < shadow_move < real_move
    assert SHADOW_WEIGHT < 1.0


def test_flat_signal_is_not_recorded(setup):
    brain, shadow = setup
    shadow.record(_masked(direction=0), [0.0], "llm_flat")
    _age_all(shadow)
    assert shadow.resolve_due(lambda ts: _bars([9999], [0])) == 0
