"""
Gyna — config.py
Central configuration. NO external imports (no mt5 here — imported only in mt5_bridge.py).
AURELIA EMPIRE | ZEROEMOTIONS | CLAUDE inside™
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Identity ───────────────────────────────────────────────────────────────
NAME    = "Gyna"
VERSION = "1.0.0"

# ── MT5 Connection ─────────────────────────────────────────────────────────
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", 0))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "")

# ── LLM ───────────────────────────────────────────────────────────────────
LLM_PROVIDER      = "anthropic"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL             = "claude-sonnet-4-20250514"

# ── Trading ────────────────────────────────────────────────────────────────
SYMBOLS        = ["BTCUSD"]
TIMEFRAME_STR  = "M15"           # human-readable; mt5_bridge converts to mt5.TIMEFRAME_M15
BARS           = 200
CYCLE_MINUTES  = 1               # M1 scalping

# Kill hours (UTC) — empirically proven worst hours across fleet
KILL_HOURS_UTC = []

# ── Risk Tiers ─────────────────────────────────────────────────────────────
# (min_trades, min_win_rate, min_profit_factor) → risk_pct
RISK_TIERS = [
    {"min_trades":   0, "min_wr": 0.00, "min_pf": 0.0, "risk_pct": 0.005},  # Tier 1 — proving ground
    {"min_trades":  20, "min_wr": 0.45, "min_pf": 1.2, "risk_pct": 0.008},  # Tier 2 — gaining confidence
    {"min_trades":  50, "min_wr": 0.50, "min_pf": 1.5, "risk_pct": 0.010},  # Tier 3 — proven
    {"min_trades": 100, "min_wr": 0.55, "min_pf": 1.8, "risk_pct": 0.015},  # Tier 4 — scaling
    {"min_trades": 200, "min_wr": 0.60, "min_pf": 2.0, "risk_pct": 0.020},  # Tier 5 — full power
]

MAX_RISK_PER_TRADE  = 0.020   # hard cap — never exceed regardless of tier
MAX_DAILY_LOSS      = 0.050   # 5% daily halt
MAX_OPEN_POSITIONS  = 1       # BTCUSD concentration — no hedging
MIN_CONFIDENCE      = 0.65    # Claude must be ≥65% confident to trade

# SL/TP defaults (ATR multiples) — Claude can override per-trade
DEFAULT_SL_ATR = 1.5
DEFAULT_TP_ATR = 3.0          # 2:1 RR minimum

# ── Memory ────────────────────────────────────────────────────────────────
DB_PATH           = "memory/gyna_trades.db"
VECTOR_STORE_PATH = "memory/qdrant_store"
SIMILAR_TRADES_K  = 5         # how many similar past trades to retrieve
RECENT_LOSSES_N   = 10        # how many recent losses to show Claude

# ── Reflection ────────────────────────────────────────────────────────────
REFLECTION_DAY    = 6         # Sunday (0=Monday)
REFLECTION_HOUR   = 0         # 00:00 UTC

# ── Notifications (optional) ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")

print(f"✅ {NAME} v{VERSION} config loaded | Model: {MODEL}")
