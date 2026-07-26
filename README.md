# Gyna

**A self-learning autonomous trading system for MetaTrader 5.**
*True memory. Compounding intelligence. Never resets.*

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.

Gyna is a learning trader, not a fixed-rule bot. Deterministic code decides
**whether** and **which direction** to trade; a language model decides **how
aggressively** within hard bounds; a risk engine has **final authority**; and
every closed trade — plus every trade it *declines* — updates the system's
memory and a real machine-learning model. The edge is meant to compound from
its own experience, per symbol, indefinitely.

---

## Table of Contents

1. [Design philosophy](#design-philosophy)
2. [Architecture](#architecture)
3. [The two trading styles](#the-two-trading-styles)
4. [How it learns](#how-it-learns)
5. [Safety systems](#safety-systems)
6. [Multi-asset instances](#multi-asset-instances)
7. [Deployment](#deployment)
8. [Configuration reference](#configuration-reference)
9. [Operations](#operations)
10. [Pre-training](#pre-training)
11. [Testing & validation](#testing--validation)
12. [File map](#file-map)
13. [Requirements](#requirements)

---

## Design philosophy

- **Features are facts. The LLM is interpretation. The RiskEngine is law.**
  Direction is never the LLM's to choose; sizing is never the final word.
- **Every action generates a lesson** — BUY, SELL, and SKIP alike. A skipped
  signal is just an unlabeled trade that hindsight later grades.
- **Trust is earned, not configured.** Position size starts at 0.25% and only
  grows as real win-rate and profit-factor justify it.
- **The market is trend, range, or volatile; the decision is BUY, SELL, or
  SKIP.** The whole machine exists to map the first onto the second.
- **Broker truth over synthetic math.** PnL, fills, and daily loss are read
  from MT5 deal history (commission and swap included), never estimated.

---

## Architecture

```
MT5 bars (M1, closed candles only — no repaint)
  │
  ▼
FeatureEngine        facts: price action + RSI/MACD/BB/HMA, regime, session,
  │                  ATR, structure, volume, quantized state signature
  ▼
Trading Styles       two candidate signals: scalper (price action) + runner
  │                  (trend), each with its own SL/TP envelope
  ▼
EdgeEngine           arbitrates the two styles by LEARNED live expectancy;
  │                  emits permitted_direction in {+1, -1, 0} — the LLM
  │                  cannot override it
  ▼
GynaBrain (ML)       predicts P(win); scales aggression; can veto a mature,
  │                  clearly-losing setup
  ▼
ClaudeAllocator      LLM sizes the trade within hard bounds
  │                  (Anthropic / Groq / Ollama / deterministic fallback)
  ▼
RiskEngine           FINAL AUTHORITY: tier sizing, daily breaker, friction,
  │                  margin, cost gates — approve or reject
  ▼
Stealth Execution    market order; virtual SL/TP held in SQLite; broker sees
  │                  only a wide emergency stop (crash insurance)
  ▼
TradeMemory          every trade logged permanently, per-style stats
  +
Shadow / Reflection  skipped signals hindsight-labeled; weekly self-review
```

Two clocks run concurrently:

- **50 ms tick loop** — terminal watchdog, position cache, and virtual SL/TP
  enforcement with adaptive time-decay (stale trades exit sooner).
- **1-minute bar loop** — the full decision pipeline above, once per closed
  M1 candle.

The bar loop is **crash-proof**: any exception in a frame is logged and the
loop continues (aborting only after 100 consecutive failures), so a transient
error can never leave an open position unmonitored.

---

## The two trading styles

Both evaluate every bar; the EdgeEngine picks the winner, weighted by each
style's live profit factor.

| | **SCALPER** | **TREND RUNNER** |
|---|---|---|
| Signal | Raw price action only — consecutive directional closes, range-expansion bursts, liquidity sweep-reclaims. **Zero lagging indicators.** | Confirmed TREND + HMA/structure agreement + momentum confirmation |
| Sessions | Any | London / NY / NY-overlap only |
| Stop (ATR) | 0.5–0.9 (tight) | 1.2–2.0 (wide) |
| Target (ATR) | 0.7–1.4 (quick ~1.5R) | 3.0–4.5 (let winners run) |
| Cadence | Frequent | Selective, high-conviction |
| Edge source | Fast reversion & momentum | Asymmetry — the tail of the winners |

The scalper's lag-free property is enforced by test: it must produce an
identical signal with and without RSI/MACD/BB present in the snapshot.

**Style arbitration is itself learned.** A style's vote is scaled 0.6x–1.4x
by its live profit factor (neutral until it has 10 closed trades). If the
runner is earning and the scalper is bleeding, runner signals start winning
ties automatically — no config change.

---

## How it learns

Three mechanisms operate on three timescales. All are per-symbol and persist
across restarts.

### 1. Statistical learning (deterministic, auditable)

| Loop | Mechanism |
|---|---|
| Risk tiers | Win-rate + profit-factor thresholds promote/demote position size (0.25% -> 1.5%) |
| Style arbitration | Live profit factor scales each style's vote |
| Toxic-state blocklist | A quantized market-state signature that recurs 3+ times with net-negative PnL earns a 50% aggression penalty |
| Empirical kill hours | Hours with >=10 trades and net-negative PnL are blocked (max 4/day, so noise can't kill the whole day) |
| EQD | Rolling slippage/latency degrades size when execution quality drops |

Learned state (toxic blocklist + kill hours) refreshes daily in-session — no
restart needed.

### 2. GynaBrain — an actual machine-learning model

`learning_brain.py` is online logistic regression with AdaGrad, written from
scratch in numpy (every weight inspectable via `brain.top_weights()`).

- Predicts **P(win)** for each candidate from ~25 features (regime, session,
  style, price action, momentum, fatigue, direction).
- Scales the LLM's aggression 0.6x–1.4x by that prediction (capped by the
  edge score — the brain is a voice, never the authority).
- **Weights update on every closed trade.** The entry feature vector is
  persisted with the position, so the gradient step survives a crash/restart.
- **Influence is earned:** confidence ramps 0->1 over the first 200 outcomes —
  a fresh brain is mute. A mature brain (100+ outcomes) may hard-veto setups
  it scores under 30% (that veto obeys the master safety toggle).
- Deliberately linear/low-variance — trading samples are few and noisy; the
  interface is drop-in upgradeable to a richer model once the ledger is deep.

### 3. Shadow learning — learning from the trades it skips

Every real signal that gets skipped (brain veto, LLM flat, risk rejection,
failed order) is recorded with its features and would-be SL/TP. Later, price
history answers whether it *would* have won or lost, and that hindsight label
trains the brain at reduced weight (0.3x — counterfactuals carry no execution
reality, so they never outvote real fills). Since Gyna skips far more than she
takes, this multiplies the learning rate. `ShadowLearner.stats()` reports, per
skip reason, whether skipping was wise.

### Weekly self-reflection

Every Sunday 00:00 UTC, Gyna sends the week's closed trades to the LLM and
asks what it should learn — which style earned its risk, what patterns lost,
one concrete rule for next week. The lesson is stored permanently and the two
most recent are injected into every future allocation prompt.

---

## Safety systems

### Master toggle

`SAFETY_FILTERS=off` in an instance's `.env` disables **all** protective
filters at once: spread firewall, entry cooldown, kill hours, toxic penalty,
3-loss cooldown, per-loss risk halving, EQD penalty, daily breaker, friction
gate, margin-stress gate. Boot logs and Telegram shout when it's off.

**Always on regardless of the toggle** (physics, not filters):
`MAX_RISK_PER_TRADE` clamp, broker lot limits, `MAX_OPEN_POSITIONS`, the
emergency broker SL, the terminal/feed watchdog, and market-closed hours.

### Key protections

- **Daily-loss breaker** — halts trading after Gyna's *own* magic-stamped PnL
  loses `MAX_DAILY_LOSS` (5%) in a UTC day. Counts only her trades, so a human
  or other EA on the same account cannot trip it.
- **3-consecutive-loss cooldown** — pauses new entries after 3 straight
  losses, then **auto-resets after 2 hours** (or on restart). An earlier
  version required a win to reset and could deadlock; this is fixed.
- **Cost-friction gate** — rejects trades where spread would consume too much
  of the target (this is why BTCUSD, with its wide M1 spread, trades less than
  gold).
- **Margin-stress gate** — rejects any order that would exceed 70% of free
  margin.
- **Account guard** — refuses to trade unless the connected login matches
  `MT5_LOGIN` exactly (prevents a demo bot ever acting on a live account).
- **Broker-authoritative PnL** — waits for the closing deal before recording a
  result; out-of-band closes (emergency SL, manual) settle fully and teach the
  brain.

---

## Multi-asset instances

One shared codebase, one folder per symbol. Each instance folder holds only
its `.env`, databases, and logs — **no code is ever copied**. Learning state
is fully isolated per symbol, because a gold lesson applied to BTC is noise.

```
gyna/
├── main.py, engines, styles, tests ...   (the engine, never duplicated)
├── START GYNA.bat / STOP GYNA.bat
└── instances/
    ├── XAUUSD/   .env + memory/*.db + gyna.log
    └── BTCUSD/   .env + memory/*.db + gyna.log
```

Per-symbol market differences live in ONE place — `SYMBOL_PROFILES` in
`config.py` (spread cap, cooldown, feed staleness, market hours, magic
number). All signal and sizing logic is ATR-relative and asset-agnostic.

Each symbol's orders carry a distinct **magic number** (BTCUSD 20260101,
XAUUSD 20260102); every position read filters by it, so Gyna, manual trading,
and other EAs coexist on one account without ever touching each other's trades.

---

## Deployment

Production deployment runs each instance as a **Windows Scheduled Task**:

- **Logon-triggered** — starts automatically on user login (survives reboots
  and power outages).
- **No execution time limit**, auto-restart on failure.
- Action: `cmd /c "START GYNA.bat" run <SYMBOL>`, which relaunches the Python
  process 10 s after any crash — a double layer of resilience.

**Dedicated terminal.** Gyna runs against its own portable MT5 copy
(`MT5_TERMINAL_PATH` + `MT5_PORTABLE=true`) so its Python login can never
switch the account of a main terminal running other EAs. Every login uses
explicit credentials — a bare `mt5.initialize()` is never used.

**One-time terminal setting:** the portable terminal's `Config/common.ini`
must have `[Experts] Account=0, Profile=0, AllowLiveTrading=1` (edit only while
the terminal is stopped, it's UTF-16). Otherwise MT5 auto-disables algo
trading on the API login and every order is rejected with retcode 10027.

### Quick start

```bash
git clone https://github.com/janpauldelacruz/gyna
cd gyna
pip install -r requirements.txt

# Configure each instance:
copy instances\XAUUSD\.env.example instances\XAUUSD\.env   # then fill in
copy instances\BTCUSD\.env.example instances\BTCUSD\.env   # then fill in

"START GYNA.bat"           # start every configured instance
"START GYNA.bat" XAUUSD    # or just one
"STOP GYNA.bat"            # stop all (or pass a symbol)
```

For unattended production, register the scheduled tasks instead of the
launcher (see [Operations](#operations)).

---

## Configuration reference

Set via each instance's `.env` (see `instances/<SYMBOL>/.env.example`):

| Key | Default | Description |
|---|---|---|
| `SYMBOL` | BTCUSD | The instance's trading symbol |
| `SAFETY_FILTERS` | on | Master safety toggle (`on`/`off`) |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | — | Account credentials |
| `MT5_TERMINAL_PATH` | — | Dedicated portable terminal64.exe |
| `MT5_PORTABLE` | false | Run the terminal in portable mode |
| `LLM_PROVIDER` | anthropic | `anthropic` / `groq` / `ollama` — falls through to the others, then a deterministic local fallback |
| `MODEL` | claude-sonnet-5 | Anthropic model ID |
| `OLLAMA_URL` / `OLLAMA_MODEL` | localhost:11434 / qwen2.5:14b | Local free-AI settings |
| `GROQ_API_KEY` / `GROQ_MODEL` | — | Optional Groq fallback |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | — | Optional phone notifications |

Trading constants (risk tiers, `MAX_DAILY_LOSS`, `MAX_RISK_PER_TRADE`,
`MAX_OPEN_POSITIONS`) and per-symbol profiles live in `config.py`.

### Risk tiers (auto-promote from live results)

| Tier | Min trades | Min WR | Min PF | Risk % |
|---|---|---|---|---|
| 1 — Observation | 0 | 0% | 0.0 | 0.25% |
| 2 — Validated | 30 | 40% | 1.3 | 0.50% |
| 3 — Proven | 75 | 45% | 1.5 | 0.75% |
| 4 — Scaling | 150 | 50% | 1.8 | 1.00% |
| 5 — Full Power | 300 | 55% | 2.0 | 1.50% |

---

## Operations

### Run as scheduled tasks (production)

```powershell
$action  = New-ScheduledTaskAction -Execute "cmd.exe" `
  -Argument '/c ""C:\Trading\container\GYNA\START GYNA.bat" run BTCUSD"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "GYNA-BTCUSD" -Action $action -Trigger $trigger -Settings $settings
Start-ScheduledTask -TaskName "GYNA-BTCUSD"
```

Stop: `Stop-ScheduledTask -TaskName "GYNA-BTCUSD"` (or `STOP GYNA.bat`).

> **Never launch Gyna as a child of another process** (e.g. an IDE or agent
> session) — it will be killed when that parent exits. Use scheduled tasks or
> the standalone launcher only.

### Monitoring

- **Logs:** `instances/<SYMBOL>/gyna.log` — grep for `EXEC. FILLED`,
  `STEALTH.*closed`, `EDGE. style`, `BRAIN`, `SHADOW`, `STREAK`, `Frame error`.
- **Telegram:** boot, every open/close with PnL, daily-loss halt, weekly
  reflection, shutdown (if configured).
- **Databases** (per instance, under `memory/`):
  - `gyna_trades.db` — permanent trade log, reflections, per-style stats
  - `system_state.db` — live stealth positions, telemetry, closed-trades
    ledger, shadow trades
  - `brain.json` — the ML model's weights and lifetime lesson count

### Tests

```bash
python -m pytest tests/ -q
```

---

## Pre-training

`pretrain_brain.py` replays historical M1 bars through the exact live pipeline
(FeatureEngine -> styles -> EdgeEngine), simulates each signal's virtual SL/TP
outcome (conservative: same-bar SL/TP ties count as losses), and trains the
brain on thousands of labeled outcomes before it trades live:

```bash
cd instances\XAUUSD
python ..\..\pretrain_brain.py --bars 20000      # ~2 weeks of M1 from MT5
python ..\..\pretrain_brain.py --csv history.csv # or an offline OHLCV file
```

The result is an *educated prior*, not truth — simulated fills are optimistic
(no spread/slippage), so live fills at full weight steadily re-calibrate it.
After pre-training, zero the AdaGrad accumulator (keep weights, restore
plasticity) so live lessons land at full step size.

---

## Testing & validation

Gyna is a **demo-forward system under validation**. It is not a proven,
production-graded strategy, and nothing here is investment advice.

**Graduation criteria (per symbol, judged independently):**

- 3 weeks of clean, unthrottled trading
- >= 30 closed trades
- Profit factor >= 1.3, win rate >= 40%
- 3 weekly windows, 0 net-losing
- `brain_p_win` predictions calibrated against actual outcomes
- No unexplained halts or crashes

A symbol that fails the bar does not "go live anyway" — validation extends, or
a deliberate change is made. Even after graduation, live deployment starts at
Tier 1 (0.25%) on the smallest viable account and re-earns every promotion
with real money. The tier ladder means evaluation never truly ends; it only
changes stakes.

---

## File map

| File | Role |
|---|---|
| `main.py` | Entry point |
| `main_orchestrator.py` | 50 ms + 1 min loop supervisor; full execution pipeline |
| `config.py` | Constants, risk tiers, per-symbol profiles, master toggle |
| `feature_engine.py` | Closed-bar features + price-action facts + state signature |
| `regime_engine.py` | ADX + BB width + Choppiness -> TREND/RANGE/VOLATILE |
| `trading_styles.py` | Scalper + runner signals, learned style weighting |
| `edge_engine.py` | Style arbiter -> directional mask (LLM cannot override) |
| `learning_brain.py` | GynaBrain — online-ML P(win) model |
| `shadow_learner.py` | Hindsight-labels skipped signals, teaches the brain |
| `claude_allocator.py` | Multi-provider LLM allocator |
| `risk_engine.py` | Final authority — sizing, tiers, gates |
| `execution_telemetry.py` | Latency + slippage -> EQD coefficient |
| `state_manager.py` | SQLite live state, closed-trades ledger, telemetry |
| `post_trade_analytics.py` | Toxic-state, kill-hour, style-expectancy analysis |
| `reflection_engine.py` | Weekly LLM self-review |
| `telegram_notifier.py` | Fire-and-forget notifications |
| `memory/trade_log.py` | Permanent trade memory — never deletes |
| `pretrain_brain.py` | Historical brain pre-training |

---

## Requirements

- Windows + MetaTrader 5 (broker: ICMarkets SC / Raw Trading Ltd)
- Python 3.12
- `MetaTrader5`, `pandas`, `numpy`, `ta`, `python-dotenv`
- One LLM provider: local Ollama (free, default in production), or an
  Anthropic / Groq API key
- `pip install -r requirements.txt`

---

*Built by PARALLAX — JP × Claude. Gyna learns, therefore Gyna endures.*
