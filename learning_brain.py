"""
Gyna — learning_brain.py
GynaBrain: an ACTUAL machine-learning model whose weights update after every
closed trade. Online logistic regression with AdaGrad, implemented from
scratch in numpy — every weight is inspectable, nothing is a black box.

What it does:
  predict(snapshot)  -> P(win) for the candidate trade + confidence
  update(features,y) -> gradient step the moment a trade closes (win/loss)

How it plugs in:
  - The prediction SCALES Claude's aggression (0.6x-1.4x at full confidence)
  - Confidence ramps from 0 with the number of updates seen — the brain has
    ZERO influence until it has real experience, and earns its voice
  - A mature brain (>=100 updates) can hard-veto trades it scores < 30%
    (that veto is a safety filter and obeys the master SAFETY_FILTERS toggle;
    the sizing modulation is core intelligence and is always on)
  - One brain file per instance folder — XAUUSD and BTCUSD learn separately

This is genuine ML — weight updates from live outcomes — deliberately kept
LOW-VARIANCE (linear model, L2, per-feature normalization) because trading
samples are few and noisy; a deep net here would memorize noise.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("Gyna.Brain")

# Fixed feature order — changing this invalidates saved brains, bump VERSION.
BRAIN_VERSION = 1

NUMERIC_FEATURES: List[str] = [
    # (snapshot key, scale divisor, clip)
    "rsi_dist_50",            # -50..50
    "macd_hist_z",
    "bb_position",
    "atr_percentile",
    "structure_strength",
    "volume_z",
    "regime_fatigue_factor",
    "consec_dir_closes",
    "burst_range_ratio",
    "last_close_pos",
    "edge_quality_score",
    "transition_frequency",
]
REGIMES  = ["TREND", "RANGE", "VOLATILE"]
SESSIONS = ["ASIA", "LONDON", "NY_OVERLAP", "NY", "LATE_PACIFIC"]
STYLES   = ["scalper", "runner"]

FEATURE_NAMES: List[str] = (
    NUMERIC_FEATURES
    + [f"regime_{r}" for r in REGIMES]
    + [f"session_{s}" for s in SESSIONS]
    + [f"style_{s}" for s in STYLES]
    + ["direction", "swept_low", "swept_high", "bias"]
)
N_FEATURES = len(FEATURE_NAMES)

# Influence shape
CONFIDENCE_FULL_AT = 200   # updates needed for full voice
VETO_MIN_UPDATES   = 100   # brain may hard-veto only after this many updates
VETO_P_THRESHOLD   = 0.30
SCALAR_MIN, SCALAR_MAX = 0.6, 1.4


def featurize(snap: Dict[str, Any]) -> np.ndarray:
    """Snapshot (post-EdgeEngine) -> fixed-order feature vector."""
    x = np.zeros(N_FEATURES, dtype=np.float64)
    i = 0
    for key in NUMERIC_FEATURES:
        try:
            x[i] = float(snap.get(key) or 0.0)
        except (TypeError, ValueError):
            x[i] = 0.0
        i += 1
    regime = str(snap.get("regime") or "")
    for r in REGIMES:
        x[i] = 1.0 if regime == r else 0.0
        i += 1
    session = str(snap.get("session") or "")
    for s_ in SESSIONS:
        x[i] = 1.0 if session == s_ else 0.0
        i += 1
    style = str(snap.get("style") or "")
    for s_ in STYLES:
        x[i] = 1.0 if style == s_ else 0.0
        i += 1
    x[i] = float(snap.get("permitted_direction") or 0); i += 1
    x[i] = 1.0 if snap.get("swept_low") else 0.0;       i += 1
    x[i] = 1.0 if snap.get("swept_high") else 0.0;      i += 1
    x[i] = 1.0                                          # bias
    return x


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


class GynaBrain:
    def __init__(self, path: str = "memory/brain.json",
                 lr: float = 0.08, l2: float = 1e-4):
        self.path = path
        self.lr   = lr
        self.l2   = l2
        self.w    = np.zeros(N_FEATURES)
        self.g2   = np.zeros(N_FEATURES)          # AdaGrad accumulator
        self.mean = np.zeros(N_FEATURES)          # Welford running stats
        self.m2   = np.zeros(N_FEATURES)
        self.n_norm    = 0
        self.n_updates = 0
        self._load()

    # ── Normalization (online Welford; one-hots/bias pass through) ─────────

    def _norm_mask(self) -> np.ndarray:
        m = np.zeros(N_FEATURES, dtype=bool)
        m[:len(NUMERIC_FEATURES)] = True
        return m

    def _observe(self, x: np.ndarray) -> None:
        self.n_norm += 1
        delta = x - self.mean
        self.mean += delta / self.n_norm
        self.m2   += delta * (x - self.mean)

    def _normalize(self, x: np.ndarray) -> np.ndarray:
        z = x.copy()
        if self.n_norm >= 2:
            std = np.sqrt(self.m2 / max(self.n_norm - 1, 1))
            std[std < 1e-9] = 1.0
            mask = self._norm_mask()
            z[mask] = np.clip((x[mask] - self.mean[mask]) / std[mask], -5, 5)
        z[-1] = 1.0   # bias untouched
        return z

    # ── Inference ───────────────────────────────────────────────────────────

    def predict(self, snap: Dict[str, Any]) -> Tuple[float, float]:
        """Returns (p_win, confidence). Confidence 0 -> the brain is mute."""
        x = self._normalize(featurize(snap))
        p = _sigmoid(float(self.w @ x))
        confidence = min(1.0, self.n_updates / CONFIDENCE_FULL_AT)
        return round(p, 4), round(confidence, 3)

    def aggression_scalar(self, p: float, confidence: float) -> float:
        """Sizing multiplier: neutral 1.0 with no experience, up to
        0.6x-1.4x at full confidence."""
        raw = 1.0 + (p - 0.5) * 1.6 * confidence
        return round(max(SCALAR_MIN, min(SCALAR_MAX, raw)), 3)

    def should_veto(self, p: float) -> bool:
        """Hard veto — only for a mature brain, only on very poor scores.
        Caller gates this behind the master SAFETY_FILTERS toggle."""
        return self.n_updates >= VETO_MIN_UPDATES and p < VETO_P_THRESHOLD

    # ── Learning (THE weight update) ────────────────────────────────────────

    def update(self, features: Any, won: bool, weight: float = 1.0) -> float:
        """
        One online gradient step from a real outcome. Called the moment a
        trade closes. Returns the pre-update P(win) for calibration logging.
        Accepts a raw feature vector (list/ndarray) or a snapshot dict.
        """
        x_raw = (featurize(features) if isinstance(features, dict)
                 else np.asarray(features, dtype=np.float64))
        if x_raw.shape[0] != N_FEATURES:
            log.warning(f"[BRAIN] Feature size mismatch "
                        f"({x_raw.shape[0]} != {N_FEATURES}) — skipping update")
            return 0.5
        self._observe(x_raw)
        x = self._normalize(x_raw)
        p = _sigmoid(float(self.w @ x))
        y = 1.0 if won else 0.0

        grad = (p - y) * x * weight + self.l2 * self.w
        self.g2 += grad * grad
        self.w  -= (self.lr / np.sqrt(1.0 + self.g2)) * grad

        self.n_updates += 1
        self._save()
        log.info(f"[BRAIN] update #{self.n_updates}: outcome="
                 f"{'WIN' if won else 'LOSS'} predicted={p:.3f}")
        return p

    # ── Introspection ───────────────────────────────────────────────────────

    def top_weights(self, k: int = 8) -> List[Tuple[str, float]]:
        """The k most influential features — the brain in plain English."""
        idx = np.argsort(-np.abs(self.w))[:k]
        return [(FEATURE_NAMES[i], round(float(self.w[i]), 4)) for i in idx]

    # ── Persistence ─────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "version":   BRAIN_VERSION,
                    "features":  FEATURE_NAMES,
                    "w":         self.w.tolist(),
                    "g2":        self.g2.tolist(),
                    "mean":      self.mean.tolist(),
                    "m2":        self.m2.tolist(),
                    "n_norm":    self.n_norm,
                    "n_updates": self.n_updates,
                }, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning(f"[BRAIN] Save failed: {e}")

    def _load(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("version") != BRAIN_VERSION or \
               d.get("features") != FEATURE_NAMES:
                log.warning("[BRAIN] Saved brain is from an incompatible "
                            "feature set — starting fresh")
                return
            self.w         = np.asarray(d["w"])
            self.g2        = np.asarray(d["g2"])
            self.mean      = np.asarray(d["mean"])
            self.m2        = np.asarray(d["m2"])
            self.n_norm    = int(d["n_norm"])
            self.n_updates = int(d["n_updates"])
            log.info(f"[BRAIN] Loaded: {self.n_updates} lifetime updates | "
                     f"top weights: {self.top_weights(5)}")
        except Exception as e:
            log.warning(f"[BRAIN] Load failed ({e}) — starting fresh")
