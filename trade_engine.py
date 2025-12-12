import time
from typing import Dict, Any, List, Optional

from config import (
    CATEGORY,
    ENTRY_TOO_FAR_PCT,
    ENTRY_TRIGGER_BUFFER_PCT,
    ENTRY_LIMIT_PRICE_OFFSET_PCT,
    INITIAL_SL_PCT,
    MOVE_SL_TO_BE_ON_TP1,
    DRY_RUN,
    DCA_QTY_MULTS,
    # Runner trailing
    TRAIL_ENABLED,
    TRAIL_AFTER_TP_INDEX,
    TRAIL_DIST_PCT,
    TRAIL_TRIGGER_BY,
)

def _side_to_pos_side(order_side: str) -> str:
    # order_side: Buy/Sell
    return "Long" if order_side == "Buy" else "Short"


class TradeEngine:
    def __init__(self, bybit, state: dict):
        self.bybit = bybit
        self.state = state

    # ----------------- State helper -----------------
    def _state_save(self):
        # Wenn du state.py mit save() hast: nutzen. Sonst egal.
        try:
            fn = getattr(self.state, "save", None)
            if callable(fn):
                fn()
        except:
            pass

    # ----------------- Helpers -----------------
    def _too_far(self, side: str, last: float, trigger: float) -> bool:
        # SHORT (Sell): wenn Preis schon X% UNTER trigger ist -> skip
        if side == "Sell":
            return last <= trigger * (1 - ENTRY_TOO_FAR_PCT / 100)
        # LONG (Buy): wenn Preis schon X% ÜBER trigger ist -> skip
        return last >= trigger * (1 + ENTRY_TOO_FAR_PCT / 100)

    def _trigger_direction(self, last: float, trigger: float) -> int:
        # Bybit: 1 = rises to trigger, 2 = falls to trigger
        if last < trigger:
            return 1
        if last > trigger:
            return 2
        return 1

    def _qty_from_position(self, symbol: str) -> float:
        plist = self.bybit.positions(CATEGORY, symbol)
        for p in plist:
            if p.get("symbol") == symbol:
                return float(p.get("size", "0") or "0")
        return 0.0

    def _set_initial_sl(self, symbol: str, side: str, entry: float, sl_price_from_signal: Optional[float] = None):
        if sl_price_from_signal is not None:
            sl_price = float(sl_price_from_signal)
        else:
            if side == "Sell":
                sl_price = entry * (1 + INITIAL_SL_PCT / 100)
            else:
                sl_price = entry * (1 - INITIAL_SL_PCT / 100)

        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,      # one-way
            "stopLoss": f"{sl_price:.10f}",
            "tpslMode": "Full",
            "slTriggerBy": TRAIL_TRIGGER_BY if TRAIL_TRIGGER_BY else "LastPrice",
        }

        if DRY_RUN:
            print("DRY_RUN set SL:", body)
            return
        self.bybit.set_trading_stop(body)

    def _move_sl_to_price(self, symbol: str, sl_price: float):
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,     # one-way
            "stopLoss": f"{sl_price:.10f}",
            "tpslMode": "Full",
            "slTriggerBy": TRAIL_TRIGGER_BY if TRAIL_TRIGGER_BY else "LastPrice",
        }
        if DRY_RUN:
            print("DRY_RUN move SL:", body)
            return
        self.bybit.set_trading_stop(body)

    def _enable_trailing_for_runner(self, symbol: str):
        # trailingStop = ABSOLUTER Abstand
        last = self.bybit.last_price(CATEGORY, symbol)
        trailing_dist = last * (TRAIL_DIST_PCT / 100.0)

        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
            "trailingStop": f"{trailing_dist:.10f}",
            "tpTriggerBy": TRAIL_TRIGGER_BY if TRAIL_TRIGGER_BY else "LastPrice",
            "slTriggerBy": TRAIL_TRIGGER_BY if TRAIL_TRIGGER_BY else "LastPrice",
        }

        if DRY_RUN:
            print("DRY_RUN enable trailing:", body)
            return

        # Du hast in bybit_v5.py set_trailing_stop(body) als wrapper
        self.bybit.set_trailing_stop(body)

    # ----------------- Core: Conditional Entry -----------------
    def place_conditional_entry(self, sig: Dict[str, Any], client_trade_id: str, qty: float) -> Optional[str]:
        """
        sig expected:
          symbol, side ('buy'/'sell'), trigger (float)
        qty: konkrete Ordermenge (du gibst die von deinem Risk-Modell rein)
        """
        symbol = sig["symbol"]
        side = "Sell" if sig["side"] == "sell" else "Buy"
        trigger = float(sig["trigger"])

        last = self.bybit.last_price(CATEGORY, symbol)
        if self._too_far(side, last, trigger):
            print(f"⛔ Skip {symbol}: price too far from trigger (last={last}, trigger={trigger})")
            return None

        # Optionaler Buffer auf Trigger (z.B. SHORT: +buffer, LONG: -buffer)
        trigger_adj = trigger * (1 - ENTRY_TRIGGER_BUFFER_PCT / 100) if side == "Buy" else trigger * (1 + ENTRY_TRIGGER_BUFFER_PCT / 100)

        # Entry-Limit-Preis (gegen Slippage / Fill-Qualität)
        limit_price = trigger
        if ENTRY_LIMIT_PRICE_OFFSET_PCT != 0:
            if side == "Sell":
                limit_price = trigger * (1 + abs(ENTRY_LIMIT_PRICE_OFFSET_PCT) / 100)
            else:
                limit_price = trigger * (1 - abs(ENTRY_LIMIT_PRICE_OFFSET_PCT) / 100)

        td = self._trigger_direction(last, trigger_adj)

        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": f"{qty:.10f}",
            "price": f"{limit_price:.10f}",
            "timeInForce": "GTC",
            "triggerDirection": td,
            "triggerPrice": f"{trigger_adj:.10f}",
            "triggerBy": "LastPrice",
            "reduceOnly": False,
            "closeOnTrigger": False,
            "orderLinkId": client_trade_id,
        }

        if DRY_RUN:
            print("DRY_RUN entry:", body)
            return "DRY_RUN"

        resp = self.bybit.place_order(body)
        oid = ((resp.get("result") or {}).get("orderId")) or None
        print(f"✅ Conditional entry placed: {symbol} {side} trigger={trigger_adj} limit={limit_price} oid={oid}")
        return oid

    # ----------------- WS execution handler -----------------
    def on_execution(self, ev: Dict[str, Any]):
        """
        Reacts instantly to fills.
        Expects ev includes: symbol, orderId, orderLinkId, execType
        """
        symbol = ev.get("symbol")
        if not symbol:
            return

        exec_type = str(ev.get("execType") or "").lower()
        # Bybit sends different variants; we only act on trade-like execution
        if exec_type and exec_type not in ("trade", "execution"):
            return

        order_id = ev.get("orderId") or ""
        link = ev.get("orderLinkId") or ""
        if not link:
            return

        # Wir speichern TPs als {tradeId}:TP1 etc.
        trade_id = link.split(":")[0]

        tr = (self.state.get("open_trades") or {}).get(trade_id)
        if not tr:
            return

        # ----- TP1 filled -> SL to BE -----
        if MOVE_SL_TO_BE_ON_TP1 and tr.get("tp1_order_id") and order_id == tr["tp1_order_id"]:
            if not tr.get("sl_moved_to_be"):
                be = float(tr["entry_price"])
                self._move_sl_to_price(tr["symbol"], be)
                tr["sl_moved_to_be"] = True
                print(f"✅ SL -> BE @ {be} ({tr['symbol']})")
                self._state_save()

        # ----- TP3 filled -> enable trailing for runner -----
        if TRAIL_ENABLED and TRAIL_AFTER_TP_INDEX == 3 and tr.get("tp3_order_id") and order_id == tr["tp3_order_id"]:
            if not tr.get("trailing_active"):
                self._enable_trailing_for_runner(tr["symbol"])
                tr["trailing_active"] = True
                print(f"🔥 Trailing enabled ({TRAIL_DIST_PCT}%) for runner ({tr['symbol']})")
                self._state_save()

    # ----------------- Post-entry orders (after entry fill) -----------------
    def place_post_entry_orders(self, trade: Dict[str, Any]):
        """
        Call after entry is filled.
        Places:
          - initial SL on position
          - TP1..TP3 reduce-only (90% total)
          - DCA conditionals add orders
        trade must include:
          symbol, entry_price, order_side(Buy/Sell), tp_prices(list), tp_splits(list), dca_prices(list), id
          optional sl_price (from signal)
        """
        symbol = trade["symbol"]
        entry = float(trade["entry_price"])
        side = trade["order_side"]  # Buy/Sell

        # 1) Set initial SL (position-level)
        self._set_initial_sl(symbol, side, entry, trade.get("sl_price"))

        # 2) TP ladder: reduce-only LIMITs
        size = self._qty_from_position(symbol)
        if size <= 0:
            print("⚠️ No position size yet; retry later")
            return

        tp_prices: List[float] = [float(x) for x in (trade.get("tp_prices") or [])]
        splits: List[float] = [float(x) for x in (trade.get("tp_splits") or [])]

        # Expect: splits = [30,30,30] (Runner = 10% stays open)
        close_side = "Buy" if side == "Sell" else "Sell"

        for i, (tp, pct) in enumerate(zip(tp_prices, splits), start=1):
            if pct <= 0:
                continue
            qty = size * (pct / 100.0)

            o = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": close_side,
                "orderType": "Limit",
                "qty": f"{qty:.10f}",
                "price": f"{tp:.10f}",
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
                "orderLinkId": f"{trade['id']}:TP{i}",
            }

            if DRY_RUN:
                print("DRY_RUN TP:", o)
                oid = f"DRY_TP{i}"
            else:
                resp = self.bybit.place_order(o)
                oid = (resp.get("result") or {}).get("orderId")

            if i == 1:
                trade["tp1_order_id"] = oid
            if i == 3:
                trade["tp3_order_id"] = oid

        # 3) DCA conditionals (add to position)
        dca_prices: List[float] = [float(x) for x in (trade.get("dca_prices") or [])]
        for j, p in enumerate(dca_prices, start=1):
            mult = DCA_QTY_MULTS[min(j - 1, len(DCA_QTY_MULTS) - 1)]
            qty = size * float(mult)

            last = self.bybit.last_price(CATEGORY, symbol)
            td = self._trigger_direction(last, p)

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
                "orderLinkId": f"{trade['id']}:DCA{j}",
            }

            if DRY_RUN:
                print("DRY_RUN DCA:", o)
            else:
                self.bybit.place_order(o)

        # Persist in-memory trade state
        trade.setdefault("sl_moved_to_be", False)
        trade.setdefault("trailing_active", False)
        self._state_save()
