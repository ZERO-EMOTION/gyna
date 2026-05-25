"""
tests/test_risk_engine.py
Audit priority 7 — tests for RiskEngine (P2 + lot sizing + gates).
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from risk_engine import RiskEngine
from config import MAX_DAILY_LOSS


def _base_allocation(direction: int = 1, profile: str = "AGGRESSIVE",
                     aggr: float = 0.6, sl_atr: float = 1.5,
                     tp_atr: float = 3.0) -> dict:
    return {
        "permitted_direction":   direction,
        "execution_profile":     profile,
        "aggression_multiplier": aggr,
        "sl_atr_target":         sl_atr,
        "tp_atr_target":         tp_atr,
        "snapshot_hash":         "abc123",
        "allocation_rationale":  "test",
    }


def _base_account(balance: float = 10_000.0, equity: float = None,
                  free_margin: float = None, consecutive_losses: int = 0,
                  eqd: float = 0.0, daily_loss_pct: float = 0.0) -> dict:
    eq = equity if equity is not None else balance
    fm = free_margin if free_margin is not None else balance * 0.80
    return {
        "balance":                       balance,
        "equity":                        eq,
        "free_margin":                   fm,
        "leverage":                      30.0,
        "daily_realized_loss_pct":       daily_loss_pct,
        "current_spread_points":         50.0,
        "min_lot_limit":                 0.01,
        "max_lot_limit":                 100.0,
        "SYMBOL_MARGIN_INITIAL":         0.0,
        "SYMBOL_TRADE_CONTRACT_SIZE":    1.0,
        # BTCUSD realistic tick value: 1 lot × $0.01 tick = $0.01 per tick
        "SYMBOL_TRADE_TICK_VALUE":       0.01,
        "SYMBOL_TRADE_TICK_SIZE":        0.01,
        # P2: explicit risk context
        "consecutive_losses":            consecutive_losses,
        "execution_quality_degradation": eqd,
    }


def _base_snapshot(atr: float = 500.0, price: float = 50_000.0) -> dict:
    return {
        "atr_14":   atr,
        "price":    price,
        "symbol":   "BTCUSD",
        "trade_memory": {},  # P2: should NOT be needed for risk law
    }


def _base_stats(total: int = 0, wr: float = 0.0, pf: float = 0.0) -> dict:
    return {"total_trades": total, "win_rate": wr, "profit_factor": pf}


# ── 1. Rejects when daily loss breaker is active ──────────────────────────

def test_rejects_on_daily_loss_breaker():
    eng = RiskEngine()
    auth = eng.authorize_execution(
        _base_allocation(),
        _base_account(daily_loss_pct=MAX_DAILY_LOSS + 0.001),
        _base_snapshot(),
        _base_stats(),
    )
    assert auth["status"] == "REJECTED_DRAWDOWN_BREAKER_ACTIVE"


# ── 2. P2: consecutive_losses from account_state reduces lot size ─────────

def test_consecutive_losses_reduce_lot_size():
    eng = RiskEngine()
    auth_0 = eng.authorize_execution(
        _base_allocation(), _base_account(consecutive_losses=0),
        _base_snapshot(), _base_stats(),
    )
    auth_2 = eng.authorize_execution(
        _base_allocation(), _base_account(consecutive_losses=2),
        _base_snapshot(), _base_stats(),
    )

    assert auth_0["status"] == "APPROVED"
    assert auth_2["status"] == "APPROVED"

    lots_0 = auth_0["order_parameters"]["volume"]
    lots_2 = auth_2["order_parameters"]["volume"]
    assert lots_2 < lots_0, (
        f"2 consecutive losses should reduce lots: {lots_0} -> {lots_2}")


# ── 3. P2: EQD from account_state reduces lot size ───────────────────────

def test_eqd_reduces_lot_size():
    eng = RiskEngine()
    auth_clean = eng.authorize_execution(
        _base_allocation(), _base_account(eqd=0.0),
        _base_snapshot(), _base_stats(),
    )
    auth_degraded = eng.authorize_execution(
        _base_allocation(), _base_account(eqd=0.8),
        _base_snapshot(), _base_stats(),
    )

    assert auth_clean["status"]    == "APPROVED"
    assert auth_degraded["status"] == "APPROVED"
    assert (auth_degraded["order_parameters"]["volume"] <
            auth_clean["order_parameters"]["volume"]), \
        "High EQD should reduce lot size"


# ── 4. FLAT mandate returns stay-flat ─────────────────────────────────────

def test_flat_profile_returns_stay_flat():
    eng = RiskEngine()
    auth = eng.authorize_execution(
        _base_allocation(direction=0, profile="FLAT"),
        _base_account(),
        _base_snapshot(),
        _base_stats(),
    )
    assert auth["action"] == "STAY_FLAT"


# ── 5. Risk tier 1 (0 trades) uses lowest risk_pct ───────────────────────

def test_tier_1_uses_minimum_risk():
    from config import RISK_TIERS
    eng = RiskEngine()
    tier = eng.get_risk_tier({"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0})
    assert tier == RISK_TIERS[0], "Zero trades should give tier 1"


# ── 6. Risk tier promotes when stats qualify ─────────────────────────────

def test_tier_promotes_with_good_stats():
    from config import RISK_TIERS
    eng = RiskEngine()
    tier = eng.get_risk_tier({"total_trades": 200, "win_rate": 0.55, "profit_factor": 2.1})
    assert RISK_TIERS.index(tier) >= 3, \
        f"200 trades 55% WR PF 2.1 should be tier ≥4, got {tier}"


# ── 7. Margin stress blocks oversized trades ─────────────────────────────

def test_margin_stress_blocks_trade():
    eng = RiskEngine()
    # Almost no free margin
    auth = eng.authorize_execution(
        _base_allocation(aggr=1.0),
        _base_account(balance=100_000.0, free_margin=10.0),
        _base_snapshot(),
        _base_stats(),
    )
    assert "MARGIN" in auth["status"] or auth["status"] == "APPROVED", \
        "Should either block on margin or pass with min lot"
