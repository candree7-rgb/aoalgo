import time, random
from datetime import datetime, timezone
from config import *
from state import load_state, save_state, incr_daily, get_daily
from discord_reader import fetch_messages, extract_text
from signal_parser import parse_signal
from bybit_v5 import BybitV5
from trade_engine import TradeEngine

def now_utc_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def trade_id(sig_hash: str) -> str:
    return f"AO:{sig_hash[:10]}:{now_utc_ts()}"

def main():
    if not DISCORD_TOKEN or not CHANNEL_ID:
        raise SystemExit("Missing DISCORD_TOKEN/CHANNEL_ID")
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        raise SystemExit("Missing BYBIT_API_KEY/BYBIT_API_SECRET")

    st = load_state()
    bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET)
    engine = TradeEngine(bybit, st)

    # Start WS in background thread (simple way)
    import threading
    t = threading.Thread(target=lambda: bybit.run_private_ws(engine.on_execution), daemon=True)
    t.start()

    last_id = st.get("last_discord_id")

    print("✅ Bot gestartet")
    while True:
        try:
            msgs = fetch_messages(last_id, limit=50)
            msgs = sorted(msgs, key=lambda m: int(m.get("id","0")))

            for m in msgs:
                mid = str(m.get("id","0"))
                if last_id and int(mid) <= int(last_id):
                    continue

                text = extract_text(m)
                sig = parse_signal(text, quote=QUOTE)

                if sig:
                    # dedupe
                    seen = set(st.get("seen_hashes", []))
                    if sig["hash"] in seen:
                        last_id = mid
                        continue
                    seen.add(sig["hash"])
                    st["seen_hashes"] = list(seen)[-800:]

                    # limits
                    if len(st.get("open_trades", {})) >= MAX_CONCURRENT_TRADES:
                        print("⛔ max concurrent erreicht -> skip")
                        last_id = mid
                        continue
                    if get_daily(st) >= MAX_TRADES_PER_DAY:
                        print("⛔ max trades/day erreicht -> skip")
                        last_id = mid
                        continue

                    tid = trade_id(sig["hash"])
                    symbol = sig["symbol"]
                    order_side = "Sell" if sig["side"] == "sell" else "Buy"
                    pos_side = "Short" if order_side == "Sell" else "Long"

                    # Here: you MUST set qty logic.
                    # For now, we store signal and place entry after you decide qty model.
                    # If you want: fixed USDT margin * leverage => qty via last price.
                    # Sag mir dein exaktes Risk-Modell (Equity% + leverage) und ich droppe dir die qty calc 1:1.

                    # Minimal: just register trade and place conditional entry with placeholder qty fix later.
                    oid = engine.place_conditional_entry(sig, client_trade_id=tid)
                    if not oid:
                        print(f"⚠️ skipped (too far / invalid) {symbol}")
                        last_id = mid
                        continue

                    st["open_trades"][tid] = {
                        "id": tid,
                        "symbol": symbol,
                        "order_side": order_side,
                        "pos_side": pos_side,
                        "created_ts": now_utc_ts(),
                        "expires_ts": now_utc_ts() + ENTRY_EXPIRATION_MIN*60,
                        "entry_trigger": sig["trigger"],
                        "entry_order_id": oid,
                        "entry_price": sig["trigger"],   # will be replaced with actual avg fill later
                        "tp_prices": sig["tps"],
                        "tp_splits": TP_SPLITS[:len(sig["tps"])],
                        "dca_prices": sig["dcas"],
                        "sl_price": sig.get("stop_loss"),
                        "tp1_order_id": None,
                        "sl_moved_to_be": False,
                        "post_orders_placed": False,
                    }

                    incr_daily(st)
                    print(f"📌 Placed conditional entry {symbol} ({order_side}) id={tid}")

                last_id = mid

            # Expiry cleanup
            now = now_utc_ts()
            for tid, tr in list(st.get("open_trades", {}).items()):
                if now >= tr["expires_ts"]:
                    print(f"⌛ Expired -> cancel {tid} {tr['symbol']}")
                    # Here you would cancel entry + open orders (best effort)
                    # (kept short: you can add cancel-all logic per symbol)
                    st["open_trades"].pop(tid, None)

            st["last_discord_id"] = last_id
            save_state(st)

        except Exception as e:
            print("❌ error:", e)

        time.sleep(POLL_SECONDS + random.uniform(0, 0.25))

if __name__ == "__main__":
    main()
