"""Tick field definitions + value cleaning (the single source used by the feed) and an
append-only JSONL tick log with a byte-offset cursor (idempotent re-reads)."""
from __future__ import annotations

import json
import math
from pathlib import Path

FIELDS = [
    "key", "command", "instrumentId", "exchangeTimeNs",
    "lsAdapterReceiveTimeNs", "lsAdapterSendTimeNs",
    "mdsSentTimeNs", "settlementTimeNs",
    "bidPrice", "bidQuantity", "askPrice", "askQuantity",
    "impliedBidPrice", "impliedBidQuantity",
    "impliedAskPrice", "impliedAskQuantity",
    "open", "close", "high", "low",
    "settlementPrice", "settlementType",
    "tradePrice", "tradeQuantity", "totalTradedQuantity",
    "state", "VWAP", "indicativeOpen", "cumLtq", "tradeSide",
]

IDENTIFIER_FIELDS = {"key", "instrumentId"}
INTEGER_FIELDS = {
    "exchangeTimeNs", "lsAdapterReceiveTimeNs", "lsAdapterSendTimeNs",
    "settlementTimeNs", "bidQuantity", "askQuantity",
    "impliedBidQuantity", "impliedAskQuantity", "totalTradedQuantity", "cumLtq",
}


def clean_value(field: str, value):
    """Parse a raw LS field value to a typed scalar (or None). Mirrors the original feed."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return None
    if field in IDENTIFIER_FIELDS:
        return text.split("-")[0]
    if field in INTEGER_FIELDS:
        try:
            return int(text)
        except ValueError:
            return text
    try:
        number = float(text)
        if math.isfinite(number):
            return number
    except ValueError:
        pass
    return text


def tick_record(cleaned: dict, recv_ts_iso: str) -> dict:
    """Project a cleaned LS update to the bar-relevant tick record (what the bar builder reads)."""
    return {
        "ts": recv_ts_iso,
        "exchange_time_ns": cleaned.get("exchangeTimeNs"),
        "bid": cleaned.get("bidPrice"),
        "ask": cleaned.get("askPrice"),
        "trade": cleaned.get("tradePrice"),
        "total_traded_qty": cleaned.get("totalTradedQuantity"),
        "trade_qty": cleaned.get("tradeQuantity"),
    }


def append_tick(path: Path, record: dict) -> None:
    """Append one JSONL line (binary, so newline bytes/offsets are stable on Windows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, allow_nan=False) + "\n").encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(data)


def read_ticks(path: Path, start_offset: int = 0):
    """Return (records, new_offset) — complete lines only, partial trailing line left for next time."""
    pairs, offset = read_ticks_indexed(path, start_offset)
    return [rec for _, rec in pairs], offset


def read_ticks_indexed(path: Path, start_offset: int = 0):
    """Like ``read_ticks`` but returns [(line_start_offset, record), ...] so the bar builder can
    rewind the cursor to BEFORE the first still-forming-minute tick (no data loss on the open minute
    or across restarts). Plus the end offset of the last complete line."""
    path = Path(path)
    if not path.exists():
        return [], start_offset
    pairs = []
    offset = start_offset
    with open(path, "rb") as fh:
        fh.seek(start_offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            start = offset
            offset += len(raw)
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                pairs.append((start, json.loads(s)))
            except json.JSONDecodeError:
                continue
    return pairs, offset
