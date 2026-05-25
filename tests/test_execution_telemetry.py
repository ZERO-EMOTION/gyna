"""
tests/test_execution_telemetry.py
Audit priority 7 — tests for ExecutionTelemetry (P1 + EQD behaviour).
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time
import pytest
from execution_telemetry import ExecutionTelemetry


def _make_order(direction: str = "BUY", price: float = 50000.0,
                t_sent: float = None, t_sent_perf: float = None) -> dict:
    return {
        "status": "APPROVED",
        "order_parameters": {
            "type": direction,
            "execution_price": price,
            "volume": 0.01,
            "virtual_sl_points": 100,
            "virtual_tp_points": 200,
        },
        "timestamp_sent":      t_sent      or time.time(),
        "timestamp_sent_perf": t_sent_perf or time.perf_counter(),
    }


def _make_receipt(fill_price: float, tick_sz: float = 0.01,
                  t_sent_perf: float = None, t_fill_perf: float = None,
                  t_sent: float = None, t_fill: float = None) -> dict:
    now_perf = time.perf_counter()
    now_wall = time.time()
    return {
        "execution_successful": True,
        "ticket_id":            12345,
        "fill_price":           fill_price,
        "tick_size":            tick_sz,
        "timestamp_sent":       t_sent      or now_wall - 0.050,
        "timestamp_fill":       t_fill      or now_wall,
        "timestamp_sent_perf":  t_sent_perf or now_perf - 0.050,
        "timestamp_fill_perf":  t_fill_perf or now_perf,
    }


# ── 1. perf_counter path fires when timestamps present ────────────────────

def test_uses_perf_counter_when_available():
    tel = ExecutionTelemetry(tracking_window=10)
    t_sent_perf = time.perf_counter()
    time.sleep(0.02)          # 20ms simulated network
    t_fill_perf = time.perf_counter()

    order   = _make_order(t_sent_perf=t_sent_perf)
    receipt = _make_receipt(50001.0, t_sent_perf=t_sent_perf, t_fill_perf=t_fill_perf)

    result = tel.log_transaction(order, receipt)
    assert result["status"] == "PROFILED"
    lat = result["metrics"]["rolling_avg_latency_ms"]
    # Should be ~20ms, not wall-clock noise; accept 10–200ms range
    assert 10.0 <= lat <= 200.0, f"Unexpected latency {lat}"


# ── 2. EQD increases with adverse slippage ────────────────────────────────

def test_eqd_increases_with_adverse_slippage():
    tel = ExecutionTelemetry(tracking_window=20)

    # Baseline — zero slippage
    for _ in range(5):
        order   = _make_order("BUY", 50000.0)
        receipt = _make_receipt(50000.0)
        tel.log_transaction(order, receipt)

    baseline_eqd = tel._current_metrics()["execution_quality_degradation"]

    # Add significant adverse slippage (10 pts)
    for _ in range(5):
        order   = _make_order("BUY", 50000.0)
        receipt = _make_receipt(50010.0)   # 10 pts worse for BUY
        tel.log_transaction(order, receipt)

    slippage_eqd = tel._current_metrics()["execution_quality_degradation"]
    assert slippage_eqd > baseline_eqd, (
        f"EQD should increase with adverse slippage: {baseline_eqd} -> {slippage_eqd}")


# ── 3. EQD increases with high latency ────────────────────────────────────

def test_eqd_increases_with_high_latency():
    tel = ExecutionTelemetry(tracking_window=10)

    # Low-latency baseline
    t_perf = time.perf_counter()
    for _ in range(5):
        order   = _make_order(t_sent_perf=t_perf)
        receipt = _make_receipt(50000.0, t_sent_perf=t_perf, t_fill_perf=t_perf + 0.010)
        tel.log_transaction(order, receipt)

    baseline_eqd = tel._current_metrics()["execution_quality_degradation"]

    # High-latency fill (300ms)
    for _ in range(5):
        order   = _make_order(t_sent_perf=t_perf)
        receipt = _make_receipt(50000.0, t_sent_perf=t_perf, t_fill_perf=t_perf + 0.300)
        tel.log_transaction(order, receipt)

    high_lat_eqd = tel._current_metrics()["execution_quality_degradation"]
    assert high_lat_eqd > baseline_eqd, (
        f"EQD should rise with high latency: {baseline_eqd} -> {high_lat_eqd}")


# ── 4. Rejection increments counter but doesn't crash ─────────────────────

def test_rejection_increments_counter():
    tel = ExecutionTelemetry()
    order   = _make_order()
    receipt = {"execution_successful": False}

    result = tel.log_transaction(order, receipt)
    assert result["status"] == "LOG_PROFILED_REJECTION"
    assert tel.rejection_count == 1


# ── 5. EQD ceiling is 1.0 ─────────────────────────────────────────────────

def test_eqd_capped_at_one():
    tel = ExecutionTelemetry(tracking_window=5)
    t_perf = time.perf_counter()

    # Extreme slippage + extreme latency
    for _ in range(5):
        order   = _make_order("BUY", 50000.0, t_sent_perf=t_perf)
        receipt = _make_receipt(50100.0, t_sent_perf=t_perf, t_fill_perf=t_perf + 1.0)
        tel.log_transaction(order, receipt)

    eqd = tel._current_metrics()["execution_quality_degradation"]
    assert eqd <= 1.0, f"EQD exceeded 1.0: {eqd}"


# ── 6. P95 latency calculated after window fills ──────────────────────────

def test_p95_latency_after_window():
    tel = ExecutionTelemetry(tracking_window=10)
    t_perf = time.perf_counter()

    latencies = [10, 12, 11, 13, 250, 10, 11, 12, 11, 10]  # one spike
    for lat_ms in latencies:
        order   = _make_order(t_sent_perf=t_perf)
        receipt = _make_receipt(50000.0, t_sent_perf=t_perf,
                                t_fill_perf=t_perf + lat_ms / 1000.0)
        tel.log_transaction(order, receipt)

    metrics = tel._current_metrics()
    assert "p95_latency_ms" in metrics
    # P95 should be significantly above average due to the 250ms spike
    assert metrics["p95_latency_ms"] > metrics["rolling_avg_latency_ms"]
