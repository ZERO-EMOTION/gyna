"""
Gyna — reflection_engine.py
Weekly self-review: Gyna reads its own trade history, asks the LLM what it
should learn, and stores the lesson permanently. Recent reflections are then
injected into every allocation prompt — the loop that makes memory compound.

Schedule: REFLECTION_DAY / REFLECTION_HOUR from config (default Sunday 00:00
UTC). Runs at most once per week; skips silently when there is nothing to
review or every LLM provider is down (retries next cycle).

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from config import REFLECTION_DAY, REFLECTION_HOUR, SYMBOL

log = logging.getLogger("Gyna.Reflection")

REFLECTION_SYSTEM_PROMPT = f"""You are the weekly self-review module of Gyna, an autonomous {SYMBOL} M1 trading system that learns from its own trade history.

You will receive the week's closed trades and aggregate stats. Write a reflection that a risk-allocation engine can act on next week. Be specific and quantitative, never generic.

The system trades TWO styles: "scalper" (price-action bursts/sweeps, quick
~1.5R) and "runner" (trend rides, 3R+ targets). Compare them.

Cover, in order:
1. What worked: styles/regimes/sessions/setups with positive expectancy this week.
2. What failed: recurring losing patterns (style, state, session, hour, overtrading after losses).
3. Which style earned its risk this week and which did not — with numbers.
4. One concrete behavioral adjustment for next week, phrased as a rule
   (e.g. "reduce scalper aggression to <=0.3 during ASIA").
5. Anything the win-rate/PF trend implies about the current risk tier.

Maximum 250 words. Plain text, no markdown headers."""


class ReflectionEngine:
    def __init__(self, memory, allocator):
        self.memory    = memory
        self.allocator = allocator

    # ── Scheduling ─────────────────────────────────────────────────────────

    def due(self, now: Optional[datetime] = None) -> bool:
        """True when the weekly reflection window is open and none exists yet."""
        now = now or datetime.now(timezone.utc)
        if now.weekday() != REFLECTION_DAY or now.hour < REFLECTION_HOUR:
            return False
        last = self.memory.get_recent_reflections(1)
        if last:
            last_ts = datetime.fromisoformat(last[0]["timestamp"])
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if now - last_ts < timedelta(days=6):
                return False
        return True

    def maybe_run(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Run the reflection if due. Returns the stored record or None."""
        if not self.due(now):
            return None
        return self.reflect(now)

    # ── The review itself ──────────────────────────────────────────────────

    def reflect(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        now   = now or datetime.now(timezone.utc)
        start = (now - timedelta(days=7)).isoformat()
        end   = now.isoformat()
        trades = self.memory.get_trades_for_period(start, end)
        if not trades:
            log.info("Reflection skipped — no closed trades this week")
            return None

        wins   = [t for t in trades if t["outcome"] == "win"]
        losses = [t for t in trades if t["outcome"] == "loss"]
        gp = sum(t["pnl_usd"] for t in trades if (t["pnl_usd"] or 0) > 0)
        gl = sum(abs(t["pnl_usd"]) for t in trades if (t["pnl_usd"] or 0) < 0)
        wr = len(wins) / len(trades) if trades else 0.0
        pf = gp / gl if gl > 0 else float("inf")

        lines = [
            f"WEEK {start[:10]} to {end[:10]}",
            f"Trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}",
            f"Win rate: {wr:.1%} | Profit factor: {pf:.2f} | "
            f"Net PnL: ${gp - gl:+.2f}",
            "",
            "TRADES (time | style | dir | regime | session | pnl | entry rationale):",
        ]
        for t in trades[-40:]:   # cap prompt size
            lines.append(
                f"{str(t['timestamp'])[:16]} | {t.get('style') or '?'} | "
                f"{t['direction']} | "
                f"{t.get('regime')} | {t.get('session')} | "
                f"${(t['pnl_usd'] or 0):+.2f} | "
                f"{str(t.get('rationale') or '')[:100]}")

        content = self.allocator.complete(
            REFLECTION_SYSTEM_PROMPT, "\n".join(lines))
        if not content:
            log.warning("Reflection failed — all LLM providers unavailable, "
                        "will retry next cycle")
            return None

        record = {
            "period_start":  start,
            "period_end":    end,
            "n_trades":      len(trades),
            "win_rate":      round(wr, 4),
            "profit_factor": round(min(pf, 999.0), 4),
            "risk_tier":     None,
            "content":       content,
        }
        self.memory.log_reflection(record)
        log.info(f"Reflection stored ({len(trades)} trades reviewed)")
        return record

    # ── Prompt injection helper ────────────────────────────────────────────

    def recent_lessons(self, n: int = 2) -> list:
        """Reflection texts for injection into the allocation prompt."""
        return [r["content"] for r in self.memory.get_recent_reflections(n)]
