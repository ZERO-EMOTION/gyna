"""
Gyna — main.py
Full autonomous trading loop for BTCUSD.
Phase 1: Foundation — MT5 connect + feature stub + memory logging.
Phase 2+ will wire Claude brain, vector store, reflection.
AURELIA EMPIRE | ZEROEMOTIONS | CLAUDE inside™
"""
import time
import schedule
import logging
from datetime import datetime, timezone

from config import (
    NAME, VERSION, SYMBOLS, CYCLE_MINUTES, KILL_HOURS_UTC,
    MAX_DAILY_LOSS, DB_PATH,
)
from mt5_bridge import MT5Bridge
from memory.trade_log import TradeMemory

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gyna.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("Gyna")

# ── Init ───────────────────────────────────────────────────────────────────
log.info(f"🚀 Starting {NAME} v{VERSION}...")

bridge = MT5Bridge()
memory = TradeMemory(DB_PATH)

if not bridge.connect():
    log.critical("MT5 connection failed — exiting.")
    exit(1)

log.info(f"✅ {NAME} connected | Symbols: {SYMBOLS}")


# ── Kill hour check ────────────────────────────────────────────────────────
def is_kill_hour() -> bool:
    hour = datetime.now(timezone.utc).hour
    return hour in KILL_HOURS_UTC


# ── Daily loss check ───────────────────────────────────────────────────────
def check_daily_halt() -> bool:
    if memory.is_daily_halted():
        return True
    account = bridge.get_account_info()
    if account is None:
        return False
    daily_pnl = memory.get_daily_pnl()
    if account["balance"] > 0:
        pnl_pct = abs(daily_pnl) / account["balance"]
        if daily_pnl < 0 and pnl_pct >= MAX_DAILY_LOSS:
            log.warning(f"🛑 Daily loss limit hit ({pnl_pct:.1%}) — halting today")
            memory.set_daily_halt()
            return True
    return False


# ── Main trading cycle ─────────────────────────────────────────────────────
def trading_cycle():
    log.info(f"── Cycle start [{datetime.now(timezone.utc).strftime('%H:%M UTC')}] ──")

    # ── Guards ────────────────────────────────────────────────────────────
    if is_kill_hour():
        log.info(f"⏸  Kill hour {datetime.now(timezone.utc).hour} UTC — skipping cycle")
        return

    if check_daily_halt():
        log.info("⏸  Daily halt active — skipping cycle")
        return

    account = bridge.get_account_info()
    if account is None:
        log.warning("Could not fetch account info — skipping cycle")
        return

    open_positions = bridge.get_positions()
    n_open = len(open_positions) if open_positions else 0

    log.info(f"Account: ${account['balance']:.2f} | Equity: ${account['equity']:.2f} | Open: {n_open}")

    # ── Per-symbol loop ───────────────────────────────────────────────────
    for symbol in SYMBOLS:
        log.info(f"Analyzing {symbol}...")

        df = bridge.get_rates(symbol)
        if df is None or len(df) < 50:
            log.warning(f"  {symbol}: Insufficient data — skipping")
            continue

        # ── Phase 2: Feature engine will go here ──────────────────────────
        # features = FeatureEngine.compute(df)
        # For now log the last bar as placeholder
        last = df.iloc[-1]
        log.info(f"  {symbol}: Last bar close={last['close']:.2f} | Bars loaded: {len(df)}")

        # ── Phase 2: Claude brain will go here ───────────────────────────
        # similar = memory.query_similar(features, k=SIMILAR_TRADES_K)
        # losses  = memory.get_recent_losses(RECENT_LOSSES_N)
        # decision = ClaudeBrain.decide(features, similar, losses)
        # For now log a HOLD — foundation only
        decision = {
            "action":     "HOLD",
            "confidence": 0.0,
            "rationale":  "Phase 1 foundation — brain not yet wired",
            "regime":     "unknown",
            "session":    _get_session(),
        }
        log.info(f"  {symbol}: Decision={decision['action']} | Conf={decision['confidence']:.2f}")

        # ── Log every cycle decision to memory (HOLD cycles included) ─────
        if decision["action"] == "HOLD":
            # Only log HOLD decisions every 4 hours to avoid DB bloat
            if datetime.now(timezone.utc).hour % 4 == 0 and datetime.now(timezone.utc).minute < CYCLE_MINUTES:
                memory.log_trade({
                    "symbol":     symbol,
                    "direction":  "HOLD",
                    "entry_price": float(last["close"]),
                    "volume":     0.0,
                    "rationale":  decision["rationale"],
                    "regime":     decision["regime"],
                    "session":    decision["session"],
                    "confidence": decision["confidence"],
                    "outcome":    "open",
                })
            continue

        # ── Phase 3: Risk engine + execution will go here ─────────────────
        # validated = RiskEngine.validate(decision, account, n_open)
        # if validated["approved"]:
        #     ticket = bridge.send_order(...)
        #     memory.log_trade({...})

    log.info(f"── Cycle complete ──\n")


def _get_session() -> str:
    """Classify current UTC hour into trading session."""
    h = datetime.now(timezone.utc).hour
    if 7 <= h < 12:
        return "london"
    elif 12 <= h < 17:
        return "overlap"
    elif 17 <= h < 22:
        return "ny"
    else:
        return "asia"


# ── Scheduler ─────────────────────────────────────────────────────────────
schedule.every(CYCLE_MINUTES).minutes.do(trading_cycle)

log.info(f"⏰ Scheduler running — cycle every {CYCLE_MINUTES} minutes")
log.info(f"🔴 Kill hours UTC: {KILL_HOURS_UTC}")
log.info(f"📊 Monitoring: {SYMBOLS}")
log.info("Gyna is live. Press Ctrl+C to stop.\n")

# Run once immediately on start
trading_cycle()

while True:
    schedule.run_pending()
    time.sleep(10)
