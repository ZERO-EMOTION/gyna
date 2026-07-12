"""
Gyna — main_orchestrator.py
Production-hardened execution loop supervisor.

Architecture:
  50ms monotonic perf_counter tick loop (position telemetry + watchdog)
  1-min bar allocation cycle (feature → edge → Claude → risk → execute)

Key mechanisms:
  enforce_terminal_watchdog()      — connectivity + feed staleness check
  update_position_cache_barrier()  — 250ms throttle, optimistic reconcile
  historical_bar_execution_registry — prevents double-entry per bar+direction
  prune_historical_bar_registry()  — 24hr memory pruning
  toxicity_scalar                  — 0.5x aggression on toxic snapshot hash
  process_tick_telemetry()         — adaptive decay stealth exits
  system_rehydration_barrier()     — crash-safe startup reconciliation
  _rebuild_daily_loss_from_history() — broker-authoritative daily PnL (P4)
  _realized_pnl_for_position()     — broker deal history PnL (P3)

Hardening audit: GYNA_HARDENING_AUDIT_001 (priorities 1-6 applied)

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Set, Tuple

import MetaTrader5 as mt5

from config import (
    NAME, VERSION, SYMBOLS, CYCLE_MINUTES, KILL_HOURS_UTC,
    MAX_OPEN_POSITIONS, DB_PATH, STATE_DB_PATH, RECENT_LOSSES_N,
    RISK_TIERS, USE_BROKER_EMERGENCY_SL, EMERGENCY_SL_MULTIPLIER,
    MAX_DAILY_LOSS,
)
from feature_engine import FeatureEngine
from edge_engine import EdgeEngine
from claude_allocator import ClaudeAllocator
from risk_engine import RiskEngine
from execution_telemetry import ExecutionTelemetry
from state_manager import StateManager
from post_trade_analytics import (
    PostTradeValidationEngine, compute_adaptive_stealth_decay
)
from memory.trade_log import TradeMemory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gyna.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("Gyna.Orchestrator")

# ── Runtime constants ──────────────────────────────────────────────────────
LOOP_CADENCE_S           = 0.050   # 50ms tick frame
POSITION_CACHE_INTERVAL  = 0.250   # 250ms position poll throttle
HEARTBEAT_INTERVAL       = 1.0     # 1Hz terminal watchdog
MAX_FEED_STALENESS_S     = 60.0    # relaxed for BTCUSD
MAX_SPREAD_POINTS        = 3000   # BTCUSD aggressive spread filter
MIN_COOLDOWN_S           = 180   # 3 min cooldown — BTC: fewer trades, higher quality
OPTIMISTIC_TTL_S         = 2.0     # max time for optimistic position to propagate
BAR_REGISTRY_RETENTION_S = 86400  # 24hr bar registry pruning
SYMBOL                   = SYMBOLS[0]   # BTCUSD


# ── Environment validation ────────────────────────────────────────────────
def _validate_env():
    from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
    import os
    missing = []
    if not MT5_LOGIN or MT5_LOGIN == 0:   missing.append("MT5_LOGIN")
    if not MT5_PASSWORD:                   missing.append("MT5_PASSWORD")
    if not MT5_SERVER:                     missing.append("MT5_SERVER")
    if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("GROQ_API_KEY") and not os.getenv("LLM_PROVIDER","").lower() == "ollama":
        log.warning("No LLM API key set — will use local autonomous fallback")
    if missing:
        log.critical(f"Missing required .env variables: {missing}")
        log.critical("Copy .env.example to .env and fill in credentials")
        exit(1)
    log.info(f"[OK] Environment validated | MT5: {MT5_SERVER} #{MT5_LOGIN}")


class GynaSystemOrchestrator:
    def __init__(self):
        # ── Core engines ───────────────────────────────────────────────────
        # Live state and trade memory live in SEPARATE SQLite files —
        # StateManager uses WAL/per-call connections, TradeMemory holds a
        # persistent connection; sharing one file risks lock contention.
        self.state_db    = StateManager(STATE_DB_PATH)
        self.memory      = TradeMemory(DB_PATH)
        self.telemetry   = ExecutionTelemetry(tracking_window=20)
        self.analytics   = PostTradeValidationEngine(STATE_DB_PATH)
        self.features    = FeatureEngine(SYMBOL, "M1")
        self.edge        = EdgeEngine(fatigue_threshold_bars=48)
        self.allocator   = ClaudeAllocator()
        self.risk        = RiskEngine()

        # ── MT5 bridge (lazy — initialized in rehydration) ─────────────────
        self._mt5_ready  = False

        # ── Monotonic clocks ───────────────────────────────────────────────
        self.last_allocation_ts:   float = 0.0
        self.last_position_poll:   float = 0.0
        self.last_heartbeat:       float = 0.0
        self.last_entry_ts:        float = 0.0

        # ── Live state ─────────────────────────────────────────────────────
        self.cached_positions: Dict[int, Any]                   = {}
        self.bar_registry:     Dict[Tuple[str, int, int], float] = {}
        self.toxic_hashes:     List[str]                         = []
        self.consecutive_losses: int   = 0
        self.daily_loss_pct:     float = 0.0
        self.current_eqd:        float = 0.0

        # ── P4: day-anchor for broker-authoritative daily loss ─────────────
        self.day_anchor_date:   str   = ""
        self.day_start_equity:  float = 0.0
        self.daily_realized_pnl: float = 0.0

        # ── Feed staleness tracking (clock-skew immune) ─────────────────────
        self._last_tick_msc:    int   = 0
        self._last_tick_change: float = time.monotonic()

    # ── Terminal watchdog ──────────────────────────────────────────────────

    def enforce_terminal_watchdog(self) -> bool:
        now = time.time()
        if now - self.last_heartbeat < HEARTBEAT_INTERVAL:
            return True
        self.last_heartbeat = now

        info = mt5.terminal_info()
        if info is None or not info.connected:
            log.warning("[WATCHDOG] Terminal disconnected — recovery sequence")
            return self._emergency_recovery()

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            log.error(f"[WATCHDOG] Cannot fetch tick for {SYMBOL}")
            return False

        # Staleness = local monotonic time since the tick value last CHANGED.
        # Comparing tick.time_msc against local wall-clock false-triggers on
        # broker/VPS clock skew; monotonic change-tracking is skew-immune.
        if tick.time_msc != self._last_tick_msc:
            self._last_tick_msc    = tick.time_msc
            self._last_tick_change = time.monotonic()
        staleness = time.monotonic() - self._last_tick_change
        if staleness > MAX_FEED_STALENESS_S and self._is_market_active():
            log.critical(f"[WATCHDOG] Feed stale {staleness:.2f}s — suspending loop")
            return False

        return True

    def _emergency_recovery(self) -> bool:
        mt5.shutdown()
        time.sleep(1.0)
        if not mt5.initialize():
            log.critical(f"[WATCHDOG] Recovery failed: {mt5.last_error()}")
            return False
        if not mt5.symbol_select(SYMBOL, True):
            log.error(f"[WATCHDOG] Symbol select failed post-recovery: {SYMBOL}")
            return False
        log.info("[WATCHDOG] Terminal restored")
        return True

    def _is_market_active(self) -> bool:
        """BTCUSD trades 24/7. Weekends only block traditional FX."""
        if "BTC" in SYMBOL or "ETH" in SYMBOL:
            return True
        return time.gmtime().tm_wday not in (5, 6)

    # ── P3: Broker-authoritative realized PnL for a closed position ───────

    def _realized_pnl_for_position(self, position_id: int,
                                   lookback_days: int = 7,
                                   retries: int = 3,
                                   retry_delay_s: float = 0.4):
        """
        Query MT5 deal history for the true broker-reported PnL of a position
        (profit + commission + swap + fee across all its deals).

        Returns None — NOT 0.0 — when the CLOSING deal (DEAL_ENTRY_OUT) has
        not yet appeared in history. The close deal can lag order_send by a
        few hundred ms; summing only the entry deal would return the entry
        commission (a small negative number) and silently record a wrong PnL.
        Retries briefly before giving up so the caller can fall back to a
        synthetic estimate.
        """
        from datetime import datetime, timedelta, timezone
        for attempt in range(retries):
            utc_to   = datetime.now(timezone.utc)
            utc_from = utc_to - timedelta(days=lookback_days)
            deals = mt5.history_deals_get(utc_from, utc_to)
            if deals is not None:
                total = 0.0
                close_seen = False
                for d in deals:
                    if int(getattr(d, "position_id", -1)) == int(position_id):
                        total += float(getattr(d, "profit", 0.0))
                        total += float(getattr(d, "commission", 0.0))
                        total += float(getattr(d, "swap", 0.0))
                        total += float(getattr(d, "fee", 0.0))
                        if getattr(d, "entry", -1) == mt5.DEAL_ENTRY_OUT:
                            close_seen = True
                if close_seen:
                    return total
            if attempt < retries - 1:
                time.sleep(retry_delay_s)
        log.warning(f"[PNL] Close deal for position {position_id} not in "
                    f"history after {retries} attempts")
        return None

    # ── P4: Rebuild daily loss from MT5 deal history ───────────────────────

    def _rebuild_daily_loss_from_history(self) -> None:
        """
        Recompute today's realized PnL from MT5 deal history.
        Called on startup and before each allocation to survive crashes/restarts.
        Also resets day anchor when calendar day changes.
        """
        from datetime import datetime, timezone
        now       = datetime.now(timezone.utc)
        today_str = now.date().isoformat()

        # Day rollover — reset the anchor, then FALL THROUGH to recompute.
        # Returning early here would leave daily_loss_pct at yesterday's
        # value for the first allocation of the new day.
        if today_str != self.day_anchor_date:
            account = mt5.account_info()
            self.day_anchor_date  = today_str
            self.day_start_equity = float(account.equity) if account else 0.0
            self.daily_realized_pnl = 0.0
            self.daily_loss_pct     = 0.0
            log.info(f"[DAYANCHOR] New day {today_str} | start equity={self.day_start_equity:.2f}")

        # Recompute realized PnL from history deals since day start
        from datetime import timedelta
        day_start = datetime(now.year, now.month, now.day, 0, 0, 0,
                             tzinfo=timezone.utc)
        deals = mt5.history_deals_get(day_start, now)
        if deals is None:
            return
        realized = sum(
            float(getattr(d, "profit", 0.0)) +
            float(getattr(d, "commission", 0.0)) +
            float(getattr(d, "swap", 0.0)) +
            float(getattr(d, "fee", 0.0))
            for d in deals
            if getattr(d, "entry", -1) == mt5.DEAL_ENTRY_OUT   # closed-leg deals only
        )
        self.daily_realized_pnl = realized

        account = mt5.account_info()
        if account and self.day_start_equity > 0.0:
            realized_loss_pct = max(0.0, -realized / self.day_start_equity)
            # Include floating loss for conservative breaker
            floating_dd_pct = max(0.0,
                (self.day_start_equity - float(account.equity)) / self.day_start_equity)
            self.daily_loss_pct = max(realized_loss_pct, floating_dd_pct)

            if self.daily_loss_pct >= MAX_DAILY_LOSS:
                log.warning(f"[DAYANCHOR] Daily loss {self.daily_loss_pct:.2%} >= "
                            f"{MAX_DAILY_LOSS:.2%} — breaker active")

    # ── Position cache ─────────────────────────────────────────────────────

    def update_position_cache(self) -> None:
        now = time.time()
        if now - self.last_position_poll < POSITION_CACHE_INTERVAL:
            return
        self.last_position_poll = now

        terminal_positions = {
            int(p.ticket): {
                "symbol":          p.symbol,
                "direction":       1 if p.type == mt5.POSITION_TYPE_BUY else -1,
                "volume":          float(p.volume),
                "entry_price":     float(p.price_open),
                "timestamp_opened": float(p.time),
                "is_optimistic":   False,
            }
            for p in (mt5.positions_get(symbol=SYMBOL) or [])
        }

        resolved: Dict[int, Any] = {}
        for ticket, pos in list(self.cached_positions.items()):
            if ticket in terminal_positions:
                resolved[ticket] = terminal_positions[ticket]
                if pos.get("is_optimistic"):
                    log.info(f"[CACHE] Optimistic ticket {ticket} confirmed by terminal")
            elif pos.get("is_optimistic"):
                age = now - pos.get("timestamp_opened", now)
                if age < OPTIMISTIC_TTL_S:
                    resolved[ticket] = pos   # keep alive during propagation window
                else:
                    log.warning(f"[CACHE] Optimistic {ticket} failed to propagate in {OPTIMISTIC_TTL_S}s")

        for ticket, pos in terminal_positions.items():
            if ticket not in resolved:
                resolved[ticket] = pos

        self.cached_positions = resolved

    # ── Bar registry ───────────────────────────────────────────────────────

    def prune_bar_registry(self, current_bar_time: int) -> None:
        cutoff = current_bar_time - BAR_REGISTRY_RETENTION_S
        before = len(self.bar_registry)
        self.bar_registry = {k: v for k, v in self.bar_registry.items()
                             if k[2] >= cutoff}
        pruned = before - len(self.bar_registry)
        if pruned:
            log.debug(f"[REGISTRY] Pruned {pruned} stale bar records")

    # ── Startup rehydration ────────────────────────────────────────────────

    def system_rehydration_barrier(self) -> None:
        log.info(f"[BOOT] Starting {NAME} v{VERSION}...")

        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(f"Symbol select failed: {SYMBOL}")
        self._mt5_ready = True

        if not self.enforce_terminal_watchdog():
            raise RuntimeError("[BOOT] Terminal or feed not ready")

        # Restore risk context
        ctx = self.state_db.load_system_context()
        self.consecutive_losses = ctx.get("consecutive_losses", 0)
        self.daily_loss_pct     = ctx.get("daily_realized_loss_pct", 0.0)

        # Restore telemetry
        hist = self.state_db.load_historical_telemetry(limit=20)
        self.telemetry.latency_ms   = hist.get("latency", [])
        self.telemetry.slippage_pts = hist.get("slippage", [])
        self.current_eqd            = hist.get("avg_eqd", 0.0)

        # Load toxic state-signature blocklist
        try:
            report = self.analytics.generate_report(days=30)
            if report.get("status") == "PROFILED":
                self.toxic_hashes = report.get("toxic_state_signatures", [])
                log.info(f"[BOOT] Toxic blocklist: {len(self.toxic_hashes)} signatures")
        except Exception as e:
            log.warning(f"[BOOT] Analytics skip: {e}")

        # Position reconciliation
        terminal_pos = {
            int(p.ticket): {
                "symbol": p.symbol, "volume": float(p.volume),
                "direction": 1 if p.type == mt5.POSITION_TYPE_BUY else -1,
                "entry_price": float(p.price_open),
                "timestamp_opened": float(p.time),
            }
            for p in (mt5.positions_get(symbol=SYMBOL) or [])
        }
        self.cached_positions = terminal_pos

        recon = self.state_db.reconcile_state_matrices(terminal_pos)
        if recon["status"] != "SYNCHRONIZED":
            log.warning(f"[BOOT] State drift: {recon['status']}")
            for action in recon["actions_required"]:
                tid = action["ticket_id"]
                if action["action"] == "PURGE_STALE_RECORD":
                    self.state_db.remove_stealth_position(tid)
                elif action["action"] == "FORCE_IMPORT_REBUILD":
                    self.state_db.register_stealth_position(tid, action["payload"])

        # Reconcile trade-memory rows stuck 'open' (e.g. after a crash):
        # if the position is gone from the terminal, close the row from
        # broker history or flag it orphaned so tier stats stay honest.
        for stale_ticket in self.memory.get_open_tickets():
            if stale_ticket in terminal_pos:
                continue
            realized = self._realized_pnl_for_position(stale_ticket, retries=1)
            if realized is not None:
                self.memory.close_trade_by_ticket(
                    mt5_ticket=stale_ticket, exit_price=0.0,
                    pnl_pips=0.0, pnl_usd=realized,
                    outcome="loss" if realized < 0 else "win")
                log.info(f"[BOOT] Recovered stale trade {stale_ticket} "
                         f"from history: ${realized:+.2f}")
            else:
                self.memory.mark_trade_orphaned(stale_ticket)
                log.warning(f"[BOOT] Trade {stale_ticket} orphaned — "
                            f"no close deal in broker history")

        # P4: rebuild daily loss from broker history on startup
        self._rebuild_daily_loss_from_history()

        log.info("[BOOT] Rehydration complete — loop starting")

    # ── Tick telemetry (stealth exits) ─────────────────────────────────────

    def process_tick_telemetry(self, tick: Any) -> None:
        db_positions = self.state_db.get_all_active_stealth_positions()
        if not db_positions:
            return

        sym_info = mt5.symbol_info(SYMBOL)
        if sym_info is None:
            return
        tick_size = sym_info.trade_tick_size

        for ticket, pos in list(db_positions.items()):
            if ticket not in self.cached_positions:
                log.info(f"[TELEMETRY] Ticket {ticket} closed out-of-band — purging")
                self.state_db.remove_stealth_position(ticket)
                continue

            if pos.get("operational_state") == "PENDING_CLOSE":
                continue

            direction   = int(pos["direction"])
            entry_price = float(pos["entry_price"])

            loss_pts   = ((entry_price - tick.bid) / tick_size if direction == 1
                          else (tick.ask - entry_price) / tick_size)
            profit_pts = ((tick.bid - entry_price) / tick_size if direction == 1
                          else (entry_price - tick.ask) / tick_size)

            # Adaptive stealth decay
            bars_elapsed = int((time.time() - float(pos["timestamp_opened"]))
                               / (CYCLE_MINUTES * 60))
            decay = compute_adaptive_stealth_decay(bars_elapsed, self.current_eqd)
            adj_sl = max(pos["virtual_sl_points"] * 0.75,
                         pos["virtual_sl_points"] * decay)
            adj_tp = max(pos["virtual_tp_points"] * 0.65,
                         pos["virtual_tp_points"] * decay)

            if loss_pts >= adj_sl or profit_pts >= adj_tp:
                if not self.state_db.lock_position_for_closure(ticket):
                    continue

                log.warning(f"[STEALTH] Limit reached on {ticket} — closing")
                close_type  = mt5.ORDER_TYPE_SELL if direction == 1 else mt5.ORDER_TYPE_BUY
                close_price = tick.bid if direction == 1 else tick.ask

                result = mt5.order_send({
                    "action":       mt5.TRADE_ACTION_DEAL,
                    "symbol":       SYMBOL,
                    "volume":       float(pos["volume"]),
                    "type":         close_type,
                    "position":     ticket,
                    "price":        close_price,
                    "deviation":    15,
                    "type_filling": self._get_filling_mode(SYMBOL),
                    "type_time":    mt5.ORDER_TIME_GTC,
                })

                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.state_db.remove_stealth_position(ticket)
                    self.cached_positions.pop(ticket, None)

                    is_loss    = loss_pts >= adj_sl
                    exit_price = float(getattr(result, "price", 0.0)) or close_price
                    tick_value = float(getattr(sym_info, "trade_tick_value", 0.0)) or 1.0

                    # P3: broker-authoritative PnL (None until close deal lands)
                    realized_usd = self._realized_pnl_for_position(ticket)
                    if realized_usd is None:
                        # Synthetic estimate: points × tick_value × lots
                        pnl_pts      = -loss_pts if is_loss else profit_pts
                        realized_usd = pnl_pts * tick_value * float(pos["volume"])
                        log.warning(f"[STEALTH] {ticket} PnL from history unavailable "
                                    f"— using synthetic {realized_usd:.2f}")
                    else:
                        is_loss = realized_usd < 0  # broker truth overrides tick math
                        pnl_pts = realized_usd / max(
                            tick_value * float(pos["volume"]), 1e-10)

                    self.consecutive_losses = (self.consecutive_losses + 1
                                               if is_loss else 0)
                    self.state_db.save_system_context(
                        self.consecutive_losses, self.daily_loss_pct)

                    # Log to trade memory
                    if not self.memory.close_trade_by_ticket(
                        mt5_ticket=ticket,
                        exit_price=exit_price,
                        pnl_pips=pnl_pts,
                        pnl_usd=realized_usd,
                        outcome="loss" if is_loss else "win",
                    ):
                        log.warning(f"[STEALTH] {ticket} not found in trade memory")

                    # Feed the post-trade analytics ledger
                    tel = self.telemetry.current_metrics()
                    self.state_db.record_closed_trade(
                        ticket, pos,
                        realized_pnl_points=pnl_pts,
                        avg_latency_ms=tel["rolling_avg_latency_ms"],
                        avg_slippage_points=tel["rolling_avg_slippage_points"],
                        final_eqd=tel["execution_quality_degradation"],
                    )
                    log.info(f"[STEALTH] {ticket} closed {'LOSS' if is_loss else 'WIN'} "
                             f"pnl=${realized_usd:+.2f}")
                else:
                    err = getattr(result, "retcode", "TIMEOUT")
                    log.error(f"[STEALTH] Close failed on {ticket}: {err} — releasing lock")
                    self.state_db.release_position_lock(ticket)

    # ── Bar allocation cycle ───────────────────────────────────────────────

    def process_bar_allocation_cycle(self) -> None:
        now = time.time()

        # ── Entry guards ───────────────────────────────────────────────────
        log.info(f"[BAR] positions={len(self.cached_positions)} max={MAX_OPEN_POSITIONS}")
        if len(self.cached_positions) >= MAX_OPEN_POSITIONS:
            log.warning("[BAR] BLOCKED: max positions")
            return

        if now - self.last_entry_ts < MIN_COOLDOWN_S:
            log.warning(f"[BAR] BLOCKED: cooldown {now - self.last_entry_ts:.0f}s / {MIN_COOLDOWN_S}s")
            return

        # ── Kill hours ─────────────────────────────────────────────────────
        from datetime import datetime, timezone
        if datetime.now(timezone.utc).hour in KILL_HOURS_UTC:
            log.info("[KILL_HOUR] Skipping allocation")
            return

        # ── Market data ────────────────────────────────────────────────────
        sym_info = mt5.symbol_info(SYMBOL)
        account  = mt5.account_info()
        log.info(f"[BAR] sym_info={'OK' if sym_info else 'NONE'} account={'OK' if account else 'NONE'}")
        if sym_info is None or account is None:
            log.error("[BAR] BLOCKED: Cannot fetch symbol/account info")
            return

        # ── Spread firewall ────────────────────────────────────────────────
        if sym_info.spread > MAX_SPREAD_POINTS:
            log.warning(f"[SPREAD] {sym_info.spread} > {MAX_SPREAD_POINTS} — abort")
            return

        # ── OHLCV data ─────────────────────────────────────────────────────
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, 500)
        log.info(f"[BAR] rates={'None' if rates is None else len(rates)} bars")
        if rates is None or len(rates) < 200:
            log.error("[BAR] BLOCKED: insufficient bars")
            return

        import pandas as pd
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").rename(columns={"tick_volume": "volume"})

        current_bar_time = int(rates[-1]["time"])
        self.prune_bar_registry(current_bar_time)

        # ── Feature snapshot ───────────────────────────────────────────────
        log.info(f"[BAR] Calling feature engine on {len(df)} bars...")
        try:
            snap = self.features.generate_snapshot(df)
            log.info(f"[BAR] Feature snapshot OK: regime={snap.get('regime')} session={snap.get('session')}")
        except Exception as e:
            import traceback
            log.error(f"[BAR] Feature engine CRASH: {e}")
            log.error(traceback.format_exc())
            return

        # ── Performance memory ─────────────────────────────────────────────
        mem_stats = self.memory.get_stats()
        perf_ctx  = {
            "current_drawdown_pct": self.daily_loss_pct,
            "consecutive_losses":   self.consecutive_losses,
            "eqd_coefficient":      self.current_eqd,
            "win_rate_calibrated":  mem_stats.get("win_rate", 0.0),
        }

        # ── Edge engine (directional mask) ─────────────────────────────────
        masked = self.edge.process_state(snap, perf_ctx)

        direction = int(masked.get("permitted_direction", 0))
        if direction == 0:
            return

        # ── Bar execution lock ─────────────────────────────────────────────
        bar_key = (SYMBOL, direction, current_bar_time)
        if bar_key in self.bar_registry:
            log.info(f"[BAR] Direction {direction} already executed on this bar")
            return

        # ── Toxic state soft-block (quantized signature, recurs across bars) ─
        toxicity_scalar = 1.0
        if snap.get("state_signature") in self.toxic_hashes:
            log.warning("[TOXIC] State signature in blocklist — 50% aggression penalty")
            toxicity_scalar = 0.50

        # ── Claude allocation ──────────────────────────────────────────────
        recent_losses  = self.memory.get_recent_losses(RECENT_LOSSES_N)
        try:
            allocation = self.allocator.allocate_cycle(
                masked,
                recent_losses=recent_losses,
            )
            allocation["aggression_multiplier"] = round(
                allocation["aggression_multiplier"] * toxicity_scalar, 3)
        except Exception as e:
            log.warning(f"[ALLOC] Exception: {e} — local fallback")
            edge_score = float(masked.get("edge_quality_score", 0.3))
            sl_b = snap.get("allowed_sl_atr_range", [1.0, 2.0])
            tp_b = snap.get("allowed_tp_atr_range", [2.0, 4.0])
            allocation = {
                "status":               "SUCCESS_LOCAL_AUTONOMOUS_FALLBACK",
                "snapshot_hash":        snap.get("snapshot_hash", ""),
                "permitted_direction":  direction,
                "execution_profile":    "CONSERVATIVE",
                "aggression_multiplier": round(edge_score * 0.4 * toxicity_scalar, 3),
                "sl_atr_target":        sl_b[0] + (sl_b[1]-sl_b[0]) * 0.8,
                "tp_atr_target":        tp_b[0] + (tp_b[1]-tp_b[0]) * 0.4,
                "allocation_rationale": "Local autonomous fallback.",
            }

        if allocation.get("execution_profile") == "FLAT":
            return

        # ── P4: rebuild daily loss before risk check ───────────────────────
        self._rebuild_daily_loss_from_history()

        # ── Risk engine (final authority) ──────────────────────────────────
        account_state = {
            "balance":                      account.balance,
            "equity":                       account.equity,
            "free_margin":                  account.margin_free,
            "leverage":                     account.leverage,
            "daily_realized_loss_pct":      self.daily_loss_pct,
            "current_spread_points":        sym_info.spread,
            "min_lot_limit":                sym_info.volume_min,
            "max_lot_limit":                sym_info.volume_max,
            "SYMBOL_MARGIN_INITIAL":        sym_info.margin_initial,
            "SYMBOL_TRADE_CONTRACT_SIZE":   sym_info.trade_contract_size,
            "SYMBOL_TRADE_TICK_VALUE":      sym_info.trade_tick_value,
            "SYMBOL_TRADE_TICK_SIZE":       sym_info.trade_tick_size,
            # P2: explicit risk context — risk law reads from here, not signal layer
            "consecutive_losses":           self.consecutive_losses,
            "execution_quality_degradation": self.current_eqd,
        }

        auth = self.risk.authorize_execution(allocation, account_state, masked, mem_stats)

        if auth["status"] != "APPROVED":
            log.info(f"[RISK] Rejected: {auth['status']}")
            return

        # ── Execute ────────────────────────────────────────────────────────
        params    = auth["order_parameters"]
        order_type = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL
        price      = (mt5.symbol_info_tick(SYMBOL).ask if direction == 1
                      else mt5.symbol_info_tick(SYMBOL).bid)

        # P1: capture both wall-clock (for DB/logs) and perf_counter (for latency)
        t_sent_wall = time.time()
        t_sent_perf = time.perf_counter()

        # P5: emergency broker SL as disaster fallback (Python/VPS crash protection)
        #     Virtual SL remains primary exit; broker SL is only a safety net
        tick_sz = sym_info.trade_tick_size
        if USE_BROKER_EMERGENCY_SL:
            emg_sl_pts  = int(params["virtual_sl_points"] * EMERGENCY_SL_MULTIPLIER)
            broker_sl   = (price - emg_sl_pts * tick_sz if direction == 1
                           else price + emg_sl_pts * tick_sz)
        else:
            broker_sl = 0.0

        filling = self._get_filling_mode(SYMBOL)
        log.info(f"[EXEC] Sending: {order_type} {params['volume']}lot @ {price:.2f} "
                 f"emg_sl={broker_sl:.2f} filling={filling}")
        result = mt5.order_send({
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       SYMBOL,
            "volume":       float(params["volume"]),
            "type":         order_type,
            "price":        price,
            "deviation":    20,
            "sl":           round(broker_sl, 2),
            "tp":           0.0,
            "type_filling": filling,
            "type_time":    mt5.ORDER_TIME_GTC,
            "comment":      f"Gyna:{snap.get('snapshot_hash','')[:8]}",
        })

        # P1: capture fill perf_counter immediately after broker response
        t_fill_perf = time.perf_counter()
        t_fill_wall = time.time()

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = getattr(result, "retcode", "TIMEOUT")
            log.error(f"[EXEC] Order failed: {err}")
            self.telemetry.log_transaction(
                {**auth, "timestamp_sent": t_sent_wall,
                 "timestamp_sent_perf": t_sent_perf},
                {"execution_successful": False})
            return

        ticket     = int(result.order)
        fill_price = float(result.price)
        slippage   = ((fill_price - price) / tick_sz if direction == 1
                      else (price - fill_price) / tick_sz)

        # P1: pass both wall-clock and perf timestamps — telemetry uses perf for precision
        broker_receipt = {
            "execution_successful":  True,
            "ticket_id":             ticket,
            "fill_price":            fill_price,
            "timestamp_sent":        t_sent_wall,
            "timestamp_fill":        t_fill_wall,
            "timestamp_sent_perf":   t_sent_perf,   # P1: high-precision latency
            "timestamp_fill_perf":   t_fill_perf,   # P1: high-precision latency
            "tick_size":             tick_sz,
        }
        tel_profile = self.telemetry.log_transaction(
            {**auth, "timestamp_sent": t_sent_wall, "timestamp_sent_perf": t_sent_perf},
            broker_receipt)
        self.current_eqd = tel_profile["metrics"]["execution_quality_degradation"]

        # P6: telemetry buffer uses perf_counter delta (not wall-clock) for latency
        latency_ms = (t_fill_perf - t_sent_perf) * 1000.0
        self.state_db.buffer_telemetry_metric(
            latency_ms,
            slippage,
            self.current_eqd,
        )

        # Stealth position registration — carries entry context so the
        # closed-trades ledger can profile regime/session/spread later
        pos_details = {
            "symbol":              SYMBOL,
            "direction":           direction,
            "volume":              float(params["volume"]),
            "entry_price":         fill_price,
            "virtual_sl_points":   params["virtual_sl_points"],
            "virtual_tp_points":   params["virtual_tp_points"],
            "snapshot_hash":       auth.get("snapshot_hash", ""),
            "state_signature":     snap.get("state_signature", ""),
            "regime":              snap.get("regime"),
            "session":             snap.get("session"),
            "execution_profile":   allocation.get("execution_profile"),
            "entry_spread_points": float(sym_info.spread),
            "timestamp_opened":    t_sent_wall,
        }
        self.state_db.register_stealth_position(ticket, pos_details)

        # Optimistic cache injection
        self.cached_positions[ticket] = {**pos_details, "is_optimistic": True}

        # Bar registry lock
        self.bar_registry[bar_key] = t_sent_wall
        self.last_entry_ts = t_sent_wall

        # Trade memory log
        trade_id = self.memory.log_trade({
            "symbol":       SYMBOL,
            "direction":    "BUY" if direction == 1 else "SELL",
            "entry_price":  fill_price,
            "volume":       float(params["volume"]),
            "sl":           fill_price - params["virtual_sl_points"] * tick_sz * direction,
            "tp":           fill_price + params["virtual_tp_points"] * tick_sz * direction,
            "rationale":    allocation.get("allocation_rationale", ""),
            "regime":       snap.get("regime"),
            "session":      snap.get("session"),
            "rsi":          snap.get("rsi_14"),
            "macd_hist":    snap.get("macd_hist"),
            "bb_position":  snap.get("bb_position"),
            "hma_trend":    snap.get("hma_trend"),
            "atr":          snap.get("atr_14"),
            "hhll_bias":    snap.get("hhll_bias"),
            "confidence":   masked.get("edge_quality_score"),
            "risk_tier":    auth.get("risk_tier"),
            "risk_pct":     auth.get("risk_pct"),
            "mt5_ticket":   ticket,
            "outcome":      "open",
        })

        log.info(
            f"[EXEC] FILLED Ticket={ticket} {'BUY' if direction==1 else 'SELL'} "
            f"{params['volume']}lot @ {fill_price:.2f} | "
            f"SL={params['virtual_sl_points']}pts TP={params['virtual_tp_points']}pts | "
            f"Tier={auth.get('risk_tier')} risk={auth.get('risk_pct',0)*100:.2f}% | "
            f"EQD={self.current_eqd:.3f} slip={slippage:+.1f}pts"
        )

    # ── Filling mode ───────────────────────────────────────────────────────

    def _get_filling_mode(self, symbol: str) -> int:
        try:
            info = mt5.symbol_info(symbol)
            if info is None:
                return mt5.ORDER_FILLING_IOC
            modes = info.filling_mode
            if modes & mt5.SYMBOL_FILLING_FOK:  return mt5.ORDER_FILLING_FOK
            if modes & mt5.SYMBOL_FILLING_IOC:  return mt5.ORDER_FILLING_IOC
            return mt5.ORDER_FILLING_RETURN
        except Exception:
            return mt5.ORDER_FILLING_IOC

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self.system_rehydration_barrier()
        log.info("[IGNITION] Gyna live — 50ms monotonic loop active")

        cadence       = LOOP_CADENCE_S
        next_frame    = time.perf_counter()
        bar_interval  = CYCLE_MINUTES * 60.0

        consecutive_frame_errors = 0
        try:
            while True:
                now_perf = time.perf_counter()

                if now_perf >= next_frame:
                    # Crash-proof frame: one bad cycle must not kill the bot
                    # while positions may be open (virtual SL/TP lives here).
                    try:
                        # Watchdog
                        if not self.enforce_terminal_watchdog():
                            next_frame = now_perf + cadence
                            continue

                        # Position cache
                        self.update_position_cache()

                        # Tick telemetry (stealth exits)
                        tick = mt5.symbol_info_tick(SYMBOL)
                        if tick is not None:
                            self.process_tick_telemetry(tick)

                        # Bar allocation (monotonic gate)
                        elapsed = time.time() - self.last_allocation_ts
                        if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                            log.info(f"[LOOP] Waiting for bar: {elapsed:.0f}s / {bar_interval:.0f}s | positions={len(self.cached_positions)}")
                        if elapsed >= bar_interval:
                            log.info("[LOOP] BAR CYCLE FIRING NOW")
                            self.process_bar_allocation_cycle()
                            self.last_allocation_ts = time.time()

                        consecutive_frame_errors = 0
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        import traceback
                        consecutive_frame_errors += 1
                        log.error(f"[LOOP] Frame error "
                                  f"#{consecutive_frame_errors}:\n"
                                  f"{traceback.format_exc()}")
                        if consecutive_frame_errors >= 100:
                            log.critical("[LOOP] 100 consecutive frame errors "
                                         "— structural failure, shutting down")
                            raise
                        time.sleep(1.0)

                    next_frame += cadence
                    # Drift correction
                    if now_perf > next_frame + cadence:
                        next_frame = now_perf + cadence
                else:
                    time.sleep(0.001)

        except KeyboardInterrupt:
            log.info("[SHUTDOWN] Manual stop — flushing buffers...")
        finally:
            self.state_db.flush_telemetry_buffer()
            mt5.shutdown()
            log.info("[SHUTDOWN] Gyna offline. Telemetry synchronized.")


if __name__ == "__main__":
    GynaSystemOrchestrator().run()
