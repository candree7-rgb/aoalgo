import re
import hashlib
from typing import Any, Dict, Optional, List

NUM = r"([0-9]+(?:\.[0-9]+)?)"

# AO Algo Format patterns
# Symbol: "AO Algo • VELVET #1" or "AO Algo • ALU #1"
RE_SYMBOL = re.compile(r"AO\s*Algo\s*[•·]\s*([A-Z0-9]+)\s*#?\d*", re.I)

# Side: "🔵 LONG SIGNAL" or "🔴 SHORT SIGNAL"
RE_SIDE = re.compile(r"(LONG|SHORT)\s+SIGNAL", re.I)

# Entry price: "$0.147400" after "ENTRY"
RE_ENTRY = re.compile(r"\$" + NUM)

# TPs: "TP1: $0.148580" or "✅ TP1: $0.148580" or "⏳ TP1:"
RE_TP = re.compile(r"TP(\d+)\s*:\s*\$?" + NUM, re.I)

# SL: "🛑 SL: $0.143830" or "SL: $0.143830"
RE_SL = re.compile(r"SL\s*:\s*\$?" + NUM, re.I)

# Status patterns
RE_STATUS_ACTIVE = re.compile(r"🟢\s*ACTIVE", re.I)
RE_STATUS_BREAKEVEN = re.compile(r"⚖️?\s*BREAKEVEN", re.I)
RE_STATUS_WIN = re.compile(r"✅\s*TP\d+\s*WIN", re.I)
RE_STATUS_CANCELLED = re.compile(r"CANCELLED|CANCELED", re.I)
RE_STATUS_CLOSED = re.compile(r"CLOSED", re.I)


def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    """
    Parse AO Algo signal format.

    Example:
        AO Algo • VELVET #1
        🔵 LONG SIGNAL • Leverage: 25x
        📊 ENTRY
        $0.147400 ✅ Triggered
        🎯 PROFIT TARGETS
        ✅ TP1: $0.148580
        ✅ TP2: $0.149760
        ⏳ TP3: $0.153300
        ⏳ TP4: $0.206360
        🛑 SL: $0.143830
        STATUS
        🟢 ACTIVE
    """
    # Only process ACTIVE signals (not BREAKEVEN, WIN, CLOSED, CANCELLED)
    if RE_STATUS_BREAKEVEN.search(text):
        return None
    if RE_STATUS_WIN.search(text):
        return None
    if RE_STATUS_CANCELLED.search(text):
        return None
    if RE_STATUS_CLOSED.search(text):
        return None

    # Must have ACTIVE status or be a fresh signal
    # Fresh signals might not have status yet, so we check for LONG/SHORT SIGNAL
    if not RE_STATUS_ACTIVE.search(text):
        # If no ACTIVE status, must at least have LONG/SHORT SIGNAL
        if not RE_SIDE.search(text):
            return None

    # Extract symbol
    ms = RE_SYMBOL.search(text)
    if not ms:
        return None
    base = ms.group(1).upper()
    symbol = f"{base}{quote}"

    # Extract side
    mside = RE_SIDE.search(text)
    if not mside:
        return None
    side_word = mside.group(1).upper()
    side = "sell" if side_word == "SHORT" else "buy"

    # Extract entry price (first $ amount after ENTRY section)
    # Split by ENTRY to get the section after it
    entry_section = text
    if "ENTRY" in text.upper():
        idx = text.upper().find("ENTRY")
        entry_section = text[idx:]

    mentry = RE_ENTRY.search(entry_section)
    if not mentry:
        return None
    trigger = float(mentry.group(1))

    # Extract TPs (only TP1, TP2, TP3 - ignore TP4)
    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        if idx > 3:  # Ignore TP4 and beyond
            continue
        price = float(m.group(2))
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx-1] = price
    tps = [p for p in tps if p > 0]

    # Extract SL
    sl = None
    msl = RE_SL.search(text)
    if msl:
        sl = float(msl.group(1))

    return {
        "base": base,
        "symbol": symbol,
        "side": side,          # buy/sell
        "trigger": trigger,
        "tp_prices": tps,
        "dca_prices": [],      # No DCA for AO Algo
        "sl_price": sl,
        "raw": text,
    }


def signal_hash(sig: Dict[str, Any]) -> str:
    """Generate unique hash for signal deduplication."""
    core = f"{sig.get('symbol')}|{sig.get('side')}|{sig.get('trigger')}|{sig.get('tp_prices')}"
    return hashlib.md5(core.encode("utf-8")).hexdigest()


def parse_signal_status(text: str) -> str:
    """
    Parse the current status of a signal.
    Returns: 'active', 'breakeven', 'win', 'cancelled', 'closed', 'unknown'
    """
    if RE_STATUS_ACTIVE.search(text):
        return "active"
    if RE_STATUS_BREAKEVEN.search(text):
        return "breakeven"
    if RE_STATUS_WIN.search(text):
        return "win"
    if RE_STATUS_CANCELLED.search(text):
        return "cancelled"
    if RE_STATUS_CLOSED.search(text):
        return "closed"
    return "unknown"


def is_signal_still_valid(text: str) -> bool:
    """Check if signal is still valid for entry (ACTIVE status)."""
    status = parse_signal_status(text)
    return status in ("active", "unknown")
