"""
Gyna — claude_allocator.py
Claude's role: ALLOCATION only — not direction, not alpha generation.
The EdgeEngine has already computed permitted_direction and edge_quality_score.
Claude selects sl_atr_target and tp_atr_target within the risk envelope,
and sets aggression_multiplier ≤ edge_quality_score.

On any API failure → local autonomous fallback (conservative, no FLAT).

AURELIA EMPIRE | ZEROEMOTIONS | CLAUDE inside™
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger("Gyna.ClaudeAllocator")

# ── System prompt (hardened) ───────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are the risk allocation engine of Gyna, an autonomous BTCUSD trading system.

YOUR ROLE IS ALLOCATION — NOT DIRECTION.
The primary signal layer has already computed:
  - permitted_direction: 1=BUY, -1=SELL (you cannot reverse this)
  - edge_quality_score:  your aggression_multiplier ceiling
  - allowed_sl_atr_range / allowed_tp_atr_range: hard bounds (you must stay inside)

DECISION INPUTS YOU MUST USE:
1. regime + regime_confidence + regime_duration + transition_frequency
2. All technical indicators (RSI, MACD, ATR, BB, HMA, structure)
3. trade_memory: consecutive losses, drawdown, EQD
4. regime_fatigue_factor: if high (>0.5), reduce aggression
5. session: WEEKEND = no trade regardless of signal
6. Similar past trades from memory (if provided)
7. Recent losses from memory (if provided)

HARD CONSTRAINTS (violations are rejected, fallback fires):
1. If permitted_direction == 0 → return execution_profile FLAT, aggression_multiplier 0.0
2. aggression_multiplier MUST be ≤ edge_quality_score
3. sl_atr_target MUST be within allowed_sl_atr_range (inclusive)
4. tp_atr_target MUST be within allowed_tp_atr_range (inclusive)
5. If session == WEEKEND → execution_profile FLAT

ALLOCATION PROFILES:
  AGGRESSIVE:   aggression_multiplier = 0.7–1.0× edge_quality_score
  CONSERVATIVE: aggression_multiplier = 0.3–0.6× edge_quality_score
  FLAT:         aggression_multiplier = 0.0 (no trade)

RESPOND ONLY with a single JSON object. No markdown, no preamble:
{
  "execution_profile": "AGGRESSIVE|CONSERVATIVE|FLAT",
  "aggression_multiplier": <float 0.0–edge_quality_score>,
  "sl_atr_target": <float within allowed_sl_atr_range>,
  "tp_atr_target": <float within allowed_tp_atr_range>,
  "allocation_rationale": "<one paragraph — what in the data drove this decision>"
}
"""


class ClaudeAllocator:
    def __init__(self, api_key: Optional[str] = None,
                 model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY missing from environment")
        self.model = model

        # Import lazily so module loads without anthropic installed during tests
        from anthropic import Anthropic
        self.client = Anthropic(api_key=self.api_key)

    def allocate_cycle(self,
                       masked_snapshot: Dict[str, Any],
                       similar_trades: Optional[list] = None,
                       recent_losses: Optional[list] = None) -> Dict[str, Any]:
        """
        Main allocation call. Returns allocation dict.
        On any failure → local autonomous fallback (never crashes cycle).
        """
        # Fast-path: deterministic flat
        if masked_snapshot.get("permitted_direction", 0) == 0:
            return self._bypass("DETERMINISTIC_FLAT", masked_snapshot)

        if masked_snapshot.get("session") == "WEEKEND":
            return self._bypass("WEEKEND_HALT", masked_snapshot)

        # Build user message
        payload = masked_snapshot.copy()
        if similar_trades:
            payload["similar_past_trades"] = similar_trades
        if recent_losses:
            payload["recent_losses"] = recent_losses

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                temperature=0.0,        # deterministic allocation
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Compute allocation for this market state:\n\n"
                        f"{json.dumps(payload, indent=2, default=str)}"
                    )
                }]
            )
            raw = response.content[0].text.strip()
            return self._parse_and_verify(raw, masked_snapshot)

        except Exception as e:
            log.warning(f"Claude API error: {e} — activating local fallback")
            return self._local_autonomous_fallback(masked_snapshot)

    # ── Response parser + hard assertion bounds ────────────────────────────

    def _parse_and_verify(self, raw: str,
                          snap: Dict[str, Any]) -> Dict[str, Any]:
        try:
            # Strip markdown fences if present
            if "```" in raw:
                raw = raw.split("```json")[-1].split("```")[0].strip()
                if not raw.startswith("{"):
                    raw = raw.split("{", 1)[-1]
                    raw = "{" + raw

            parsed = json.loads(raw)

            sl_bounds = snap["allowed_sl_atr_range"]
            tp_bounds = snap["allowed_tp_atr_range"]
            edge_cap  = float(snap.get("edge_quality_score", 0.5))

            # Hard assertion bounds (violations → fallback, not crash)
            assert parsed["execution_profile"] in ("AGGRESSIVE", "CONSERVATIVE", "FLAT"), \
                f"Invalid profile: {parsed['execution_profile']}"
            assert 0.0 <= float(parsed["aggression_multiplier"]) <= edge_cap + 1e-6, \
                f"Aggression {parsed['aggression_multiplier']} > edge cap {edge_cap}"
            assert sl_bounds[0] <= float(parsed["sl_atr_target"]) <= sl_bounds[1], \
                f"SL {parsed['sl_atr_target']} outside {sl_bounds}"
            assert tp_bounds[0] <= float(parsed["tp_atr_target"]) <= tp_bounds[1], \
                f"TP {parsed['tp_atr_target']} outside {tp_bounds}"

            return {
                "status":               "SUCCESS",
                "snapshot_hash":        snap.get("snapshot_hash", ""),
                "permitted_direction":  snap["permitted_direction"],
                "execution_profile":    parsed["execution_profile"],
                "aggression_multiplier": round(float(parsed["aggression_multiplier"]), 3),
                "sl_atr_target":        round(float(parsed["sl_atr_target"]), 2),
                "tp_atr_target":        round(float(parsed["tp_atr_target"]), 2),
                "allocation_rationale": str(parsed.get("allocation_rationale", "")),
            }

        except Exception as e:
            log.warning(f"Allocation parse/verify failed: {e} — fallback")
            return self._local_autonomous_fallback(snap)

    # ── Fallbacks ──────────────────────────────────────────────────────────

    def _local_autonomous_fallback(self,
                                   snap: Dict[str, Any]) -> Dict[str, Any]:
        """
        Conservative autonomous allocation when Claude API is unavailable.
        Uses edge_quality_score at 50% capacity. Never returns FLAT
        unless permitted_direction is already 0.
        """
        edge   = float(snap.get("edge_quality_score", 0.3))
        sl_b   = snap.get("allowed_sl_atr_range", [1.0, 2.0])
        tp_b   = snap.get("allowed_tp_atr_range", [2.0, 4.0])
        # Conservative: SL at 80th percentile of range, TP at 40th
        sl_val = round(sl_b[0] + (sl_b[1] - sl_b[0]) * 0.8, 2)
        tp_val = round(tp_b[0] + (tp_b[1] - tp_b[0]) * 0.4, 2)

        return {
            "status":               "SUCCESS_LOCAL_AUTONOMOUS_FALLBACK",
            "snapshot_hash":        snap.get("snapshot_hash", ""),
            "permitted_direction":  snap.get("permitted_direction", 0),
            "execution_profile":    "CONSERVATIVE",
            "aggression_multiplier": round(edge * 0.5, 3),
            "sl_atr_target":        sl_val,
            "tp_atr_target":        tp_val,
            "allocation_rationale": "Local autonomous fallback: Claude API unavailable.",
        }

    def _bypass(self, condition: str,
                snap: Dict[str, Any]) -> Dict[str, Any]:
        sl_b = snap.get("allowed_sl_atr_range", [1.0, 2.0])
        tp_b = snap.get("allowed_tp_atr_range", [2.0, 4.0])
        return {
            "status":               f"BYPASS_{condition}",
            "snapshot_hash":        snap.get("snapshot_hash", ""),
            "permitted_direction":  0,
            "execution_profile":    "FLAT",
            "aggression_multiplier": 0.0,
            "sl_atr_target":        sl_b[0],
            "tp_atr_target":        tp_b[0],
            "allocation_rationale": f"Bypass: {condition}",
        }
