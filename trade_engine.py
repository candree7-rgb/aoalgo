import time
from typing import Dict, Any, List, Optional
from config import (
    CATEGORY, DEFAULT_LEVERAGE, ENTRY_EXPIRATION_MIN, ENTRY_TOO_FAR_PCT,
    ENTRY_TRIGGER_BUFFER_PCT, ENTRY_LIMIT_PRICE_OFFSET_PCT,
    TP_SPLITS, DCA_QTY_MULTS,
    INITIAL_SL_PCT, MOVE_SL_TO_BE_ON_TP1, DRY_RUN
)

def _side_to_pos_side(order_side: str) -> str:
    # order_side: Buy/Sell
    return "Long" if order_side == "Buy" else "Short"

class TradeEngine:
    def __init__(self, bybit, state: dict):
        self.bybit = bybit
        self.state = state

    # ---------- Helpers ----------
    def _too_far(self, side: str, last: float, trigger: float) -> bool:
        # User rule: wenn SHORT und Preis schon X% unter trigger -> skip
        # SHORT => Sell
        if side == "Sell":
            return last <= trigger * (1 - ENTRY_TOO_FAR_PCT/100)
        else:
            return last >= trigger * (1 + ENTRY_TOO_FAR_PCT/100)

    def _trigger_direction(self, side: str, last: float, trigger: float) -> int:
        # Bybit triggerDirection: 1 = rises to trigger, 2 = falls to trigger (Bybit convention)
        # We infer based on where last is vs trigger
        if last < trigger:
            return 1  # needs rise
        if last > trigger:
            return 2  # needs fall
        return 1

    def _qty_from_position(self, symbol: str) -> float:
        plist = self.bybit.positions(CATEGORY, symbol)
        for p in plist:
            if p.get("symbol") == symbol:
                sz = float(p.get("size","0") or "0")
                return sz
        return 0.0

    # ---------- Core ----------
    def place_conditional_entry(self, sig: Dict[str,Any], client_trade_id: str) -> Optional[str]:
        symbol = sig["symbol"]
        side   = "Sell" if sig["side"] == "sell" else "Buy"
        trigger = float(sig["trigger"])

        last = self.bybit.last_price(CATEGORY, symbol)
        if self._too_far(side, last, trigger):
            return None

        trigger_adj = trigger * (1 - ENTRY_TRIGGER_BUFFER_PCT/100) if side == "Buy" else trigger * (1 + ENTRY_TRIGGER_BUFFER_PCT/100)

        # LIMIT price: optional aggressiver, um Fill zu sichern
        limit_price = trigger
        if ENTRY_LIMIT_PRICE_OFFSET_PCT != 0:
            if side == "Sell":
                # für SHORT willst du lieber etwas höher füllen (Sell Limit höher)
                limit_price = trigger * (1 + abs(ENTRY_LIMIT_PRICE_OFFSET_PCT)/100)
            else:
                limit_price = trigger * (1 - abs(ENTRY_LIMIT_PRICE_OFFSET_PCT)/100)

        td = self._trigger_direction(side, last, trigger_adj)

        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": "0.0",  # IMPORTANT: Set via risk model later (du kannst hier fixed qty rein tun)
            "price": f"{limit_price:.10f}",
            "timeInForce": "GTC",
            "triggerDirection": td,
            "triggerPrice": f"{trigger_adj:.10f}",
            "triggerBy": "LastPrice",
            "reduceOnly": False,
            "closeOnTrigger": False,
            "orderLinkId": client_trade_id
        }

        # NOTE: qty musst du setzen. Für jetzt: minimal safe, du kannst’s an Equity koppeln.
        # Wenn du schon in Bybit “position size by margin” machst: dann musst du per API qty rechnen.
        # Ich lasse es absichtlich NICHT “magisch” – sonst ballerst du dich weg.

        if DRY_RUN:
            print("DRY_RUN entry:", body)
            return "DRY_RUN"

        resp = self.bybit.place_order(body)
        oid = ((resp.get("result") or {}).get("orderId")) or None
        return oid

    def on_execution(self, ev: Dict[str,Any]):
        """
        Called from WS: reacts instantly to fills.
        """
        symbol = ev.get("symbol")
        if not symbol: return

        exec_type = ev.get("execType") or ev.get("type")  # WS payload differs slightly
        if str(exec_type).lower() not in ("trade","execution","fill","taker","maker"):
            # ignore non-fill events
            pass

        # Identify which trade this execution belongs to via orderLinkId if available
        link = ev.get("orderLinkId") or ev.get("orderLinkID") or ""
        if not link:
            return

        tr = self.state["open_trades"].get(link)
        if not tr:
            return

        # If TP1 order filled -> move SL to BE
        if MOVE_SL_TO_BE_ON_TP1 and tr.get("tp1_order_id") and ev.get("orderId") == tr.get("tp1_order_id"):
            be = tr["entry_price"]
            self._move_sl(symbol, tr["pos_side"], be)
            tr["sl_moved_to_be"] = True
            print(f"✅ SL -> BE @ {be} ({symbol})")

    def _move_sl(self, symbol: str, pos_side: str, sl_price: float):
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,  # one-way
            "stopLoss": f"{sl_price:.10f}",
            "tpslMode": "Full"
        }
        if DRY_RUN:
            print("DRY_RUN move SL:", body)
            return
        self.bybit.set_trading_stop(body)

    def place_post_entry_orders(self, trade: Dict[str,Any]):
        """
        Call this after detecting entry fill (either via WS order fill or via polling fallback).
        Places:
        - SL (initial)
        - TP ladder reduce-only
        - DCA conditionals (add)
        """
        symbol = trade["symbol"]
        entry  = trade["entry_price"]
        side   = trade["order_side"]     # Buy/Sell
        pos_side = trade["pos_side"]

        # Initial SL: from signal if provided else INITIAL_SL_PCT
        if trade.get("sl_price"):
            sl_price = trade["sl_price"]
        else:
            if side == "Sell":
                sl_price = entry * (1 + INITIAL_SL_PCT/100)
            else:
                sl_price = entry * (1 - INITIAL_SL_PCT/100)

        # Set SL at position-level (auto adjusts with size)
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "stopLoss": f"{sl_price:.10f}",
            "tpslMode": "Full"
        }
        if DRY_RUN:
            print("DRY_RUN trading-stop:", body)
        else:
            self.bybit.set_trading_stop(body)

        # TP ladder reduce-only LIMITs
        # qty calc: percent of current position size
        size = self._qty_from_position(symbol)
        if size <= 0:
            print("⚠️ No position size yet; retry later")
            return

        tp_prices: List[float] = trade["tp_prices"]
        splits = trade["tp_splits"]
        for i, (tp, pct) in enumerate(zip(tp_prices, splits), start=1):
            if pct <= 0: continue
            qty = size * (pct/100)
            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": "Buy" if side == "Sell" else "Sell",  # close direction
                "orderType": "Limit",
                "qty": f"{qty:.10f}",
                "price": f"{tp:.10f}",
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:TP{i}"
            }
            if DRY_RUN:
                print("DRY_RUN TP:", o)
                oid = f"DRY_TP{i}"
            else:
                resp = self.bybit.place_order(o)
                oid = (resp.get("result") or {}).get("orderId")
            if i == 1:
                trade["tp1_order_id"] = oid

        # DCA conditionals (add to position)
        dca_prices = trade.get("dca_prices") or []
        for j, p in enumerate(dca_prices, start=1):
            # qty multiplier vs initial size chunk (simple)
            # (du kannst später: base_qty * mult; und base_qty definierst du sauber)
            qty = size * DCA_QTY_MULTS[min(j-1, len(DCA_QTY_MULTS)-1)]
            last = self.bybit.last_price(CATEGORY, symbol)
            td = self._trigger_direction(side, last, p)
            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": f"{qty:.10f}",
                "price": f"{p:.10f}",
                "timeInForce": "GTC",
                "triggerDirection": td,
                "triggerPrice": f"{p:.10f}",
                "triggerBy": "LastPrice",
                "reduceOnly": False,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:DCA{j}"
            }
            if DRY_RUN:
                print("DRY_RUN DCA:", o)
            else:
                self.bybit.place_order(o)
