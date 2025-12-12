## Bybit Signal Executor (Discord -> Bybit V5)

### Features
- Parse Discord AO-style signals (Entry/TP/SL/DCA)
- Places Bybit Conditional Entry as LIMIT at exact signal price
- On entry fill:
  - Sets leverage
  - Places TPs (reduce-only)
  - Sets Stop Loss
  - Places DCA limit orders (increase position)
- On TP1 execution: moves SL to Breakeven (entry price) immediately via WebSocket (no polling delay)
- Expiration: cancels entry (and pending DCA/TP) after ENTRY_EXPIRATION_MIN
- Filter: skips placing entry if price already moved too far beyond entry (ENTRY_EXPIRY_PRICE_PCT)

### Run
1) Create .env from .env.example
2) `pip install -r requirements.txt`
3) `python main.py`

### Railway
- Add all env vars in Railway
- Start command: `python main.py`
