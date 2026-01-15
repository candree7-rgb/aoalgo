import sys
import time
import datetime
import threading
import logging

from config import (
    DISCORD_TOKEN, CHANNEL_ID,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW, ACCOUNT_TYPE,
    CATEGORY, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY, TC_MAX_LAG_SEC,
    STATE_FILE, DRY_RUN, LOG_LEVEL
)
from bybit_v5 import BybitV5
from discord_reader import DiscordReader
from signal_parser import parse_signal, signal_hash, is_signal_still_valid
from state import load_state, save_state, utc_day_key
from trade_engine import TradeEngine
import db_export

# ----- Hourly Polling Config -----
SIGNAL_CHECK_DELAYS = [4, 8, 12, 16]  # Seconds after full hour to check for signals
TRADE_MGMT_INTERVAL = 30              # Seconds between trade management checks


def setup_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log


def get_utc_now():
    """Get current UTC time."""
    return datetime.datetime.now(datetime.timezone.utc)


def seconds_until_next_hour():
    """Calculate seconds until next full hour."""
    now = get_utc_now()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    return (next_hour - now).total_seconds()


def check_signal_status(discord, engine, st, log):
    """Check if any pending trades have been cancelled by the signal source."""
    pending_trades = [
        tr for tr in st.get("open_trades", {}).values()
        if tr.get("status") == "pending" and tr.get("discord_msg_id")
    ]

    for tr in pending_trades:
        try:
            msg_id = tr.get("discord_msg_id")
            if not msg_id:
                continue

            msg = discord.fetch_message(str(msg_id))
            if not msg:
                continue

            txt = discord.extract_text(msg)
            if not txt:
                continue

            # Check if signal is still valid (ACTIVE status)
            if not is_signal_still_valid(txt):
                log.warning(f"❌ Signal no longer ACTIVE for {tr['symbol']} - cancelling entry")
                entry_oid = tr.get("entry_order_id")
                if entry_oid:
                    engine.cancel_entry(tr["symbol"], entry_oid)
                tr["status"] = "cancelled"
                tr["exit_reason"] = "signal_cancelled"
                save_state(STATE_FILE, st)

        except Exception as e:
            log.debug(f"Signal status check failed for {tr.get('symbol')}: {e}")


def main():
    log = setup_logger()

    # Basic env checks
    missing = [k for k, v in {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "CHANNEL_ID": CHANNEL_ID,
        "BYBIT_API_KEY": BYBIT_API_KEY,
        "BYBIT_API_SECRET": BYBIT_API_SECRET,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing ENV(s): {', '.join(missing)}")

    st = load_state(STATE_FILE)

    bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET, demo=BYBIT_DEMO, recv_window=RECV_WINDOW)
    discord = DiscordReader(DISCORD_TOKEN, CHANNEL_ID)
    engine = TradeEngine(bybit, st, log)

    log.info("=" * 58)
    mode_str = " | DRY_RUN" if DRY_RUN else ""
    mode_str += " | DEMO" if BYBIT_DEMO else ""
    mode_str += " | TESTNET" if BYBIT_TESTNET else ""
    log.info("AO Algo → Bybit Bot (Hourly Signals)" + mode_str)
    log.info("=" * 58)
    log.info(f"Config: CATEGORY={CATEGORY}, QUOTE={QUOTE}, LEVERAGE={LEVERAGE}x")
    log.info(f"Config: RISK_PCT={RISK_PCT}%, MAX_CONCURRENT={MAX_CONCURRENT_TRADES}, MAX_DAILY={MAX_TRADES_PER_DAY}")
    log.info(f"Config: Signal checks at XX:00:{SIGNAL_CHECK_DELAYS}")
    log.info(f"Config: Trade management every {TRADE_MGMT_INTERVAL}s")
    log.info(f"Config: DRY_RUN={DRY_RUN}, LOG_LEVEL={LOG_LEVEL}")

    # Initialize database if enabled
    if db_export.is_enabled():
        log.info("📊 Initializing database...")
        if db_export.init_database():
            log.info("✅ Database ready")
        else:
            log.warning("⚠️ Database initialization failed (continuing without DB export)")

    # Startup sync - check for orphaned positions
    engine.startup_sync()

    # Heartbeat tracking
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300  # Log heartbeat every 5 minutes

    # Trade management tracking
    last_trade_mgmt = time.time()

    # Signal check tracking (for hourly polling)
    signal_checks_done = set()  # Track which delays we've checked this hour
    last_check_hour = -1        # Track which hour we last checked

    # ----- WS thread -----
    ws_err = {"err": None}

    def on_execution(ev):
        try:
            engine.on_execution(ev)
        except Exception as e:
            log.warning(f"WS execution handler error: {e}")

    def on_order(ev):
        return

    def on_ws_error(err):
        ws_err["err"] = err
        log.debug(f"WS reconnecting: {err}")

    def ws_loop():
        while True:
            try:
                bybit.run_private_ws(on_execution=on_execution, on_order=on_order, on_error=on_ws_error)
            except Exception as e:
                on_ws_error(e)
            time.sleep(3)

    t = threading.Thread(target=ws_loop, daemon=True)
    t.start()

    # ----- helper: limits -----
    def trades_today() -> int:
        return int(st.get("daily_counts", {}).get(utc_day_key(), 0))

    def inc_trades_today():
        k = utc_day_key()
        st.setdefault("daily_counts", {})[k] = int(st.get("daily_counts", {}).get(k, 0)) + 1

    def can_take_new_trade() -> bool:
        active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
        if len(active) >= MAX_CONCURRENT_TRADES:
            return False
        if trades_today() >= MAX_TRADES_PER_DAY:
            return False
        return True

    def check_for_new_signals():
        """Fetch and process new signals from Discord."""
        if not can_take_new_trade():
            return

        after = st.get("last_discord_id")
        log.debug(f"Polling Discord (after={after})...")

        try:
            msgs = discord.fetch_after(after, limit=50)
        except Exception as e:
            log.warning(f"Discord fetch failed: {e}")
            return

        log.debug(f"Fetched {len(msgs)} message(s) from Discord")
        msgs_sorted = sorted(msgs, key=lambda m: int(m.get("id", "0")))
        max_seen = int(after or 0)
        found_signal = False

        for m in msgs_sorted:
            mid = int(m.get("id", "0"))
            max_seen = max(max_seen, mid)

            # Ignore very old messages
            ts = discord.message_timestamp_unix(m)
            age = time.time() - ts if ts else 0
            if ts and age > TC_MAX_LAG_SEC:
                log.debug(f"Skipping old message (age={age:.0f}s > {TC_MAX_LAG_SEC}s)")
                continue

            txt = discord.extract_text(m)
            if not txt:
                continue

            log.debug(f"Message {mid}: {txt[:200]}...")

            sig = parse_signal(txt, quote=QUOTE)
            if not sig:
                if "SIGNAL" in txt.upper() and "LONG" in txt.upper() or "SHORT" in txt.upper():
                    log.warning(f"⚠️ Possible signal NOT parsed: {txt[:300]}...")
                continue

            log.info(f"📨 Signal parsed: {sig['symbol']} {sig['side'].upper()} @ {sig['trigger']}")
            found_signal = True

            sh = signal_hash(sig)
            seen = set(st.get("seen_signal_hashes", []))
            if sh in seen:
                log.debug(f"Signal {sig['symbol']} already seen, skipping")
                continue

            # Mark seen early
            seen.add(sh)
            st["seen_signal_hashes"] = list(seen)[-500:]

            trade_id = f"{sig['symbol']}|{sig['side']}|{int(time.time())}"
            log.info(f"🔄 Placing entry order for {sig['symbol']}...")
            oid = engine.place_conditional_entry(sig, trade_id)
            if not oid:
                log.warning(f"❌ Entry order failed for {sig['symbol']}")
                continue

            # Get current equity for risk tracking
            try:
                equity_now = bybit.wallet_equity(ACCOUNT_TYPE)
            except Exception:
                equity_now = 0

            # Store trade
            st.setdefault("open_trades", {})[trade_id] = {
                "id": trade_id,
                "symbol": sig["symbol"],
                "order_side": "Sell" if sig["side"] == "sell" else "Buy",
                "pos_side": "Short" if sig["side"] == "sell" else "Long",
                "trigger": float(sig["trigger"]),
                "tp_prices": sig.get("tp_prices") or [],
                "tp_splits": None,
                "dca_prices": [],  # No DCA for AO Algo
                "sl_price": sig.get("sl_price"),
                "entry_order_id": oid,
                "status": "pending",
                "placed_ts": time.time(),
                "base_qty": engine.calc_base_qty(sig["symbol"], float(sig["trigger"])),
                "raw": sig.get("raw", ""),
                "discord_msg_id": mid,
                "risk_pct": RISK_PCT,
                "risk_amount": round(equity_now * RISK_PCT / 100, 2) if equity_now > 0 else None,
                "equity_at_entry": round(equity_now, 2) if equity_now > 0 else None,
                "leverage": LEVERAGE,
            }
            inc_trades_today()
            log.info(f"🟡 ENTRY PLACED {sig['symbol']} {sig['side'].upper()} trigger={sig['trigger']} (id={trade_id})")

            # Stop if we hit limits mid-batch
            if not can_take_new_trade():
                break

        st["last_discord_id"] = str(max_seen) if max_seen else after
        save_state(STATE_FILE, st)
        return found_signal

    # ----- main loop -----
    log.info("🚀 Bot started. Waiting for signals at full hours...")

    while True:
        try:
            now = get_utc_now()
            current_hour = now.hour
            current_second = now.second

            # Reset signal checks at new hour
            if current_hour != last_check_hour:
                signal_checks_done = set()
                last_check_hour = current_hour
                if now.minute == 0 and current_second < 2:
                    log.info(f"⏰ New hour: {now.strftime('%H:%M')} UTC - preparing signal checks")

            # Heartbeat log every 5 minutes
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
                next_check = seconds_until_next_hour()
                log.info(f"💓 Heartbeat: {len(active)} active trade(s), {trades_today()} today, next signal check in {next_check:.0f}s")
                last_heartbeat = time.time()

            # ----- SIGNAL POLLING (only at full hour) -----
            if now.minute == 0:
                for delay in SIGNAL_CHECK_DELAYS:
                    if delay not in signal_checks_done and current_second >= delay:
                        log.info(f"🔍 Signal check #{SIGNAL_CHECK_DELAYS.index(delay)+1} at XX:00:{delay:02d}")
                        signal_checks_done.add(delay)

                        if can_take_new_trade():
                            found = check_for_new_signals()
                            if found:
                                log.info("✅ Signal found! Skipping remaining checks this hour.")
                                # Mark all remaining checks as done
                                signal_checks_done.update(SIGNAL_CHECK_DELAYS)
                                break
                        else:
                            active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
                            log.info(f"⏸️ Skipping signal check: {len(active)}/{MAX_CONCURRENT_TRADES} active, {trades_today()}/{MAX_TRADES_PER_DAY} today")

            # ----- TRADE MANAGEMENT (every TRADE_MGMT_INTERVAL seconds) -----
            if time.time() - last_trade_mgmt >= TRADE_MGMT_INTERVAL:
                last_trade_mgmt = time.time()

                # Maintenance tasks
                engine.cancel_expired_entries()
                engine.check_entry_order_validity()
                engine.cleanup_closed_trades()
                engine.check_tp_fills_fallback()
                engine.check_position_alerts()
                engine.log_daily_stats()

                # Check signal status for pending trades
                check_signal_status(discord, engine, st, log)

                # Entry-fill fallback (polling) and post-orders placement
                for tid, tr in list(st.get("open_trades", {}).items()):
                    if tr.get("status") == "pending":
                        sz, avg = engine.position_size_avg(tr["symbol"])
                        if sz > 0 and avg > 0:
                            tr["status"] = "open"
                            tr["entry_price"] = avg
                            tr["filled_ts"] = time.time()
                            log.info(f"✅ ENTRY (poll) {tr['symbol']} @ {avg}")

                    if tr.get("status") == "open" and not tr.get("post_orders_placed"):
                        engine.place_post_entry_orders(tr)

                save_state(STATE_FILE, st)

        except KeyboardInterrupt:
            log.info("Bye")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")
            time.sleep(3)

        # Short sleep - we check time frequently for precise hourly polling
        time.sleep(1)


if __name__ == "__main__":
    main()
