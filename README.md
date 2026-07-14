# PARALLAX — Gyna

**Persistent Intelligence Trading System for MetaTrader 5**
*True Memory. Compounding AI. Never resets.*
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.

---

## Architecture

```
MT5 BTCUSD M1 bars
  → FeatureEngine    (RSI, MACD, BB, HMA, HHLL, regime, session)
  → EdgeEngine       (directional mask — the LLM cannot override)
  → ClaudeAllocator  (Anthropic / Groq / Ollama / local fallback)
  → RiskEngine       (final authority — lot size, tier, daily halt)
  → Stealth Execution (virtual SL/TP in SQLite; broker gets only a wide emergency SL)
  → TradeMemory      (every trade logged permanently)
  → EQD Telemetry    (execution quality degrades aggression)
  → Post-Trade Audit (closed-trades ledger → toxic-state blocklist, kill hours)
```

**Key properties:**
- Claude is the *allocator*, not the signal — EdgeEngine controls direction
- Broker-authoritative PnL via MT5 deal history (waits for the closing deal; synthetic tick-value fallback)
- Crash-safe: rebuilds daily loss + position state from broker on every restart; reconciles orphaned trade-memory rows
- Crash-proof loop: a failing frame is logged and retried, never kills the process while positions are open
- Risk tier auto-promotes based on real win rate + profit factor
- Toxic-state blocklist matches on a **quantized state signature** (recurring across bars), not the per-bar snapshot hash
- 48/48 unit tests passing

**How it learns (the loops that make it Gyna):**
1. **Recent losses** are injected into every allocation prompt
2. **Risk tiers** are earned from real WR/PF, never configured
3. **Toxic-state blocklist** — recurring losing market states get a 50% aggression penalty, refreshed daily in-session
4. **EQD** — degrading fill quality automatically shrinks size
5. **Empirical kill hours** — hours with ≥10 trades and net-negative PnL are blocked automatically (max 4, so noise can never kill the whole day)
6. **Weekly self-reflection** — every Sunday 00:00 UTC Gyna reviews its week with the LLM, stores the lesson permanently, and feeds its two most recent lessons into every future allocation prompt

**How it runs itself:**
- `START GYNA.bat` — 1-click launch with auto-restart 10s after any crash
- `STOP GYNA.bat` — 1-click kill
- Telegram notifications (optional): boot, every open/close with PnL, daily-loss halt, weekly reflection, shutdown

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

Set via `.env` (see `.env.example`) or environment variables:

| Key | Default | Description |
|-----|---------|-------------|
| `MT5_LOGIN` | — | ICMarkets account number |
| `MT5_PASSWORD` | — | Account password |
| `MT5_SERVER` | `ICMarketsSC-Demo` | Broker server |
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `groq` / `ollama` — falls through groq → anthropic → local |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `MODEL` | `claude-sonnet-5` | Anthropic model ID |
| `GROQ_API_KEY` | — | Optional Groq fallback (free tier) |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `localhost:11434` / `qwen2.5:14b` | Optional local LLM |

Trading constants (risk tiers, daily halt, cooldowns) live in `config.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `MAX_RISK_PER_TRADE` | 0.25% | Hard per-trade risk cap |
| `MAX_DAILY_LOSS` | 5% | Daily drawdown breaker |
| `MAX_OPEN_POSITIONS` | 1 | BTCUSD concentration — no hedging |

---

## File Map

| File | Role |
|------|------|
| `main.py` | Entry point |
| `main_orchestrator.py` | 50ms loop supervisor — full execution pipeline |
| `config.py` | All constants + risk tiers (env-overridable LLM settings) |
| `feature_engine.py` | Causal OHLCV → FeatureSnapshot (no repaint) + quantized state signature |
| `regime_engine.py` | ADX + BB + Choppiness → TREND/RANGE/VOLATILE |
| `edge_engine.py` | Deterministic directional mask + quality score |
| `claude_allocator.py` | Multi-provider LLM allocator (Anthropic/Groq/Ollama/local) |
| `risk_engine.py` | Final authority — lot sizing, gates, tier promotion |
| `execution_telemetry.py` | Latency + slippage → EQD coefficient |
| `state_manager.py` | SQLite WAL live position state + closed-trades ledger + telemetry buffer |
| `post_trade_analytics.py` | Post-session audit — regime/session/toxic-state analysis |
| `mt5_bridge.py` | Thin MT5 connection wrapper |
| `memory/trade_log.py` | Permanent trade memory — never deletes |

Databases: `memory/gyna_trades.db` (permanent trade memory) and
`memory/system_state.db` (live stealth positions, telemetry, closed-trades ledger)
are intentionally separate files.

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

## Tests

```bash
python -m pytest tests/ -q
```

Covers: edge engine, risk engine, execution telemetry, trade memory
(close-by-ticket, orphan reconciliation), state manager (locking, schema
migration, closed-trades ledger), feature engine (state-signature recurrence),
and post-trade analytics (toxic-state detection).

---

## Hardening History

- `docs/GYNA_HARDENING_AUDIT_001.md` — priorities 1–7 (latency telemetry,
  risk-context plumbing, broker-authoritative PnL, day anchor, emergency SL).
- **2026-07-12 audit remediation** — restored the missing
  `close_trade_by_ticket()` (fatal AttributeError on every stealth close),
  wired the closed-trades ledger (analytics/toxic-blocklist were inert),
  introduced the quantized state signature (per-bar snapshot hashes can never
  recur), fixed the broker-PnL race (`None` sentinel + close-deal wait +
  tick-value synthetic fallback), day-rollover breaker reset, clock-skew-immune
  feed watchdog, crash-proof frame loop, boot-time orphan reconciliation,
  split state/trade DBs, env-driven provider/model config, `.gitignore`
  hardening, and 20 new unit tests.
