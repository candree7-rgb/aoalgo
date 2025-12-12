import re
from typing import Optional, Dict, Any, List

NUM = r"([0-9][0-9,]*\.?[0-9]*)"

def _to_f(s: str) -> float:
    return float(s.replace(",", ""))

def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    t = text.replace("\r", "")

    # Match: "**BARD** SHORT Signal" oder "**YALA** SHORT Signal"
    m = re.search(r"\*\*([A-Z0-9]+)\*\*\s+(LONG|SHORT)\s+Signal", t, re.I)
    if not m:
        return None
    base = m.group(1).upper()
    side = "buy" if m.group(2).upper() == "LONG" else "sell"

    # Trigger/Entry
    trig = None
    m_tr = re.search(r"Enter\s+on\s+Trigger\s*:\s*`?\$?\s*" + NUM, t, re.I)
    if m_tr:
        trig = _to_f(m_tr.group(1))
    else:
        m_en = re.search(r"\bEntry\s*:\s*`?\$?\s*" + NUM, t, re.I)
        if m_en:
            trig = _to_f(m_en.group(1))
    if trig is None:
        return None

    # TPs (TP1..TP4)
    tp_prices: List[float] = []
    for i in range(1, 5):
        m_tp = re.search(rf"\*\*TP{i}\:\*\*\s*`?\$?\s*{NUM}", t, re.I)
        if m_tp:
            tp_prices.append(_to_f(m_tp.group(1)))
    if len(tp_prices) < 3:
        return None  # wir brauchen mind. 3

    # DCA #1..#3 (optional)
    dca_prices: List[float] = []
    for i in range(1, 4):
        m_d = re.search(rf"\*\*DCA\s*#?{i}\:\*\*\s*`?\$?\s*{NUM}", t, re.I)
        if m_d:
            dca_prices.append(_to_f(m_d.group(1)))

    # SL optional (manchmal steht er drin, manchmal später "moved to breakeven")
    sl_price = None
    m_sl = re.search(r"\*\*Stop\s+Loss\:\*\*\s*`?\$?\s*" + NUM, t, re.I)
    if m_sl:
        sl_price = _to_f(m_sl.group(1))

    symbol = f"{base}{quote}"

    return {
        "base": base,
        "symbol": symbol,
        "side": side,            # "buy" / "sell"
        "trigger": trig,
        "tp_prices": tp_prices,  # [tp1,tp2,tp3,(tp4?)]
        "dca_prices": dca_prices,
        "sl_price": sl_price,
    }
