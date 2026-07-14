"""
Gyna — tests/test_trade_log.py
TradeMemory: close-by-ticket, stats, orphan reconciliation.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from memory.trade_log import TradeMemory


@pytest.fixture
def mem(tmp_path):
    return TradeMemory(str(tmp_path / "trades.db"))


def _open_trade(mem, ticket=12345, direction="BUY"):
    return mem.log_trade({
        "symbol": "BTCUSD", "direction": direction,
        "entry_price": 60000.0, "volume": 0.01,
        "mt5_ticket": ticket, "outcome": "open",
    })


def test_close_trade_by_ticket_win(mem):
    _open_trade(mem, ticket=111)
    assert mem.close_trade_by_ticket(
        mt5_ticket=111, exit_price=60100.0,
        pnl_pips=100.0, pnl_usd=10.0, outcome="win") is True
    stats = mem.get_stats()
    assert stats["total_trades"] == 1
    assert stats["win_rate"] == 1.0
    assert stats["net_pnl"] == 10.0
    assert mem.get_open_tickets() == []


def test_close_trade_by_ticket_missing_returns_false(mem):
    assert mem.close_trade_by_ticket(
        mt5_ticket=999, exit_price=0.0,
        pnl_pips=0.0, pnl_usd=0.0, outcome="loss") is False
    # No phantom row was created and daily stats untouched
    assert mem.get_stats()["total_trades"] == 0
    assert mem.get_daily_pnl() == 0.0


def test_close_targets_most_recent_open_row(mem):
    _open_trade(mem, ticket=222)          # first trade on ticket 222
    mem.close_trade_by_ticket(222, 60050.0, 50.0, 5.0, "win")
    second_id = _open_trade(mem, ticket=222)  # ticket reused later
    assert mem.close_trade_by_ticket(222, 59950.0, -50.0, -5.0, "loss") is True
    rows = mem.get_trades_for_period("2000-01-01", "2100-01-01")
    assert len(rows) == 2
    closed_second = [r for r in rows if r["id"] == second_id][0]
    assert closed_second["outcome"] == "loss"


def test_orphan_reconciliation(mem):
    _open_trade(mem, ticket=333)
    assert mem.get_open_tickets() == [333]
    mem.mark_trade_orphaned(333)
    assert mem.get_open_tickets() == []
    # Orphaned trades must NOT pollute win-rate / PF tier stats
    assert mem.get_stats()["total_trades"] == 0


def test_daily_stats_updated_on_close(mem):
    _open_trade(mem, ticket=444)
    mem.close_trade_by_ticket(444, 60100.0, 100.0, 12.5, "win")
    assert mem.get_daily_pnl() == 12.5


def test_per_style_stats_feed_learned_arbitration(mem):
    ticket = 500
    for style, pnl in (("scalper", 10.0), ("scalper", -4.0),
                       ("runner", 30.0), ("runner", 25.0), ("runner", -10.0)):
        ticket += 1
        mem.log_trade({"symbol": "BTCUSD", "direction": "BUY",
                       "entry_price": 60000.0, "volume": 0.01,
                       "mt5_ticket": ticket, "style": style,
                       "outcome": "open"})
        mem.close_trade_by_ticket(ticket, 60000.0 + pnl, pnl, pnl,
                                  "win" if pnl > 0 else "loss")
    stats = mem.get_style_stats()
    assert stats["scalper"]["total_trades"] == 2
    assert stats["runner"]["total_trades"] == 3
    assert stats["runner"]["profit_factor"] == pytest.approx(5.5)
    assert stats["scalper"]["profit_factor"] == pytest.approx(2.5)
