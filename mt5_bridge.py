import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime

class MT5Bridge:
    def __init__(self):
        self.connected = False

    def connect(self):
        if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            print("MT5 initialize failed:", mt5.last_error())
            return False
        self.connected = True
        print(f"✅ {NAME} connected to MT5")
        return True

    def get_rates(self, symbol, timeframe=mt5.TIMEFRAME_M15, bars=1000):
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df

    def send_order(self, symbol, order_type, volume, sl=0, tp=0, comment="Gyna"):
        tick = mt5.symbol_info_tick(symbol)
        if order_type.upper() == "BUY":
            price = tick.ask
            order_type_mt = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            order_type_mt = mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type_mt,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 30,
            "magic": 202505,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return result

    def get_positions(self):
        return mt5.positions_get()