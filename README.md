# PARALLAX — Gyna

**Persistent Intelligence Trading System for MetaTrader 5**  
*True Memory. Compounding AI. Never resets.*  
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.

---

## Architecture

```
MT5 BTCUSD M1 bars
  → FeatureEngine    (RSI, MACD, BB, HMA, HHLL, regime, session)
  → EdgeEngine       (directional mask — Claude cannot override)
  → ClaudeAllocator  (Anthropic / Groq / local fallback)
  → RiskEngine       (final authority — lot size, tier, daily halt)
  → Stealth Execution (virtual SL/TP in SQLite, none on broker)
  → TradeMemory      (every trade logged permanently)
  → EQD Telemetry    (execution quality degrades aggression)
```

**Key properties:**
- Claude is the *allocator*, not the signal — EdgeEngine controls direction
- Broker-authoritative PnL via MT5 deal history (not synthetic math)
- Crash-safe: rebuilds daily loss + position state from broker on every restart
- Risk tier auto-promotes based on real win rate + profit factor
- 20/20 unit tests passing (telemetry, risk engine, edge engine)

---

## Quick Start

```bash
git clone https://github.com/janpauldelacruz/gyna
cd gyna
pip install -r requirements.txt
cp .env.example .env
# Fill in MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, ANTHROPIC_API_KEY
python main.py
```

---

## Configuration

Edit  or set via :

| Key | Default | Description |
|-----|---------|-------------|
|  | — | ICMarkets account number |
|  | ICMarketsSC-Demo | Broker server |
|  | — | Claude API key |
|  | — | Optional Groq fallback |
|  | anthropic |  /  /  |
|  | 0.05 | 5% daily halt |
|  | 1 | BTCUSD concentration |

---

## File Map

| File | Role |
|------|------|
|  | Entry point |
|  | 50ms loop supervisor — full execution pipeline |
|  | All constants + risk tiers |
|  | Causal OHLCV → FeatureSnapshot (no repaint) |
|  | ADX + BB + Choppiness → TREND/RANGE/VOLATILE |
|  | Deterministic directional mask + quality score |
|  | Multi-provider LLM allocator (Anthropic/Groq/local) |
|  | Final authority — lot sizing, gates, tier promotion |
|  | Latency + slippage → EQD coefficient |
|  | SQLite WAL live position state + telemetry buffer |
|  | Post-session audit — regime/session/hash analysis |
|  | Thin MT5 connection wrapper |
|  | Permanent trade memory — never deletes |

---

## Risk Tiers (auto-promotes)

| Tier | Min Trades | Min WR | Min PF | Risk % |
|------|-----------|--------|--------|--------|
| 1 — Observation | 0 | 0% | 0.0 | 0.25% |
| 2 — Validated | 30 | 40% | 1.3 | 0.50% |
| 3 — Proven | 75 | 45% | 1.5 | 0.75% |
| 4 — Scaling | 150 | 50% | 1.8 | 1.00% |
| 5 — Full Power | 300 | 55% | 2.0 | 1.50% |

---

## Hardening Audit

 — All 7 priorities implemented.  
Live readiness grade: **B+ execution safety** → A- after forward test logs.
