import time
import schedule
from config import *
from mt5_bridge import MT5Bridge
from memory.trade_log import TradeMemory

print(f"🚀 Starting {NAME}...")

bridge = MT5Bridge()
memory = TradeMemory()

if not bridge.connect():
    exit(1)

def trading_cycle():
    print(f"[{datetime.now()}] Running analysis cycle...")
    for symbol in SYMBOLS:
        df = bridge.get_rates(symbol)
        if df is not None:
            print(f"  {symbol}: Loaded {len(df)} bars")
            # TODO: Add LLM reasoning here
            # For now, just log
            memory.log_trade({
                'symbol': symbol,
                'direction': 'HOLD',
                'entry_price': df['close'].iloc[-1],
                'volume': 0.01,
                'rationale': 'Initial cycle - memory building'
            })

schedule.every(5).minutes.do(trading_cycle)

print("Gyna is running. Press Ctrl+C to stop.")
while True:
    schedule.run_pending()
    time.sleep(10)