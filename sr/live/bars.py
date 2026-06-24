"""Pure tick -> 1-minute OHLCV bars (engine master schema). The headline seam.

Bar timestamp = exchangeTimeNs (broker-authoritative), fall back to receive ts. Price per tick =
trade if usable, else mid of an uncrossed book, else the one usable side. Volume per minute = the
within-minute delta of totalTradedQuantity (never accumulated -> no double count), else sum of
trade_qty, else 0 (quote-only is first-class). Only CLOSED minutes are emitted."""
from __future__ import annotations

import math

import pandas as pd

_COLS = ["datetime", "open", "high", "low", "close", "volume", "instrument"]


def _finite(v) -> bool:
    """A usable PRICE: finite number, not NaN/None/inf/bool. NOT > 0 — spread instruments
    (FLY/1MS, the majority of the universe) legitimately quote negative or zero."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def bar_price(rec: dict):
    """Representative price for a tick, or None if nothing usable. Rejects a crossed (bid>ask)
    book and NaN/None — but accepts negative/zero prices (valid for spreads). Never invents a price."""
    trade = rec.get("trade")
    if _finite(trade):
        return float(trade)
    bid, ask = rec.get("bid"), rec.get("ask")
    b_ok, a_ok = _finite(bid), _finite(ask)
    if b_ok and a_ok:
        return (float(bid) + float(ask)) / 2.0 if bid <= ask else None
    if b_ok:
        return float(bid)
    if a_ok:
        return float(ask)
    return None


def _tick_time(rec):
    ns = rec.get("exchange_time_ns")
    if isinstance(ns, (int, float)) and not isinstance(ns, bool) and math.isfinite(ns) and ns > 0:
        try:
            return pd.Timestamp(int(ns), unit="ns")        # UTC-based, tz-naive
        except (ValueError, OverflowError):
            pass
    ts = rec.get("ts")
    if ts:
        t = pd.to_datetime(ts, utc=True, errors="coerce")
        if pd.notna(t):
            return t.tz_convert(None)
    return None


def bars_from_ticks(ticks, instrument, drop_unclosed_after=None) -> pd.DataFrame:
    """ticks -> master-schema 1-min OHLCV frame. ``drop_unclosed_after`` (a datetime) drops the
    in-progress minute (>= floor(drop_unclosed_after, '1min')). OHLC-valid by construction;
    enforced defensively. Empty/all-unusable -> empty frame with the right columns."""
    rows = []
    for rec in ticks:
        p = bar_price(rec)
        if p is None:
            continue
        t = _tick_time(rec)
        if t is None:
            continue
        rows.append((t, p, rec.get("total_traded_qty"), rec.get("trade_qty")))
    if not rows:
        return pd.DataFrame(columns=_COLS)

    df = pd.DataFrame(rows, columns=["datetime", "price", "ttq", "tq"]).sort_values("datetime")
    df["minute"] = df["datetime"].dt.floor("1min")
    if drop_unclosed_after is not None:
        cutoff = pd.Timestamp(drop_unclosed_after)
        if cutoff.tzinfo is not None:                 # bars are tz-naive UTC; normalize the cutoff
            cutoff = cutoff.tz_convert("UTC").tz_localize(None)
        cutoff = cutoff.floor("1min")
        df = df[df["minute"] < cutoff]
    if df.empty:
        return pd.DataFrame(columns=_COLS)

    out = []
    for minute, g in df.groupby("minute", sort=True):
        prices = g["price"]
        ttq = pd.to_numeric(g["ttq"], errors="coerce").dropna()
        if len(ttq):
            vol = float(max(0.0, float(ttq.max()) - float(ttq.min())))
        else:
            tq = pd.to_numeric(g["tq"], errors="coerce").dropna()
            vol = float(tq.clip(lower=0).sum()) if len(tq) else 0.0
        out.append({"datetime": minute, "open": float(prices.iloc[0]), "high": float(prices.max()),
                    "low": float(prices.min()), "close": float(prices.iloc[-1]),
                    "volume": vol, "instrument": instrument})

    res = pd.DataFrame(out, columns=_COLS)
    from ..engine import ohlc_invalid_mask     # local import: avoids any import cycle at module load
    return res[~ohlc_invalid_mask(res)].reset_index(drop=True)
