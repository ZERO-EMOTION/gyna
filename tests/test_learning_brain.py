"""
Gyna — tests/test_learning_brain.py
GynaBrain: proof that it ACTUALLY learns — weights move with outcomes,
patterns become separable, knowledge survives restarts, influence is earned.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest
from learning_brain import (GynaBrain, featurize, N_FEATURES, FEATURE_NAMES,
                            CONFIDENCE_FULL_AT, VETO_MIN_UPDATES)


def _snap(regime="TREND", session="LONDON", style="runner", direction=1,
          rsi_dist=10.0, vol_z=1.0):
    return {"regime": regime, "session": session, "style": style,
            "permitted_direction": direction, "rsi_dist_50": rsi_dist,
            "macd_hist_z": 0.5, "bb_position": 0.6, "atr_percentile": 50.0,
            "structure_strength": 0.7, "volume_z": vol_z,
            "regime_fatigue_factor": 0.0, "consec_dir_closes": 2,
            "burst_range_ratio": 1.1, "last_close_pos": 0.6,
            "edge_quality_score": 0.6, "transition_frequency": 0.05,
            "swept_low": False, "swept_high": False}


@pytest.fixture
def brain(tmp_path):
    return GynaBrain(path=str(tmp_path / "brain.json"))


def test_featurize_shape_and_missing_keys():
    assert featurize(_snap()).shape == (N_FEATURES,)
    assert featurize({}).shape == (N_FEATURES,)      # degrades, never crashes
    assert featurize({})[-1] == 1.0                  # bias always present


def test_weights_actually_change_on_update(brain):
    w_before = brain.w.copy()
    brain.update(_snap(), won=True)
    assert brain.n_updates == 1
    assert not np.allclose(w_before, brain.w), \
        "update() must move the weights — that IS the learning"


def test_learns_a_real_pattern(brain):
    """THE proof test: TREND/runner wins, RANGE/scalper loses. After
    training, the brain must score the winning pattern clearly higher."""
    good = _snap(regime="TREND", session="LONDON", style="runner")
    bad  = _snap(regime="RANGE", session="ASIA", style="scalper",
                 rsi_dist=-10.0, vol_z=-0.5)
    for _ in range(150):
        brain.update(good, won=True)
        brain.update(bad,  won=False)
    p_good, conf = brain.predict(good)
    p_bad,  _    = brain.predict(bad)
    assert p_good > 0.7, f"winning pattern scored only {p_good}"
    assert p_bad  < 0.3, f"losing pattern scored {p_bad}"
    assert conf == 1.0


def test_knowledge_survives_restart(tmp_path):
    path = str(tmp_path / "brain.json")
    b1 = GynaBrain(path=path)
    good, bad = _snap(regime="TREND"), _snap(regime="VOLATILE", rsi_dist=-20)
    for _ in range(60):
        b1.update(good, won=True)
        b1.update(bad,  won=False)
    p1_good, _ = b1.predict(good)

    b2 = GynaBrain(path=path)                    # fresh process, same file
    p2_good, _ = b2.predict(good)
    assert b2.n_updates == b1.n_updates
    assert p2_good == pytest.approx(p1_good, abs=1e-6), \
        "a restart must not lose what was learned"


def test_influence_is_earned_not_assumed(brain):
    """Day one: zero confidence -> neutral sizing, no veto possible."""
    p, conf = brain.predict(_snap())
    assert conf == 0.0
    assert brain.aggression_scalar(p, conf) == 1.0    # mute until experienced
    assert brain.should_veto(0.01) is False           # immature brains can't veto


def test_veto_requires_maturity_and_terrible_score(brain):
    for _ in range(VETO_MIN_UPDATES):
        brain.update(_snap(), won=True)
    assert brain.should_veto(0.29) is True
    assert brain.should_veto(0.35) is False


def test_scalar_bounded(brain):
    for _ in range(CONFIDENCE_FULL_AT):
        brain.update(_snap(), won=True)
    assert 0.6 <= brain.aggression_scalar(0.01, 1.0) <= 1.4
    assert 0.6 <= brain.aggression_scalar(0.99, 1.0) <= 1.4
    assert brain.aggression_scalar(0.8, 1.0) > 1.0    # good setup -> size up
    assert brain.aggression_scalar(0.35, 1.0) < 1.0   # poor setup -> size down


def test_update_accepts_persisted_feature_vector(brain):
    """The orchestrator stores the entry vector as JSON and replays it at
    close — the brain must learn from the raw list identically."""
    vec = featurize(_snap()).tolist()
    brain.update(vec, won=True)
    assert brain.n_updates == 1
    wrong_size = [0.0] * 3
    assert brain.update(wrong_size, won=True) == 0.5   # rejected, no crash
    assert brain.n_updates == 1


def test_incompatible_brain_file_starts_fresh(tmp_path):
    path = str(tmp_path / "brain.json")
    import json
    with open(path, "w") as f:
        json.dump({"version": 0, "features": ["old"], "w": [1.0]}, f)
    b = GynaBrain(path=path)
    assert b.n_updates == 0
    assert b.w.shape == (N_FEATURES,)


def test_top_weights_are_readable(brain):
    for _ in range(30):
        brain.update(_snap(regime="TREND"), won=True)
        brain.update(_snap(regime="RANGE"), won=False)
    top = brain.top_weights(5)
    assert len(top) == 5
    assert all(name in FEATURE_NAMES for name, _ in top)
