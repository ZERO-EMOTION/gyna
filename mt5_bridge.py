"""
Gyna — mt5_bridge.py
Thin MT5 connection wrapper. Main execution logic lives in main_orchestrator.py.
This module used for standalone connection testing and simple queries.
Copyright © 2026 PARALLAX — JP × Claude. All rights reserved.
"""
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone

from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, NAME, TIMEFRAME_STR


# Map string timeframe to MT5 constant
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}


class MT5Bridge:
    def __init__(self):
        self.connected = False

    def connect(self) -> bool:
        if not mt5.initialize(login=int(MT5_LOGIN),
                              password=MT5_PASSWORD,
                              server=MT5_SERVER):
            print(f"MT5 initialize failed: {mt5.last_error()}")
            return False
        self.connected = True
        print(f"[OK] {NAME} connected to MT5 | Server: {MT5_SERVER}")
        return True

    def disconnect(self):
        mt5.shutdown()
        self.connected = False

    def get_rates(self, symbol: str, timeframe: str = None, bars: int = 500):
        tf = TF_MAP.get(timeframe or TIMEFRAME_STR, mt5.TIMEFRAME_M1)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        return df

    def get_account_info(self) -> dict:
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance":    info.balance,
            "equity":     info.equity,
            "margin":     info.margin,
            "free_margin": info.margin_free,
            "leverage":   info.leverage,
        }

    def get_positions(self, symbol: str = None) -> list:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []
        return list(positions)

    def get_filling_mode(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        modes = info.filling_mode
        if modes & mt5.SYMBOL_FILLING_FOK: return mt5.ORDER_FILLING_FOK
        if modes & mt5.SYMBOL_FILLING_IOC: return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN
