"""Pure execution-aware support/resistance hit detection.

You BUY into support, so a support zone is hit when the feed ASK reaches its near edge
(``ask <= zone_high``). You SELL into resistance, so it is hit when the BID reaches its near
edge (``bid >= zone_low``). Low-confidence zones are ignored; NaN/None/<=0 prices never fire."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Hit:
    instrument: str
    side: str
    edge: float          # the near edge the price reached (support: zone_high, resistance: zone_low)
    center: float
    zone_low: float
    zone_high: float
    confidence: str
    bucket: str
    touches: int
    score: float
    bid: "float | None"
    ask: "float | None"
    hit_price: float     # the side that triggered (support: ask, resistance: bid)


def _fp(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def detect_hits(bid, ask, zones) -> list:
    """Return the list of zones hit at this (bid, ask). zones: dicts with instrument/side/
    zone_low/zone_high/zone_center/confidence/bucket/touches/score."""
    hits: list[Hit] = []
    b = float(bid) if _fp(bid) else None
    a = float(ask) if _fp(ask) else None
    if b is not None and a is not None and b > a:
        return []   # crossed / half-updated book (bid>ask) -> unusable; never fire (mirrors bar_price)
    for z in zones:
        if str(z.get("confidence")) == "Low":
            continue
        zl, zh = z.get("zone_low"), z.get("zone_high")
        if not (_fp(zl) and _fp(zh)):
            continue
        zl, zh = float(zl), float(zh)
        center = float(z["zone_center"]) if _fp(z.get("zone_center")) else (zl + zh) / 2.0
        common = dict(instrument=str(z.get("instrument")), center=center, zone_low=zl, zone_high=zh,
                      confidence=str(z.get("confidence")), bucket=str(z.get("bucket")),
                      touches=int(z.get("touches") or 0),
                      score=float(z["score"]) if _fp(z.get("score")) else 0.0, bid=b, ask=a)
        side = z.get("side")
        if side == "support" and a is not None and a <= zh:
            hits.append(Hit(side="support", edge=zh, hit_price=a, **common))
        elif side == "resistance" and b is not None and b >= zl:
            hits.append(Hit(side="resistance", edge=zl, hit_price=b, **common))
    return hits
