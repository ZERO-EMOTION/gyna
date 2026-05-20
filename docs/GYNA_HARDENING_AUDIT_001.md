# GYNA Hardening Audit 001

Scope: first contribution to harden live-execution integrity without changing the alpha/entry logic.

Repository: `janpauldelacruz/gyna`
System inspected: `main_orchestrator.py`, `execution_telemetry.py`, `risk_engine.py`, `feature_engine.py`, `edge_engine.py`, `claude_allocator.py`, `state_manager.py`, `config.py`.

---

## Verdict

GYNA has a correct institutional architecture pattern:

```text
Closed-bar features -> deterministic edge gate -> constrained LLM allocator -> RiskEngine final authority -> stealth execution -> persistent state/memory/telemetry
```

The core separation is strong. The system should not be treated as live-ready until execution accounting is made broker-authoritative. The main current weakness is not the LLM. The weakness is execution measurement, PnL accounting, and risk context persistence.

---

## Priority 1 — Fix latency telemetry end-to-end

### Problem

`main_orchestrator.py` captures:

```python
t_sent_perf = time.perf_counter()
```

but the value is not passed into `ExecutionTelemetry.log_transaction()`.

`execution_telemetry.py` already supports the correct path:

```python
t_sent_perf = authorized_order.get("timestamp_sent_perf")
t_fill_perf = broker_receipt.get("timestamp_fill_perf")
```

but that path is currently unreachable because the orchestrator only passes wall-clock timestamps.

### Required patch

In `main_orchestrator.py`, replace the execution timing block with:

```python
t_sent_wall = time.time()
t_sent_perf = time.perf_counter()

result = mt5.order_send({...})

t_fill_perf = time.perf_counter()
t_fill_wall = time.time()
latency_ms = (t_fill_perf - t_sent_perf) * 1000.0
```

Then build the broker receipt as:

```python
broker_receipt = {
    "execution_successful": True,
    "ticket_id": ticket,
    "fill_price": fill_price,
    "timestamp_sent": t_sent_wall,
    "timestamp_fill": t_fill_wall,
    "timestamp_sent_perf": t_sent_perf,
    "timestamp_fill_perf": t_fill_perf,
    "tick_size": tick_sz,
}
```

And call telemetry as:

```python
tel_profile = self.telemetry.log_transaction(
    {**auth, "timestamp_sent": t_sent_wall, "timestamp_sent_perf": t_sent_perf},
    broker_receipt,
)
```

Finally, persist the same monotonic latency:

```python
self.state_db.buffer_telemetry_metric(
    latency_ms,
    slippage,
    self.current_eqd,
)
```

### Why this matters

Latency measurements must use monotonic time. Wall-clock time can jump due to NTP sync, OS clock correction, timezone updates, or manual time changes. EQD should not be trained on contaminated latency.

---

## Priority 2 — Make RiskEngine risk context explicit

### Problem

`RiskEngine.authorize_execution()` currently reads consecutive losses and EQD from:

```python
market_snapshot.get("trade_memory", {})
```

That works only because `EdgeEngine` mutates the feature snapshot and injects `trade_memory`. Risk law should not depend on signal-layer mutation.

### Required patch

In `main_orchestrator.py`, add explicit risk fields to `account_state`:

```python
account_state = {
    "balance": account.balance,
    "equity": account.equity,
    "free_margin": account.margin_free,
    "leverage": account.leverage,
    "daily_realized_loss_pct": self.daily_loss_pct,
    "consecutive_losses": self.consecutive_losses,
    "execution_quality_degradation": self.current_eqd,
    ...
}
```

In `risk_engine.py`, replace:

```python
consecutive_l = int(market_snapshot.get("trade_memory", {}).get("consecutive_losses", 0))
eqd = float(market_snapshot.get("trade_memory", {}).get("execution_quality_degradation", 0.0))
```

with:

```python
consecutive_l = int(account_state.get("consecutive_losses", 0))
eqd = float(account_state.get("execution_quality_degradation", 0.0))
```

Keep the `market_snapshot["trade_memory"]` copy only for allocator context, not risk law.

---

## Priority 3 — Replace synthetic PnL with broker-authoritative PnL

### Problem

Stealth close memory currently uses:

```python
pnl_usd = pnl_pts * float(pos["volume"])
```

This is not valid for BTCUSD or broker-specific CFDs. It ignores:

- tick size
- tick value
- contract size
- commission
- swap
- spread paid on exit
- broker profit currency conversion

This corrupts TradeMemory stats, which then corrupts risk-tier promotion/demotion.

### Required direction

After a close, query MT5 history deals for the closed position/ticket and compute:

```text
realized_pnl = sum(deal.profit + deal.commission + deal.swap + deal.fee)
```

Then memory should log broker-authoritative realized PnL, not synthetic point-based PnL.

### Minimal safe helper

```python
def _realized_pnl_for_position(self, position_id: int, lookback_days: int = 7) -> float:
    from datetime import datetime, timedelta, timezone
    utc_to = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=lookback_days)
    deals = mt5.history_deals_get(utc_from, utc_to)
    if deals is None:
        return 0.0
    total = 0.0
    for d in deals:
        if int(getattr(d, "position_id", -1)) == int(position_id):
            total += float(getattr(d, "profit", 0.0))
            total += float(getattr(d, "commission", 0.0))
            total += float(getattr(d, "swap", 0.0))
            total += float(getattr(d, "fee", 0.0))
    return total
```

Use this after confirmed close before updating TradeMemory.

---

## Priority 4 — Rebuild daily loss from MT5 history

### Problem

The daily drawdown breaker uses persisted `daily_realized_loss_pct`. That is fragile after:

- restart
- crash
- manual close
- broker-side close
- VPS interruption
- missed state transition

### Required direction

On startup and before each allocation cycle, compute today’s realized PnL from MT5 deal history and compare to starting-day equity or balance.

Recommended fields:

```python
self.day_anchor_date
self.day_start_equity
self.daily_realized_pnl
self.daily_realized_loss_pct
```

The breaker should halt when:

```python
realized_loss_pct >= MAX_DAILY_LOSS
```

A stricter version should also include floating equity loss:

```python
floating_dd_pct = max(0.0, (day_start_equity - account.equity) / day_start_equity)
```

For live safety, use both:

```python
if realized_loss_pct >= MAX_DAILY_LOSS or floating_dd_pct >= MAX_DAILY_LOSS:
    halt
```

---

## Priority 5 — Add broker emergency SL option

GYNA uses stealth SL/TP. That is fine, but live systems need a disaster fallback if Python/MT5 bridge freezes.

Recommended input/config:

```python
USE_BROKER_EMERGENCY_SL = True
EMERGENCY_SL_MULTIPLIER = 1.50
```

Execution behavior:

- Broker SL is wider than virtual SL.
- Virtual SL remains the primary exit.
- Broker SL exists only for terminal crash, Python crash, VPS freeze, or network interruption.

Example:

```python
broker_sl_points = int(params["virtual_sl_points"] * EMERGENCY_SL_MULTIPLIER)
```

Then set broker SL price on entry while leaving TP virtual.

---

## Priority 6 — Fix timestamp semantics

Current code uses mixed timestamp semantics:

- `time.time()` for lifecycle timestamps
- `time.perf_counter()` for loop cadence
- MT5 tick `time_msc` for broker feed chronology

Correct hierarchy:

```text
time.perf_counter()  -> latency deltas only
tick.time_msc        -> broker chronology / feed staleness
time.time()          -> logs, DB event time, wall-clock audit joins
```

Do not use `time.time()` for execution-latency deltas.

---

## Priority 7 — Add test harness before live

Create a minimal offline test suite:

```text
tests/
  test_execution_telemetry.py
  test_risk_engine.py
  test_feature_no_lookahead.py
  test_edge_direction_mask.py
```

Required tests:

1. `ExecutionTelemetry` uses perf timestamps when present.
2. EQD increases with adverse slippage and latency.
3. RiskEngine rejects when daily loss exceeds cap.
4. RiskEngine reduces risk after consecutive losses.
5. FeatureEngine uses the last closed bar, not the forming bar.
6. EdgeEngine returns FLAT after 3 consecutive losses.
7. LLM allocator cannot exceed edge_quality_score.

---

## Recommended next commit sequence

1. `fix: use monotonic latency for execution telemetry`
2. `fix: pass explicit risk context into risk engine`
3. `fix: compute realized pnl from mt5 deal history`
4. `feat: rebuild daily loss from broker history`
5. `feat: add optional emergency broker stop`
6. `test: add telemetry and risk engine unit tests`

---

## Live readiness grade

Current grade: **B- architecture, C+ live execution safety**

After priorities 1-4: **B+ live execution safety**

After priorities 1-7 with forward test logs: **A- candidate for small-size demo/live pilot**

---

## Do not change yet

Do not change these until execution accounting is clean:

- edge rules
- regime thresholds
- LLM prompt philosophy
- risk tiers
- SL/TP ATR envelopes
- cooldown logic

Reason: if telemetry/PnL is wrong, any strategy optimization is contaminated.
