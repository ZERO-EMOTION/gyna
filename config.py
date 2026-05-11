import os
from dotenv import load_dotenv
load_dotenv()

NAME = "Gyna"

# MT5
MT5_LOGIN = int(os.getenv("MT5_LOGIN", 0))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

# LLM
LLM_PROVIDER = "anthropic"  # anthropic, openai, groq
MODEL = "claude-3-5-sonnet-20240620"

# Trading params
SYMBOLS = ["EURUSD", "XAUUSD"]
TIMEFRAME = mt5.TIMEFRAME_M15
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03

print(f"✅ {NAME} config loaded")