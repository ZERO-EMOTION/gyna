"""
Gyna — tests/test_reflection_engine.py
Weekly self-review: scheduling, prompt content, storage, prompt injection.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

import pytest
from memory.trade_log import TradeMemory
from reflection_engine import ReflectionEngine
from config import REFLECTION_DAY, REFLECTION_HOUR


class FakeAllocator:
    """Captures the reflection prompt; returns a canned lesson."""
    def __init__(self, response="Reduce aggression in RANGE during ASIA."):
        self.response = response
        self.last_system = None
        self.last_user = None

    def complete(self, system, user, max_tokens=800):
        self.last_system, self.last_user = system, user
        return self.response


_seq = {"n": 5000}


def _sunday():
    """The next configured reflection day, late enough that trades logged
    'now' fall inside the reviewed 7-day window."""
    d = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    while d.weekday() != REFLECTION_DAY:
        d += timedelta(days=1)
    return d.replace(hour=max(23, REFLECTION_HOUR))


@pytest.fixture
def mem(tmp_path):
    return TradeMemory(str(tmp_path / "trades.db"))


def _closed_trade(mem, pnl, regime="TREND"):
    _seq["n"] += 1
    tid = mem.log_trade({
        "symbol": "BTCUSD", "direction": "BUY", "entry_price": 60000.0,
        "volume": 0.01, "regime": regime, "session": "NY",
        "rationale": "trend continuation", "mt5_ticket": _seq["n"],
        "outcome": "open",
    })
    mem.close_trade(tid, 60000.0 + pnl, pnl, pnl, "win" if pnl > 0 else "loss")
    return tid


def test_not_due_on_wrong_day(mem):
    engine = ReflectionEngine(mem, FakeAllocator())
    wrong_day = _sunday() + timedelta(days=1)
    assert engine.due(wrong_day) is False


def test_due_then_runs_once_per_week(mem):
    alloc = FakeAllocator()
    engine = ReflectionEngine(mem, alloc)
    _closed_trade(mem, 10.0)
    _closed_trade(mem, -5.0)

    now = _sunday()
    assert engine.due(now) is True
    record = engine.maybe_run(now)
    assert record is not None
    assert record["n_trades"] == 2
    assert "Reduce aggression" in record["content"]

    # Same window again — already reflected, must not run twice
    assert engine.due(now + timedelta(hours=2)) is False
    assert engine.maybe_run(now + timedelta(hours=2)) is None


def test_prompt_contains_stats_and_trades(mem):
    alloc = FakeAllocator()
    engine = ReflectionEngine(mem, alloc)
    _closed_trade(mem, 20.0)
    _closed_trade(mem, -10.0, regime="RANGE")
    engine.reflect(_sunday())
    assert "Win rate: 50.0%" in alloc.last_user
    assert "RANGE" in alloc.last_user
    assert "trend continuation" in alloc.last_user


def test_no_trades_skips_quietly(mem):
    engine = ReflectionEngine(mem, FakeAllocator())
    assert engine.reflect(_sunday()) is None
    assert mem.get_recent_reflections(1) == []


def test_llm_failure_stores_nothing_and_retries(mem):
    class DeadAllocator:
        def complete(self, *_a, **_k):
            return None
    engine = ReflectionEngine(mem, DeadAllocator())
    _closed_trade(mem, 10.0)
    now = _sunday()
    assert engine.reflect(now) is None
    assert mem.get_recent_reflections(1) == []
    assert engine.due(now) is True   # still due — will retry next cycle


def test_recent_lessons_feed_the_allocator(mem):
    engine = ReflectionEngine(mem, FakeAllocator("Lesson: avoid hour 21."))
    _closed_trade(mem, 10.0)
    engine.reflect(_sunday())
    lessons = engine.recent_lessons()
    assert lessons == ["Lesson: avoid hour 21."]
