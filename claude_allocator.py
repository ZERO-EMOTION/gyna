"""
Gyna — claude_allocator.py
Multi-provider LLM allocator with automatic fallback chain.

Provider priority (set via LLM_PROVIDER in .env):
  groq      → Groq Llama 3.3 70B (free tier, fast, proven in fleet)
  anthropic → Claude Sonnet (best quality, paid)
  local     → Conservative deterministic fallback (no API needed)

Architecture contract:
  Claude/Groq role = ALLOCATION only (not direction, not alpha)
  EdgeEngine has already set permitted_direction — cannot be reversed
  RiskEngine enforces final hard bounds

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger("Gyna.Allocator")

# ── System prompt (same for all providers) ────────────────────────────────
SYSTEM_PROMPT = """You are the risk allocation engine of Gyna, an autonomous BTCUSD M1 scalping system.

YOUR ROLE IS ALLOCATION NOT DIRECTION.
The primary signal layer has already computed permitted_direction. You cannot change it.

BTCUSD M1 EXECUTION PHILOSOPHY:
- Never use fixed pip stops. Always ATR-relative and structure-relative.
- BTC noise is too aggressive for tight stops. Death by noise = overtrading.
- Edge comes from asymmetry (larger wins), not ultra-high win rate.
- Target RR: 1.5R minimum. Preferred 1.5R to 2.0R.
- SL buffer: 0.50 x ATR14 beyond structure.
- Fewer trades, higher quality. Patience is an edge.

HARD CONSTRAINTS (violations trigger local fallback):
1. permitted_direction == 0 means FLAT, aggression_multiplier 0.0
2. aggression_multiplier MUST be <= edge_quality_score
3. sl_atr_target MUST be within allowed_sl_atr_range (inclusive)
4. tp_atr_target MUST be within allowed_tp_atr_range (inclusive)
5. regime_fatigue_factor > 0.5 means CONSERVATIVE, reduce aggression
6. consecutive_losses >= 2 means CONSERVATIVE
7. regime == RANGE means CONSERVATIVE or FLAT (BTC range trades are noisy)
8. session == ASIA means CONSERVATIVE (lower liquidity)

SIZING GUIDANCE:
- TREND + London/NY + strong edge: AGGRESSIVE (0.7-1.0x)
- VOLATILE regime: CONSERVATIVE (0.3-0.5x), wider SL
- RANGE regime: CONSERVATIVE (0.2-0.4x) or FLAT
- Consecutive losses: step down aggression

RESPOND ONLY with a single JSON object, no markdown, no preamble:
{
  "execution_profile": "AGGRESSIVE|CONSERVATIVE|FLAT",
  "aggression_multiplier": <float 0.0 to edge_quality_score>,
  "sl_atr_target": <float within allowed_sl_atr_range>,
  "tp_atr_target": <float within allowed_tp_atr_range>,
  "allocation_rationale": "<one paragraph explaining regime, session, structure, sizing>"
}
"""


class GynAllocator:
    """
    Multi-provider LLM allocator.
    Auto-detects provider from LLM_PROVIDER env var or API key prefix.
    Falls back through: groq → anthropic → local
    """

    def __init__(self):
        self.provider     = os.getenv("LLM_PROVIDER", "ollama").lower()
        self.groq_key     = os.getenv("GROQ_API_KEY", "")
        self.claude_key   = os.getenv("ANTHROPIC_API_KEY", "")
        self.ollama_url   = os.getenv("OLLAMA_URL", "http://localhost:11434")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
        self.groq_model   = "llama-3.3-70b-versatile"
        self.claude_model = "claude-sonnet-4-20250514"
        log.info(f"Allocator: provider={self.provider} model={self.ollama_model if self.provider=='ollama' else ''}")

    def allocate_cycle(self,
                       masked_snapshot: Dict[str, Any],
                       recent_losses:   Optional[list] = None) -> Dict[str, Any]:
        """Main allocation call. Returns allocation dict."""

        # Fast-path: deterministic flat
        if masked_snapshot.get("permitted_direction", 0) == 0:
            return self._bypass("DETERMINISTIC_FLAT", masked_snapshot)
        # WEEKEND trading allowed for BTCUSD (24/7 asset)

        # Build payload
        payload = masked_snapshot.copy()
        if recent_losses:
            payload["recent_losses"] = recent_losses[-5:]  # last 5 only

        # Try provider chain
        result = None
        if self.provider == "ollama":
            result = self._call_ollama(payload, masked_snapshot)
        elif self.provider == "groq" and self.groq_key:
            result = self._call_groq(payload, masked_snapshot)
        elif self.provider == "anthropic" and self.claude_key:
            result = self._call_anthropic(payload, masked_snapshot)
        # Fallback chain
        if not result and self.groq_key:
            result = self._call_groq(payload, masked_snapshot)
        if not result and self.claude_key:
            result = self._call_anthropic(payload, masked_snapshot)
        if not result:
            result = self._local_fallback(masked_snapshot)

        return result

    # ── Groq ───────────────────────────────────────────────────────────────

    def _call_groq(self, payload: Dict, snap: Dict) -> Optional[Dict]:
        if not self.groq_key:
            return None
        try:
            import urllib.request
            body = json.dumps({
                "model":       self.groq_model,
                "messages":    [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": f"Compute allocation:\n{json.dumps(payload, indent=2, default=str)}"}
                ],
                "temperature": 0.0,
                "max_tokens":  400,
            }).encode()

            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {self.groq_key}",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            raw = data["choices"][0]["message"]["content"].strip()
            log.info(f"Groq response received ({len(raw)} chars)")
            return self._parse_and_verify(raw, snap, source="groq")

        except Exception as e:
            log.warning(f"Groq failed: {e}")
            return None

    # ── Ollama (local) ────────────────────────────────────────────────────

    def _call_ollama(self, payload: Dict, snap: Dict) -> Optional[Dict]:
        try:
            import urllib.request
            body = json.dumps({
                "model":  self.ollama_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": f"Compute allocation:\n{json.dumps(payload, indent=2, default=str)}"}
                ],
                "stream": False,
                "options": {"temperature": 0.0}
            }).encode()

            req = urllib.request.Request(
                f"{self.ollama_url}/api/chat",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())

            raw = data.get("message", {}).get("content", "").strip()
            log.info(f"Ollama response received ({len(raw)} chars)")
            return self._parse_and_verify(raw, snap, source="ollama")

        except Exception as e:
            log.warning(f"Ollama failed: {e}")
            return None

    # ── Anthropic ──────────────────────────────────────────────────────────

    def _call_anthropic(self, payload: Dict, snap: Dict) -> Optional[Dict]:
        if not self.claude_key:
            return None
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=self.claude_key)
            response = client.messages.create(
                model=self.claude_model,
                max_tokens=400,
                temperature=0.0,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Compute allocation:\n{json.dumps(payload, indent=2, default=str)}"
                }]
            )
            raw = response.content[0].text.strip()
            log.info(f"Anthropic response received ({len(raw)} chars)")
            return self._parse_and_verify(raw, snap, source="anthropic")

        except Exception as e:
            log.warning(f"Anthropic failed: {e}")
            return None

    # ── Response parser ────────────────────────────────────────────────────

    def _parse_and_verify(self, raw: str, snap: Dict,
                          source: str = "llm") -> Optional[Dict]:
        try:
            # Strip markdown fences
            if "```" in raw:
                raw = raw.split("```json")[-1].split("```")[0].strip()
            if not raw.startswith("{"):
                raw = "{" + raw.split("{", 1)[-1]

            parsed = json.loads(raw)
            sl_b     = snap["allowed_sl_atr_range"]
            tp_b     = snap["allowed_tp_atr_range"]
            edge_cap = float(snap.get("edge_quality_score", 0.5))

            # Hard bounds validation
            assert parsed["execution_profile"] in ("AGGRESSIVE", "CONSERVATIVE", "FLAT")
            assert 0.0 <= float(parsed["aggression_multiplier"]) <= edge_cap + 1e-6
            assert sl_b[0] <= float(parsed["sl_atr_target"]) <= sl_b[1]
            assert tp_b[0] <= float(parsed["tp_atr_target"]) <= tp_b[1]

            return {
                "status":                "SUCCESS",
                "source":                source,
                "snapshot_hash":         snap.get("snapshot_hash", ""),
                "permitted_direction":   snap["permitted_direction"],
                "execution_profile":     parsed["execution_profile"],
                "aggression_multiplier": round(float(parsed["aggression_multiplier"]), 3),
                "sl_atr_target":         round(float(parsed["sl_atr_target"]), 2),
                "tp_atr_target":         round(float(parsed["tp_atr_target"]), 2),
                "allocation_rationale":  str(parsed.get("allocation_rationale", "")),
            }

        except Exception as e:
            log.warning(f"Parse/verify failed ({source}): {e}")
            return None

    # ── Local autonomous fallback ──────────────────────────────────────────

    def _local_fallback(self, snap: Dict) -> Dict:
        edge = float(snap.get("edge_quality_score", 0.3))
        sl_b = snap.get("allowed_sl_atr_range", [1.0, 2.0])
        tp_b = snap.get("allowed_tp_atr_range", [2.0, 4.0])
        sl_val = round(sl_b[0] + (sl_b[1] - sl_b[0]) * 0.8, 2)
        tp_val = round(tp_b[0] + (tp_b[1] - tp_b[0]) * 0.4, 2)
        log.warning("Using local autonomous fallback")
        return {
            "status":                "SUCCESS_LOCAL_FALLBACK",
            "source":                "local",
            "snapshot_hash":         snap.get("snapshot_hash", ""),
            "permitted_direction":   snap.get("permitted_direction", 0),
            "execution_profile":     "CONSERVATIVE",
            "aggression_multiplier": round(edge * 0.5, 3),
            "sl_atr_target":         sl_val,
            "tp_atr_target":         tp_val,
            "allocation_rationale":  "Local autonomous fallback — all APIs unavailable.",
        }

    def _bypass(self, condition: str, snap: Dict) -> Dict:
        sl_b = snap.get("allowed_sl_atr_range", [1.0, 2.0])
        tp_b = snap.get("allowed_tp_atr_range", [2.0, 4.0])
        return {
            "status":                f"BYPASS_{condition}",
            "source":                "bypass",
            "snapshot_hash":         snap.get("snapshot_hash", ""),
            "permitted_direction":   0,
            "execution_profile":     "FLAT",
            "aggression_multiplier": 0.0,
            "sl_atr_target":         sl_b[0],
            "tp_atr_target":         tp_b[0],
            "allocation_rationale":  f"Bypass: {condition}",
        }


# Backward compatible alias
ClaudeAllocator = GynAllocator
