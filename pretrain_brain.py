"""
Gyna — pretrain_brain.py
Pre-train GynaBrain on historical M1 bars BEFORE it ever trades live.

Replays history through the exact live pipeline (FeatureEngine → styles →
EdgeEngine), simulates each signal's virtual SL/TP forward through the
subsequent bars (conservative: if both hit inside one bar, it counts the
LOSS), and feeds every labeled outcome to the brain in chronological order —
the same online updates it will do live, just thousands of them up front.

Usage (from the instance folder, so its .env and brain.json are used):
    cd instances\\XAUUSD
    python ..\\..\\pretrain_brain.py --bars 20000
    python ..\\..\\pretrain_brain.py --csv history.csv   # offline OHLCV file

CSV format: time,open,high,low,close,volume (UTC).
The learned brain is written to memory/brain.json in the current folder.

Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from config import SYMBOL
from feature_engine import FeatureEngine
from edge_engine import EdgeEngine
from learning_brain import GynaBrain, featurize

WARMUP_BARS   = 300    # bars needed before the first snapshot
TIMEOUT_BARS  = 240    # unresolved after 4h -> skip (no clean label)
COOLDOWN_BARS = 3      # mirror the live 3-min cooldown


def load_mt5_history(bars: int) -> pd.DataFrame:
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, bars)
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No history returned for {SYMBOL}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return (df.set_index("time")
              .rename(columns={"tick_volume": "volume"}))


def load_csv_history(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]]


def simulate_outcome(df: pd.DataFrame, i: int, direction: int,
                     entry: float, sl_price: float, tp_price: float):
    """Walk bars forward from i+1. Conservative: SL wins bar-internal ties."""
    for j in range(i + 1, min(i + 1 + TIMEOUT_BARS, len(df))):
        hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
        if direction == 1:
            if lo <= sl_price:
                return False
            if hi >= tp_price:
                return True
        else:
            if hi >= sl_price:
                return False
            if lo <= tp_price:
                return True
    return None   # unresolved — no label


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=20000,
                    help="M1 bars to pull from MT5 (default 20000 ~ 2 weeks)")
    ap.add_argument("--csv", type=str, default=None,
                    help="OHLCV CSV instead of MT5 (time,open,high,low,close,volume)")
    ap.add_argument("--stride", type=int, default=1,
                    help="Evaluate every Nth bar (speeds up long histories)")
    args = ap.parse_args()

    df = load_csv_history(args.csv) if args.csv else load_mt5_history(args.bars)
    print(f"[PRETRAIN] {SYMBOL}: {len(df)} bars "
          f"({df.index[0]} .. {df.index[-1]})")

    features = FeatureEngine(SYMBOL, "M1")
    edge     = EdgeEngine()          # respects the instance's SAFETY setting
    brain    = GynaBrain()           # loads/extends memory/brain.json in CWD
    perf     = {"current_drawdown_pct": 0.0, "consecutive_losses": 0,
                "win_rate_calibrated": 0.5, "eqd_coefficient": 0.0}

    signals = wins = 0
    next_allowed = 0
    t0 = time.time()

    for i in range(WARMUP_BARS, len(df) - 1, args.stride):
        if i < next_allowed:
            continue
        window = df.iloc[: i + 2]     # bar i is the last CLOSED bar
        try:
            snap = features.generate_snapshot(window)
        except Exception:
            continue
        masked = edge.process_state(snap, perf)
        direction = int(masked.get("permitted_direction", 0))
        if direction == 0:
            continue

        atr    = float(masked["atr_14"])
        sl_mid = sum(masked["allowed_sl_atr_range"]) / 2 * atr
        tp_mid = sum(masked["allowed_tp_atr_range"]) / 2 * atr
        entry  = float(df["close"].iloc[i])
        sl_p   = entry - sl_mid * direction
        tp_p   = entry + tp_mid * direction

        won = simulate_outcome(df, i + 1, direction, entry, sl_p, tp_p)
        if won is None:
            continue
        brain.update(featurize(masked), won=won)
        signals += 1
        wins    += int(won)
        next_allowed = i + COOLDOWN_BARS

        if signals % 100 == 0:
            print(f"[PRETRAIN] {signals} labeled signals "
                  f"(win rate {wins / signals:.1%}) "
                  f"| bar {i}/{len(df)} | {time.time() - t0:.0f}s")

    print(f"\n[PRETRAIN] DONE: {signals} outcomes learned "
          f"(win rate {wins / max(signals, 1):.1%})")
    print(f"[PRETRAIN] Brain now has {brain.n_updates} lifetime updates")
    print(f"[PRETRAIN] Top weights: {brain.top_weights(10)}")
    print(f"[PRETRAIN] Saved to {os.path.abspath(brain.path)}")


if __name__ == "__main__":
    main()
