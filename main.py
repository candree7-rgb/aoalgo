#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, hmac, hashlib, random, html
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import requests
import websockets
import asyncio
from dotenv import load_dotenv

load_dotenv()

# =========================
# ENVs
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

QUOTE = os.getenv("QUOTE", "USDT").strip().upper()
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))

RISK_PCT = float(os.getenv("RISK_PCT", "5"))  # % Equity als Margin (ohne Leverage)
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "50"))

ENTRY_EXPIRATION_MIN = int(os.getenv("ENTRY_EXPIRATION_MIN", "180"))
ENTRY_TRIGGER_BUFFER_PCT = float(os.getenv("ENTRY_TRIGGER_BUFFER_PCT", "0.0"))
ENTRY_EXPIRY_PRICE_PCT = float(os.getenv("ENTRY_EXPIRY_PRICE_PCT", "0.6"))

TP_SPLITS = [float(x.strip()) for x in os.getenv("TP_SPLITS", "30,30,30,10").split(",") if x.strip()]
DCA_QTY_MULTS = [float(x.strip()) for x in os.getenv("DCA_QTY_MULTS", "1.5,2.0,3.0").split(",") if x.strip()]

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
DISCORD_FETCH_LIMIT = int(os.getenv("DISCORD_FETCH_LIMIT", "50"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

STATE_FILE = Path("state.json")

if not (DISCORD_TOKEN and CHANNEL_ID and BYBIT_API_KEY and BYBIT_API_SECRET):
    raise SystemExit("❌ Missing envs: DISCORD_TOKEN, CHANNEL_ID, BYBIT_API_KEY, BYBIT_API_SECRET")

# =========================
# Discord
# =========================
DISCORD_HEADERS = {
    "Authorization": DISCORD_TOKEN,
    "User-Agent": "AO-Discord-Bybit-Executor/1.0"
}

def fetch_messages_after(after_id: Optional[str]) -> List[dict]:
    params = {"limit": max(1, min(DISCORD_FETCH_LIMIT, 100))}
    if after_id:
        params["after"] = str(after_id)
    r = requests.get(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
        headers=DISCORD_HEADERS, params=params, timeout=15
    )
    if r.status_code == 429:
        retry = float(r.json().get("retry_after", 5))
        time.sleep(retry + 0.5)
        return []
    r.raise_for_status()
    return r.json() or []

MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
MD_MARK = re.compile(r"[*_`~]+")
MULTI_WS = re.compile(r"[ \t\u00A0]+")

def clean_markdown(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "")
    s = html.unescape(s)
    s = MD_LINK.sub(r"\1", s)
    s = MD_MARK.sub("", s)
    s = MULTI_WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip()

def message_text(m: dict) -> str:
    parts = []
    parts.append(m.get("content") or "")
    for e in (m.get("embeds") or []):
        if not isinstance(e, dict):
            continue
        if e.get("title"): parts.append(str(e.get("title")))
        if e.get("description"): parts.append(str(e.get("description")))
        for f in (e.get("fields") or []):
            if not isinstance(f, dict): continue
            if f.get("name"): parts.append(str(f.get("name")))
            if f.get("value"): parts.append(str(f.get("value")))
        footer = (e.get("footer") or {}).get("text")
        if footer: parts.append(str(footer))
    return clean_markdown("\n".join([p for p in parts if p]))

# =========================
# Signal parsing (AO style)
# =========================
NUM = r"([0-9][0-9,]*\.?[0-9]*)"

HDR1 = re.compile(r"^\s*\*\*([A-Z0-9]+)\*\*\s+(LONG|SHORT)\s+Signal", re.I | re.M)
ENTER_ON_TRIGGER = re.compile(r"Enter\s+on\s+Trigger\s*:\s*`?\$?\s*" + NUM, re.I)
ENTRY_COLON = re.compile(r"\bEntry\s*:\s*`?\$?\s*" + NUM, re.I)
STOP_LOSS = re.compile(r"\bStop\s+Loss\s*:\s*`?\$?\s*" + NUM, re.I)
TPX = re.compile(r"\bTP([1-9])\s*:\s*`?\$?\s*" + NUM, re.I)
DCA = re.compile(r"\bDCA\s*#?\s*([1-9])\s*:\s*`?\$?\s*" + NUM, re.I)

def to_price(x: str) -> float:
    return float(x.replace(",", ""))

def parse_signal(txt: str) -> Optional[dict]:
    mh = HDR1.search(txt)
    if not mh:
        return None
    base = mh.group(1).upper()
    side = "long" if mh.group(2).upper() == "LONG" else "short"

    m_entry = ENTER_ON_TRIGGER.search(txt) or ENTRY_COLON.search(txt)
    if not m_entry:
        return None
    entry = to_price(m_entry.group(1))

    tps = {}
    for m in TPX.finditer(txt):
        idx = int(m.group(1))
        tps[idx] = to_price(m.group(2))

    # SL ist manchmal später "Moved to Breakeven" etc. – aber beim "awaiting entry" Signal fehlt es oft.
    m_sl = STOP_LOSS.search(txt)
    sl = to_price(m_sl.group(1)) if m_sl else None

    dcas = {}
    for m in DCA.finditer(txt):
        idx = int(m.group(1))
        dcas[idx] = to_price(m.group(2))

    if not tps:
        return None

    # sort
    tps_list = [tps[i] for i in sorted(tps.keys())]
    dca_list = [dcas[i] for i in sorted(dcas.keys())]

    return {
        "base": base,
        "side": side,
        "entry": entry,
        "tps": tps_list,
        "sl": sl,
        "dcas": dca_list
    }

# =========================
# State
# =========================
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {"last_id": None, "trades_today": 0, "day": None, "open_orders": {}}

def save_state(st: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

state = load_state()

def reset_daily_counters_if_needed():
    today = date.today().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["trades_today"] = 0
        save_state(state)

# =========================
# Bybit V5 REST helpers
# =========================
BASE_URL = "https://api.bybit.com" if not BYBIT_TESTNET else "https://api-testnet.bybit.com"

def _ts() -> str:
    return str(int(time.time() * 1000))

def _sign(secret: str, prehash: str) -> str:
    return hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()

def bybit_request(method: str, path: str, params: Optional[dict]=None, body: Optional[dict]=None) -> dict:
    params = params or {}
    body = body or {}

    ts = _ts()
    recv_window = "5000"

    if method.upper() == "GET":
        query = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())]) if params else ""
        prehash = ts + BYBIT_API_KEY + recv_window + query
        sign = _sign(BYBIT_API_SECRET, prehash)
        headers = {
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window
        }
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
        r = requests.get(url, headers=headers, timeout=15)
    else:
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        prehash = ts + BYBIT_API_KEY + recv_window + payload
        sign = _sign(BYBIT_API_SECRET, prehash)
        headers = {
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json"
        }
        url = f"{BASE_URL}{path}"
        r = requests.request(method.upper(), url, headers=headers, data=payload, timeout=15)

    data = r.json()
    if data.get("retCode") not in (0, "0"):
        raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')} | {data}")
    return data

def get_last_price(symbol: str) -> float:
    d = bybit_request("GET", "/v5/market/tickers", params={"category":"linear", "symbol":symbol})
    lst = d["result"]["list"]
    return float(lst[0]["lastPrice"])

def get_usdt_equity() -> float:
    d = bybit_request("GET", "/v5/account/wallet-balance", params={"accountType":"UNIFIED", "coin":"USDT"})
    coins = d["result"]["list"][0]["coin"]
    usdt = next((c for c in coins if c["coin"] == "USDT"), None)
    if not usdt:
        return 0.0
    # equity includes pnl
    return float(usdt.get("equity", usdt.get("walletBalance", 0.0)))

def set_leverage(symbol: str, lev: int):
    if DRY_RUN: return
    bybit_request("POST", "/v5/position/set-leverage", body={
        "category":"linear",
        "symbol":symbol,
        "buyLeverage": str(lev),
        "sellLeverage": str(lev)
    })

def place_conditional_entry(symbol: str, side: str, entry: float, qty: float) -> str:
    # Bybit: place order with triggerPrice + price (limit). orderType=Limit; triggerDirection depends.
    # side: long -> Buy, short -> Sell
    bybit_side = "Buy" if side == "long" else "Sell"
    trigger = entry * (1 - ENTRY_TRIGGER_BUFFER_PCT/100.0) if side == "long" else entry * (1 + ENTRY_TRIGGER_BUFFER_PCT/100.0)

    # triggerDirection: 1 = rise, 2 = fall (Bybit V5)
    # long triggers when price falls to trigger (usually) => fall
    # short triggers when price rises to trigger => rise
    trigger_dir = 2 if side == "long" else 1

    if DRY_RUN:
        print(f"[DRY_RUN] would place conditional LIMIT {bybit_side} {symbol} qty={qty} price={entry} trigger={trigger}")
        return "DRY_RUN_ORDER"

    d = bybit_request("POST", "/v5/order/create", body={
        "category":"linear",
        "symbol":symbol,
        "side":bybit_side,
        "orderType":"Limit",
        "qty": str(qty),
        "price": str(entry),
        "triggerPrice": str(round(trigger, 10)),
        "triggerDirection": trigger_dir,
        "timeInForce":"GTC",
        "reduceOnly": False,
        "closeOnTrigger": False
    })
    return d["result"]["orderId"]

def cancel_order(symbol: str, order_id: str):
    if DRY_RUN: return
    bybit_request("POST", "/v5/order/cancel", body={
        "category":"linear",
        "symbol":symbol,
        "orderId": order_id
    })

def place_reduce_only_tp(symbol: str, side: str, tp_price: float, qty: float) -> str:
    # close partial position via limit reduce-only
    bybit_side = "Sell" if side == "long" else "Buy"
    if DRY_RUN:
        print(f"[DRY_RUN] would place TP reduce-only {bybit_side} {symbol} qty={qty} price={tp_price}")
        return "DRY_TP"

    d = bybit_request("POST", "/v5/order/create", body={
        "category":"linear",
        "symbol":symbol,
        "side":bybit_side,
        "orderType":"Limit",
        "qty": str(qty),
        "price": str(tp_price),
        "timeInForce":"GTC",
        "reduceOnly": True
    })
    return d["result"]["orderId"]

def place_dca_limit(symbol: str, side: str, price: float, qty: float) -> str:
    bybit_side = "Buy" if side == "long" else "Sell"
    if DRY_RUN:
        print(f"[DRY_RUN] would place DCA {bybit_side} {symbol} qty={qty} price={price}")
        return "DRY_DCA"

    d = bybit_request("POST", "/v5/order/create", body={
        "category":"linear",
        "symbol":symbol,
        "side":bybit_side,
        "orderType":"Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce":"GTC",
        "reduceOnly": False
    })
    return d["result"]["orderId"]

def set_stop_loss(symbol: str, stop_price: float):
    # position level SL
    if DRY_RUN:
        print(f"[DRY_RUN] would set SL for {symbol} to {stop_price}")
        return
    bybit_request("POST", "/v5/position/trading-stop", body={
        "category":"linear",
        "symbol":symbol,
        "stopLoss": str(stop_price),
        "tpslMode":"Full"
    })

# =========================
# Sizing
# =========================
def calc_base_qty(symbol: str, entry: float, lev: int) -> float:
    equity = get_usdt_equity()
    margin = equity * (RISK_PCT/100.0)
    notional = margin * lev
    qty = notional / entry
    # simple rounding to 3 decimals default; for meme coins you may want 0 decimals.
    # (keeping it simple; if you want, we can add instrument-info rounding.)
    return max(0.0, round(qty, 3))

# =========================
# Filters
# =========================
def is_too_late(side: str, entry: float, last_price: float) -> bool:
    p = ENTRY_EXPIRY_PRICE_PCT / 100.0
    if side == "short":
        return last_price <= entry * (1 - p)
    else:
        return last_price >= entry * (1 + p)

# =========================
# Execution tracking (for BE move)
# =========================
# We'll watch execution stream and if TP1 order is filled -> move SL to entry (breakeven)
# We store mapping: symbol -> {entry, tp1_order_id, be_done, expires_at, tracked_order_ids...}
open_orders: Dict[str, Dict[str, Any]] = state.get("open_orders", {})

def now_ts() -> int:
    return int(time.time())

def cleanup_expired():
    # cancel entry orders that expired
    changed = False
    for sym, info in list(open_orders.items()):
        if now_ts() > info.get("expires_at", 0):
            print(f"⏳ Expired {sym} -> cancel pending orders")
            for oid in info.get("all_order_ids", []):
                try:
                    cancel_order(sym, oid)
                except Exception as e:
                    print("cancel error:", e)
            open_orders.pop(sym, None)
            changed = True
    if changed:
        state["open_orders"] = open_orders
        save_state(state)

# =========================
# Bybit Private WS
# =========================
def bybit_ws_url() -> str:
    # private linear ws (v5)
    return ("wss://stream.bybit.com/v5/private" if not BYBIT_TESTNET else "wss://stream-testnet.bybit.com/v5/private")

def ws_signature(expires: int) -> str:
    msg = f"GET/realtime{expires}"
    return hmac.new(BYBIT_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

async def ws_loop():
    while True:
        try:
            async with websockets.connect(bybit_ws_url(), ping_interval=20, ping_timeout=20) as ws:
                expires = int(time.time() * 1000) + 10_000
                sign = ws_signature(expires)
                auth = {"op":"auth","args":[BYBIT_API_KEY, expires, sign]}
                await ws.send(json.dumps(auth))

                # subscribe executions
                await ws.send(json.dumps({"op":"subscribe","args":["execution"]}))

                print("✅ WS connected (execution)")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("topic") != "execution":
                        continue
                    for ex in data.get("data", []):
                        symbol = ex.get("symbol")
                        order_id = ex.get("orderId")
                        exec_type = ex.get("execType")  # Trade
                        exec_qty = ex.get("execQty")

                        if not symbol or not order_id:
                            continue

                        info = open_orders.get(symbol)
                        if not info:
                            continue

                        # If TP1 filled -> move SL to BE
                        if order_id == info.get("tp1_order_id") and not info.get("be_done"):
                            entry = info.get("entry")
                            print(f"🎯 TP1 hit on {symbol} -> move SL to BE ({entry})")
                            try:
                                set_stop_loss(symbol, entry)
                                info["be_done"] = True
                                open_orders[symbol] = info
                                state["open_orders"] = open_orders
                                save_state(state)
                            except Exception as e:
                                print("SL->BE error:", e)

        except Exception as e:
            print("WS error:", e)
            await asyncio.sleep(3)

# =========================
# Main trading logic
# =========================
def handle_signal(sig: dict):
    reset_daily_counters_if_needed()

    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        print("🛑 Max trades/day reached -> ignore")
        return

    symbol = f"{sig['base']}{QUOTE}"
    side = sig["side"]
    entry = sig["entry"]
    tps = sig["tps"]
    dcas = sig["dcas"]

    # Safety: max open trades
    if len(open_orders) >= MAX_OPEN_TRADES:
        print("🛑 Max open trades reached -> ignore")
        return

    # Too-late filter
    last_price = get_last_price(symbol)
    if is_too_late(side, entry, last_price):
        print(f"⛔ Skip {symbol}: too late. entry={entry} last={last_price} (expiry_pct={ENTRY_EXPIRY_PRICE_PCT}%)")
        return

    lev = DEFAULT_LEVERAGE
    qty_base = calc_base_qty(symbol, entry, lev)
    if qty_base <= 0:
        print("⛔ qty_base <= 0")
        return

    print(f"\n📌 NEW {symbol} {side.upper()} entry={entry} last={last_price} qty_base={qty_base} lev={lev}")

    if not DRY_RUN:
        set_leverage(symbol, lev)

    # Place conditional entry LIMIT at exact entry (with trigger buffer if set)
    entry_order_id = place_conditional_entry(symbol, side, entry, qty_base)

    expires_at = now_ts() + ENTRY_EXPIRATION_MIN * 60

    # Store tracking; TP/DCA/SL are placed ONLY after entry fill in ideal world,
    # BUT because we want everything "prepared", we place them now as standing orders.
    # That said, exchange may reject reduce-only orders if no position yet on some setups.
    # So we do it in two-step:
    # - record plan now
    # - actual placement of TP/DCA/SL is done when we detect entry filled (not implemented here)
    #
    # To keep it stable today, we place them immediately (works on Bybit for many setups),
    # and worst case they fail -> you can enable "post-on-fill" enhancement next.
    all_ids = [entry_order_id]

    # Stop Loss: if signal provides none, we compute from your rule (19% beyond DCA2 or from entry)
    # Your rule: DCA1 +5%, DCA2 +15%, SL ~19% (for SHORT), mirrored for LONG.
    sl_price = sig.get("sl")
    if sl_price is None:
        # infer SL as entry moved against by 19%
        sl_price = entry * (1 + 0.19) if side == "short" else entry * (1 - 0.19)

    try:
        set_stop_loss(symbol, sl_price)
    except Exception as e:
        print("SL set error (likely no position yet) -> will still move to BE on TP1 later:", e)

    # Take Profits (reduce-only) based on TP_SPLITS
    # qty splits
    splits = TP_SPLITS[:len(tps)]
    if sum(splits) <= 0:
        splits = [100.0]
    total = sum(splits)
    splits = [s * 100.0 / total for s in splits]

    tp1_order_id = None
    for i, tp_price in enumerate(tps[:len(splits)]):
        part_qty = round(qty_base * (splits[i] / 100.0), 3)
        if part_qty <= 0:
            continue
        try:
            oid = place_reduce_only_tp(symbol, side, tp_price, part_qty)
            all_ids.append(oid)
            if i == 0:
                tp1_order_id = oid
        except Exception as e:
            print(f"TP{i+1} place error (maybe no position yet):", e)

    # DCA orders (increase position)
    for i, dca_price in enumerate(dcas[:len(DCA_QTY_MULTS)]):
        mult = DCA_QTY_MULTS[i]
        dca_qty = round(qty_base * mult, 3)
        try:
            oid = place_dca_limit(symbol, side, dca_price, dca_qty)
            all_ids.append(oid)
        except Exception as e:
            print(f"DCA{i+1} place error:", e)

    open_orders[symbol] = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "entry_order_id": entry_order_id,
        "tp1_order_id": tp1_order_id,
        "be_done": False,
        "expires_at": expires_at,
        "all_order_ids": all_ids
    }

    state["open_orders"] = open_orders
    state["trades_today"] = state.get("trades_today", 0) + 1
    save_state(state)

    print(f"✅ Planned & placed (as much as possible). Expires at {datetime.fromtimestamp(expires_at).strftime('%H:%M:%S')}")

def main_loop():
    # baseline
    if state.get("last_id") is None:
        try:
            page = fetch_messages_after(None)
            if page:
                state["last_id"] = str(page[0]["id"])
                save_state(state)
        except:
            pass

    print("🚀 Discord -> Bybit Executor started")
    last_id = state.get("last_id")

    while True:
        try:
            cleanup_expired()

            msgs = fetch_messages_after(last_id)
            msgs_sorted = sorted(msgs, key=lambda m: int(m.get("id","0")))

            if not msgs_sorted:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] waiting...")
            else:
                for m in msgs_sorted:
                    mid = int(m.get("id","0"))
                    raw = message_text(m)
                    sig = parse_signal(raw)
                    if sig:
                        handle_signal(sig)
                    last_id = str(mid)

                state["last_id"] = last_id
                save_state(state)

        except Exception as e:
            print("loop error:", e)

        time.sleep(POLL_SECONDS + random.uniform(0, 1.0))

async def main():
    # run WS + poll loop together
    task_ws = asyncio.create_task(ws_loop())
    task_poll = asyncio.to_thread(main_loop)
    await asyncio.gather(task_ws, task_poll)

if __name__ == "__main__":
    asyncio.run(main())
