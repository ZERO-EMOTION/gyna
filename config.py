"""
Gyna — config.py
Central configuration. NO external imports (no mt5 here — imported only in mt5_bridge.py).
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Identity ───────────────────────────────────────────────────────────────
NAME    = "Gyna"
VERSION = "1.1.0"

# ── MT5 Connection ─────────────────────────────────────────────────────────
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", 0))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "")

# ── LLM ───────────────────────────────────────────────────────────────────
# Provider chain: the allocator tries LLM_PROVIDER first, then falls through
# groq → anthropic → local deterministic fallback.
LLM_PROVIDER      = os.getenv("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL             = os.getenv("MODEL", "claude-sonnet-5")
GROQ_MODEL        = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_URL        = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

# ── Trading ────────────────────────────────────────────────────────────────
SYMBOLS        = ["BTCUSD"]
TIMEFRAME_STR  = "M1"            # M1 scalping
BARS           = 200
CYCLE_MINUTES  = 1               # M1 scalping

# Kill hours (UTC) — empirically proven worst hours across fleet
KILL_HOURS_UTC = []

# ── Risk Tiers ─────────────────────────────────────────────────────────────
# (min_trades, min_win_rate, min_profit_factor) → risk_pct
# BTC M1 risk tiers — expectancy-focused, not win-rate-focused
# BTC survives through asymmetry (larger wins), not ultra-high WR
RISK_TIERS = [
    {"min_trades":   0, "min_wr": 0.00, "min_pf": 0.0, "risk_pct": 0.0025},  # Tier 1 — observation
    {"min_trades":  30, "min_wr": 0.40, "min_pf": 1.3, "risk_pct": 0.0050},  # Tier 2 — validated
    {"min_trades":  75, "min_wr": 0.45, "min_pf": 1.5, "risk_pct": 0.0075},  # Tier 3 — proven
    {"min_trades": 150, "min_wr": 0.50, "min_pf": 1.8, "risk_pct": 0.0100},  # Tier 4 — scaling
    {"min_trades": 300, "min_wr": 0.55, "min_pf": 2.0, "risk_pct": 0.0150},  # Tier 5 — full power
]

MAX_RISK_PER_TRADE  = 0.0025  # BTC M1 — 0.25% max initially
MAX_DAILY_LOSS      = 0.050   # 5% daily halt
MAX_OPEN_POSITIONS  = 1       # BTCUSD concentration — no hedging

# ── Memory ────────────────────────────────────────────────────────────────
DB_PATH           = "memory/gyna_trades.db"   # TradeMemory — permanent trade log
STATE_DB_PATH     = "memory/system_state.db"  # StateManager + analytics — live state
RECENT_LOSSES_N   = 10        # how many recent losses to show Claude

# ── Emergency broker stop (disaster fallback for Python/VPS crash) ────────
USE_BROKER_EMERGENCY_SL  = True   # Set broker SL wider than virtual; virtual remains primary
EMERGENCY_SL_MULTIPLIER  = 1.50   # Broker SL = virtual SL × this multiplier

# ── Reflection ────────────────────────────────────────────────────────────
REFLECTION_DAY    = 6         # Sunday (0=Monday)
REFLECTION_HOUR   = 0         # 00:00 UTC

# ── Notifications (optional) ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")

# ASCII-only: emoji in print() crashes on cp1252 Windows consoles
print(f"[OK] {NAME} v{VERSION} config loaded | Provider: {LLM_PROVIDER} | Model: {MODEL}")
