# PARALLAX — Gyna

**Persistent Intelligence Trading System for MetaTrader 5**
*True Memory. Compounding AI. Never resets.*
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.

---

## Architecture

```
MT5 BTCUSD M1 bars
  → FeatureEngine    (facts: price action + RSI, MACD, BB, HMA, regime, session)
  → Trading Styles   (scalper: pure price action | runner: trend rider)
  → EdgeEngine       (arbitrates styles by LEARNED live expectancy — LLM cannot override)
  → ClaudeAllocator  (Anthropic / Groq / Ollama / local fallback)
  → RiskEngine       (final authority — lot size, tier, daily halt)
  → Stealth Execution (virtual SL/TP in SQLite; broker gets only a wide emergency SL)
  → TradeMemory      (every trade logged permanently, per-style stats)
  → EQD Telemetry    (execution quality degrades aggression)
  → Post-Trade Audit (closed-trades ledger → toxic states, kill hours, style expectancy)
```

**Two trading styles** (`trading_styles.py`):

| | SCALPER | TREND RUNNER |
|---|---|---|
| Signal inputs | **Raw price action only** — consecutive directional closes, range-expansion bursts, liquidity sweep-reclaims. Zero lagging indicators. | Confirmed TREND regime + HMA/structure agreement + momentum confirmation (lag acceptable — it rides, it doesn't react) |
| Sessions | Any | LONDON / NY_OVERLAP / NY only |
| Stop (ATR) | 0.5–0.9 (tight) | 1.2–2.0 (wide) |
| Target (ATR) | 0.7–1.4 (quick ~1.5R) | 3.0–4.5 (let winners run) |
| Cadence | Frequent | Few, high conviction |

Both styles evaluate every bar; the EdgeEngine picks the winner weighted by
each style's **live profit factor** (neutral until a style has 10 closed
trades, then 0.6×–1.4×) — Gyna learns which style is earning and shifts
weight automatically. The chosen style's risk envelope bounds Claude's SL/TP.

**Key properties:**
- Claude is the *allocator*, not the signal — EdgeEngine controls direction
- Broker-authoritative PnL via MT5 deal history (waits for the closing deal; synthetic tick-value fallback)
- Crash-safe: rebuilds daily loss + position state from broker on every restart; reconciles orphaned trade-memory rows
- Crash-proof loop: a failing frame is logged and retried, never kills the process while positions are open
- Risk tier auto-promotes based on real win rate + profit factor
- Toxic-state blocklist matches on a **quantized state signature** (recurring across bars), not the per-bar snapshot hash
- 48/48 unit tests passing

## GynaBrain — actual machine learning

`learning_brain.py` is a real ML model whose **weights update after every
closed trade** — online logistic regression with AdaGrad, written from
scratch in numpy so every weight is inspectable (`brain.top_weights()`
prints the model in plain English).

- **Predicts P(win)** for every candidate signal from ~25 features (regime,
  session, style, price action, momentum, fatigue, direction)
- **Scales Claude's aggression** 0.6×–1.4× by that prediction (capped by the
  edge score — the brain is a voice, never the authority)
- **Learns at close**: the entry feature vector is persisted with the
  position (crash-safe) and replayed as a gradient step when the outcome is
  known. Win or loss literally rewires the model.
- **Influence is earned**: confidence ramps 0→1 over its first 200 outcomes
  — a day-one brain is mute. A mature brain (100+ outcomes) may hard-veto
  setups it scores under 30% (that veto is a safety filter and obeys
  `SAFETY_FILTERS`; the sizing modulation is core intelligence, always on).
- **Per-symbol brains**: `memory/brain.json` in each instance folder —
  the gold brain and the BTC brain never share weights.
- **Pre-training**: `pretrain_brain.py` replays historical M1 bars through
  the exact live pipeline, simulates each signal's virtual SL/TP outcome,
  and trains the brain on thousands of labeled samples before live trade #1:
  ```bash
  cd instances\XAUUSD && python ..\..\pretrain_brain.py --bars 20000
  ```
- Every entry prediction is stored (`brain_p_win` in trade memory) so the
  brain's calibration is auditable against reality.

Deliberately a low-variance linear model: trading samples are few and
noisy; a deep net here would memorize noise. When the ledger holds a few
hundred real trades, upgrading the same interface to a richer model is a
drop-in change.

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

## Quick Start — multi-asset instances (XAUUSD + BTCUSD)

One shared codebase, one folder per symbol. Each instance folder holds only
its `.env`, databases, and logs — **no code is ever copied**. Learning state
(trade memory, tiers, toxic blocklist, kill hours, reflections) is fully
isolated per symbol, because a gold lesson applied to BTC is noise.

```bash
git clone https://github.com/janpauldelacruz/gyna
cd gyna
pip install -r requirements.txt

# Configure each instance you want to run:
copy instances\XAUUSD\.env.example instances\XAUUSD\.env   # then fill in
copy instances\BTCUSD\.env.example instances\BTCUSD\.env   # then fill in

"START GYNA.bat"           # starts every configured instance
"START GYNA.bat" XAUUSD    # or just one
"STOP GYNA.bat"            # stops all (or pass a symbol)
```

Single-instance use still works: put a `.env` in the repo root and
`python main.py`.

Per-symbol market differences live in ONE place — `SYMBOL_PROFILES` in
`config.py` (spread cap, cooldown, feed staleness, market hours). All
signal/sizing logic is ATR-relative and asset-agnostic. The account-level
5% daily breaker is computed from account equity and all deals, so both
instances halt together no matter which one caused the loss.

## Master Safety Toggle

`SAFETY_FILTERS=off` in an instance's `.env` disables **all** protective
filters at once: spread firewall, entry cooldown, kill hours (configured +
learned), toxic-state penalty, 3-loss flatten + per-loss risk halving, EQD
penalty, daily-loss breaker, cost-friction gate, and margin-stress gate.
Boot logs and Telegram shout a warning when it's off.

**Always on regardless of the toggle** (physics, not filters):
`MAX_RISK_PER_TRADE` clamp, broker lot limits, `MAX_OPEN_POSITIONS`,
the emergency broker SL, the terminal/feed watchdog, and market-closed
hours. Default is `on`; `off` is for unfiltered testing only.

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
