"""
Gyna — execution_telemetry.py
Rolling calculation of execution latency, structural slippage tracking,
and EQD (Execution Quality Degradation) coefficient.

EQD feeds back into risk_engine.py to reduce lot sizing
when execution quality degrades (high slippage or latency = smaller risk).

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

log = logging.getLogger("Gyna.ExecutionTelemetry")


class ExecutionTelemetry:
    def __init__(self, tracking_window: int = 20):
        self.window          = tracking_window
        self.latency_ms:     List[float] = []
        self.slippage_pts:   List[float] = []
        self.rejection_count: int = 0

    def log_transaction(self,
                        authorized_order: Dict[str, Any],
                        broker_receipt:   Dict[str, Any]) -> Dict[str, Any]:
        """
        Log fill quality metrics after each execution.
        Returns metrics dict including EQD coefficient.
        """
        if not broker_receipt.get("execution_successful", False):
            self.rejection_count += 1
            log.warning(f"Execution rejection #{self.rejection_count}")
            return {"status": "LOG_PROFILED_REJECTION",
                    "metrics": self._current_metrics()}

        # Latency
        # Use perf_counter delta if available (sub-second precision)
        # Fall back to wall-clock only for chronology, not latency
        t_sent_perf = authorized_order.get("timestamp_sent_perf")
        t_fill_perf = broker_receipt.get("timestamp_fill_perf")
        if t_sent_perf is not None and t_fill_perf is not None:
            latency = (t_fill_perf - t_sent_perf) * 1000.0  # ms — perf_counter precision
        else:
            t_sent = authorized_order.get("timestamp_sent", time.time())
            t_fill = broker_receipt.get("timestamp_fill", time.time())
            latency = (t_fill - t_sent) * 1000.0  # ms — wall-clock fallback

        # Slippage in points (positive = adverse)
        req_price  = float(authorized_order["order_parameters"]["execution_price"])
        fill_price = float(broker_receipt["fill_price"])
        tick_size  = float(broker_receipt.get("tick_size", 0.01))
        order_type = authorized_order["order_parameters"]["type"]

        if order_type == "BUY":
            slip_pts = (fill_price - req_price) / tick_size   # positive = paid more
        else:
            slip_pts = (req_price - fill_price) / tick_size   # positive = received less

        # Rolling window
        self.latency_ms.append(latency)
        self.slippage_pts.append(slip_pts)
        if len(self.latency_ms)   > self.window: self.latency_ms.pop(0)
        if len(self.slippage_pts) > self.window: self.slippage_pts.pop(0)

        metrics = self._current_metrics()
        metrics.update({
            "instant_latency_ms":      round(latency, 1),
            "instant_slippage_points": round(slip_pts, 1),
        })

        log.info(f"Execution: lat={latency:.0f}ms slip={slip_pts:+.1f}pts "
                 f"eqd={metrics['execution_quality_degradation']:.3f}")

        return {"status": "PROFILED", "metrics": metrics}

    def current_metrics(self) -> Dict[str, Any]:
        """Public snapshot of rolling execution-quality metrics."""
        return self._current_metrics()

    def _current_metrics(self) -> Dict[str, Any]:
        import numpy as np

        lats = self.latency_ms   if self.latency_ms   else [0.0]
        slips = self.slippage_pts if self.slippage_pts else [0.0]

        avg_lat      = float(np.mean(lats))
        avg_slip_raw = float(np.mean(slips))          # signed — preserves asymmetry info
        avg_slip_adv = float(np.mean([max(0.0, s) for s in slips]))  # adverse only for EQD

        # EQD: linear now, piecewise/exponential after telemetry calibration
        # Slippage: adverse only (0–0.5 at 5pt threshold)
        # Latency:  (0–0.5 at 250ms threshold)
        slip_component = min(0.5, avg_slip_adv / 5.0 * 0.5)
        lat_component  = min(0.5, avg_lat / 250.0 * 0.5)
        eqd            = round(slip_component + lat_component, 3)

        # P95 latency for tail-risk awareness (available once window fills)
        p95_lat = round(float(np.percentile(lats, 95)), 1) if len(lats) >= 5 else avg_lat

        return {
            "rolling_avg_latency_ms":        round(avg_lat, 1),
            "p95_latency_ms":                p95_lat,
            "rolling_avg_slippage_points":   round(avg_slip_adv, 1),
            "raw_signed_slippage_points":    round(avg_slip_raw, 3),  # signed distribution
            "execution_quality_degradation": eqd,
        }
