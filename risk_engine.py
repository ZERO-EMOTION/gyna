"""
Gyna — risk_engine.py
Final execution authority. No order passes without RiskEngine approval.

Responsibilities:
  - Daily loss drawdown breaker
  - Transaction friction check (spread vs TP)
  - Scaled risk % (base × aggression_multiplier^1.5)
  - Consecutive-loss half-sizing (0.5^n per loss streak)
  - EQD (execution quality degradation) penalty
  - Margin stress check (≤70% free margin)
  - Lot size computation from cash-at-risk + ATR SL
  - SL/TP round-number offset (avoids sweep clusters)
  - Risk tier promotion/demotion from TradeMemory stats

RiskEngine is the last gate. Claude is interpretation. This is law.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from config import RISK_TIERS, MAX_RISK_PER_TRADE, MAX_DAILY_LOSS

log = logging.getLogger("Gyna.RiskEngine")


class RiskEngine:

    def __init__(self):
        self.risk_tiers = RISK_TIERS

    # ── Risk tier ──────────────────────────────────────────────────────────

    def get_risk_tier(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """
        Determine current risk tier from TradeMemory stats.
        Returns the highest tier whose conditions are ALL met.
        """
        total  = int(stats.get("total_trades", 0))
        wr     = float(stats.get("win_rate", 0.0))
        pf     = float(stats.get("profit_factor", 0.0))

        active_tier = self.risk_tiers[0]  # default Tier 1
        for tier in self.risk_tiers:
            if (total  >= tier["min_trades"] and
                wr     >= tier["min_wr"] and
                pf     >= tier["min_pf"]):
                active_tier = tier
        return active_tier

    # ── Main authorization ─────────────────────────────────────────────────

    def authorize_execution(self,
                            allocation:      Dict[str, Any],
                            account_state:   Dict[str, Any],
                            market_snapshot: Dict[str, Any],
                            memory_stats:    Dict[str, Any]) -> Dict[str, Any]:
        """
        Full execution authorization pipeline.
        Returns {"status": "APPROVED", "action": "EXECUTE_ORDER", ...}
        or {"status": "REJECTED_...", "action": "STAY_FLAT"}.
        """
        # ── Gate 1: Daily drawdown breaker ────────────────────────────────
        daily_loss = float(account_state.get("daily_realized_loss_pct", 0.0))
        if daily_loss >= MAX_DAILY_LOSS:
            log.warning(f"🛑 Daily loss breaker: {daily_loss:.2%} ≥ {MAX_DAILY_LOSS:.2%}")
            return {"status": "REJECTED_DRAWDOWN_BREAKER_ACTIVE",
                    "action": "HALT"}

        # ── Gate 2: Permitted direction ───────────────────────────────────
        direction = int(allocation.get("permitted_direction", 0))
        if direction == 0 or allocation.get("execution_profile") == "FLAT":
            return {"status": "SUCCESS_FLAT_MANDATED", "action": "STAY_FLAT"}

        # ── Gate 3: Transaction friction ──────────────────────────────────
        atr         = float(market_snapshot["atr_14"])
        spread_pts  = float(account_state.get("current_spread_points", 2.0))
        tick_size   = float(account_state.get("SYMBOL_TRADE_TICK_SIZE", 0.01))
        tp_atr      = float(allocation["tp_atr_target"])

        friction_pct = (spread_pts * tick_size) / (atr * tp_atr + 1e-10)
        if friction_pct > 0.15:
            log.warning(f"Friction {friction_pct:.1%} > 15% — skip")
            return {"status": "REJECTED_COST_FRICTION_EXCEEDED",
                    "action": "STAY_FLAT"}

        # ── Gate 4: Kill hours ────────────────────────────────────────────
        from datetime import datetime, timezone
        from config import KILL_HOURS_UTC
        hour = datetime.now(timezone.utc).hour
        if hour in KILL_HOURS_UTC:
            return {"status": "REJECTED_KILL_HOUR",
                    "action": "STAY_FLAT"}

        # ── Gate 5: Lot sizing ────────────────────────────────────────────
        tier          = self.get_risk_tier(memory_stats)
        base_risk     = tier["risk_pct"]
        aggr          = float(allocation.get("aggression_multiplier", 0.3))
        # P2: read risk context from account_state (explicit), not signal-layer mutation
        consecutive_l = int(account_state.get("consecutive_losses",
                            market_snapshot.get("trade_memory", {}).get("consecutive_losses", 0)))
        eqd           = float(account_state.get("execution_quality_degradation",
                              market_snapshot.get("trade_memory", {}).get("execution_quality_degradation", 0.0)))

        # Scale: base × aggression^1.5, halved per consecutive loss, EQD penalty
        scaled_risk = base_risk * (aggr ** 1.5)
        if consecutive_l > 0:
            scaled_risk *= (0.5 ** consecutive_l)
        scaled_risk *= (1.0 - eqd)
        scaled_risk = max(0.001, min(scaled_risk, MAX_RISK_PER_TRADE))

        balance      = float(account_state["balance"])
        cash_at_risk = balance * scaled_risk

        sl_atr        = float(allocation["sl_atr_target"])
        sl_dist_pts   = (atr * sl_atr) / tick_size
        tp_dist_pts   = (atr * tp_atr) / tick_size

        tick_value     = float(account_state.get("SYMBOL_TRADE_TICK_VALUE", 1.0))
        contract_size  = float(account_state.get("SYMBOL_TRADE_CONTRACT_SIZE", 1.0))
        current_price  = float(market_snapshot["price"])

        # BTC lot sizing: 1 lot = 1 BTC → value per point = contract_size * tick_value / tick_size
        raw_lots = cash_at_risk / (sl_dist_pts * tick_value + 1e-10)
        min_lot  = float(account_state.get("min_lot_limit", 0.01))
        max_lot  = float(account_state.get("max_lot_limit", 100.0))
        lots     = round(max(min_lot, min(raw_lots, max_lot)), 2)

        # ── Gate 6: Margin stress ─────────────────────────────────────────
        margin_init  = float(account_state.get("SYMBOL_MARGIN_INITIAL", 0.0))
        leverage     = float(account_state.get("leverage", 30.0))
        free_margin  = float(account_state.get("free_margin", balance))

        if margin_init > 0:
            req_margin = lots * margin_init
        else:
            req_margin = (lots * contract_size * current_price) / leverage

        if req_margin > free_margin * 0.70:
            log.warning(f"Margin stress: req={req_margin:.0f} > 70% free={free_margin:.0f}")
            return {"status": "REJECTED_MARGIN_STRESS",
                    "action": "STAY_FLAT"}

        # ── Approved — build order parameters ─────────────────────────────
        virtual_sl = int(self._offset(sl_dist_pts, spread_pts, is_tp=False))
        virtual_tp = int(self._offset(tp_dist_pts, spread_pts, is_tp=True))

        return {
            "status":         "APPROVED",
            "action":         "EXECUTE_ORDER",
            "snapshot_hash":  allocation.get("snapshot_hash", ""),
            "risk_tier":      self.risk_tiers.index(tier) + 1,
            "risk_pct":       round(scaled_risk, 5),
            "cash_at_risk":   round(cash_at_risk, 2),
            "order_parameters": {
                "symbol":           market_snapshot["symbol"],
                "type":             "BUY" if direction == 1 else "SELL",
                "volume":           lots,
                "execution_price":  current_price,
                "stealth_mode":     True,     # SL/TP zeroed on broker side
                "virtual_sl_points": virtual_sl,
                "virtual_tp_points": virtual_tp,
            },
        }

    # ── Round-number sweep offset ──────────────────────────────────────────

    def _offset(self, base_pts: float, spread_pts: float,
                is_tp: bool) -> int:
        """
        Offset SL/TP away from round numbers to avoid liquidity sweeps.
        TP: subtract small padding (just inside target)
        SL: add small padding (slightly wider than minimum)
        """
        padding = max(2, min(int(round(max(spread_pts * 0.5,
                                           base_pts * 0.01))), 15))
        return int(round(base_pts)) - padding if is_tp \
               else int(round(base_pts)) + padding
