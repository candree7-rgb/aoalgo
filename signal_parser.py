import re, hashlib
from typing import Optional, Dict, Any

NUM = r"([0-9][0-9,]*\.?[0-9]*)"

def _p(x: str) -> float:
    return float(x.replace(",", ""))

def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    # must contain SHORT/LONG signal
    m0 = re.search(r"\b([A-Z0-9]+)\b\s+(LONG|SHORT)\s+Signal", text, re.I)
    if not m0:
        return None

    base = m0.group(1).upper()
    side = "buy" if m0.group(2).upper() == "LONG" else "sell"   # Bybit: Buy/Sell

    # Trigger/Entry
    trig = None
    mtr = re.search(r"Enter\s+on\s+Trigger\s*:\s*`?\$?\s*" + NUM, text, re.I)
    if mtr:
        trig = _p(mtr.group(1))
    else:
        me = re.search(r"\bEntry\b\s*:\s*`?\$?\s*" + NUM, text, re.I)
        if me:
            trig = _p(me.group(1))
    if trig is None:
        return None

    # TPs (1..6)
    tps = []
    for i in range(1, 7):
        mi = re.search(rf"\bTP{i}\b\s*:\s*`?\$?\s*{NUM}", text, re.I)
        if mi:
            tps.append(_p(mi.group(1)))
    if not tps:
        return None

    # DCA (1..3) optional
    dcas = []
    for i in range(1, 4):
        mi = re.search(rf"\bDCA\s*#?{i}\b\s*:\s*`?\$?\s*{NUM}", text, re.I)
        if mi:
            dcas.append(_p(mi.group(1)))

    # SL optional (manchmal steht “Moved to Breakeven” statt Preis)
    sl = None
    msl = re.search(r"\bStop\s+Loss\b\s*:\s*`?\$?\s*" + NUM, text, re.I)
    if msl:
        sl = _p(msl.group(1))

    symbol = f"{base}{quote}"

    h = hashlib.md5(f"{symbol}|{side}|{trig}".encode()).hexdigest()
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "side": side,          # buy=LONG, sell=SHORT
        "trigger": trig,
        "tps": tps[:4],        # du nutzt eh TP1-TP4
        "dcas": dcas[:3],
        "stop_loss": sl,
        "hash": h
    }
