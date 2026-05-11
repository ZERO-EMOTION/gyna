import sqlite3
from datetime import datetime

class TradeMemory:
    def __init__(self, db_path="memory/gyna_trades.db"):
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._create_tables()

    def _create_tables(self):
        self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            volume REAL,
            rationale TEXT,
            regime TEXT,
            pnl REAL DEFAULT 0
        )''')
        self.conn.commit()

    def log_trade(self, data: dict):
        self.conn.execute('INSERT INTO trades (timestamp, symbol, direction, entry_price, volume, rationale, regime) VALUES (?,?,?,?,?,?,?)', (
            datetime.utcnow().isoformat(),
            data.get('symbol'),
            data.get('direction'),
            data.get('entry_price'),
            data.get('volume'),
            data.get('rationale'),
            data.get('regime', 'unknown')
        ))
        self.conn.commit()