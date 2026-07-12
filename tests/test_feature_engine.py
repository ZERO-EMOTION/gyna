"""
Gyna — tests/test_feature_engine.py
FeatureEngine: snapshot integrity, state-signature recurrence.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest
from feature_engine import FeatureEngine, _state_signature


def _make_df(n=300, seed=42, start="2026-01-01"):
    rng = np.random.default_rng(seed)
    close = 60000 * np.cumprod(1 + rng.normal(0, 0.002, n))
    high  = close * (1 + np.abs(rng.normal(0, 0.001, n)))
    low   = close * (1 - np.abs(rng.normal(0, 0.001, n)))
    idx   = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"open": close, "high": high, "low": low,
                         "close": close, "volume": rng.integers(1, 100, n)},
                        index=idx)


@pytest.fixture(scope="module")
def snap():
    return FeatureEngine("BTCUSD", "M1").generate_snapshot(_make_df())


def test_snapshot_has_both_hashes(snap):
    assert len(snap["snapshot_hash"]) == 64
    assert len(snap["state_signature"]) == 16


def test_snapshot_hash_unique_per_bar():
    """Full hash includes the timestamp — different bars, different hash."""
    engine = FeatureEngine("BTCUSD", "M1")
    s1 = engine.generate_snapshot(_make_df(start="2026-01-01"))
    s2 = engine.generate_snapshot(_make_df(start="2026-02-01"))
    assert s1["snapshot_hash"] != s2["snapshot_hash"]


def test_state_signature_recurs_for_same_market_state():
    """Signature must be identical for identical quantized states even when
    timestamp/price differ — this is what makes toxicity matching possible."""
    base = {"regime": "TREND", "session": "NY", "hma_trend": "BULLISH",
            "hhll_bias": "BULLISH", "rsi_14": 62.3, "bb_position": 0.71,
            "atr_percentile": 55.0, "volume_z": 0.4, "macd_hist_z": 0.8}
    other_bar = dict(base, timestamp="2026-03-01T00:00:00", price=61234.56,
                     rsi_14=64.9)           # same decile bucket (60-70)
    assert _state_signature(base) == _state_signature(other_bar)


def test_state_signature_changes_across_regimes():
    a = {"regime": "TREND", "session": "NY", "hma_trend": "BULLISH",
         "hhll_bias": "BULLISH", "rsi_14": 62.0, "bb_position": 0.7,
         "atr_percentile": 55.0, "volume_z": 0.4, "macd_hist_z": 0.8}
    b = dict(a, regime="RANGE")
    assert _state_signature(a) != _state_signature(b)


def test_snapshot_is_json_serializable(snap):
    import json
    json.dumps(snap)  # must not raise


def test_risk_envelope_travels_with_snapshot(snap):
    assert len(snap["allowed_sl_atr_range"]) == 2
    assert len(snap["allowed_tp_atr_range"]) == 2
    assert snap["allowed_sl_atr_range"][0] < snap["allowed_sl_atr_range"][1]
