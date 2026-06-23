"""Generate a SYNTHETIC OHLC Excel file in the QH format for demos/tests.

No real market data is used. The series is a seeded random walk so output is
reproducible. Columns match the QH export:
    Date, Open, High, Low, Volume, Close, BuyVolume, SellVolume, isNewCandle
with ISO-8601 UTC timestamps like 2026-02-18T13:30:00.000Z.

Usage:
    python scripts/make_sample_data.py
    python scripts/make_sample_data.py --instrument DEMO --days 60 --interval 15min --out sample_data/SAMPLE_15min.xlsx
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def build(instrument: str, days: int, interval: str, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # weekday intraday session 13:30–20:00 UTC
    start = pd.Timestamp("2026-01-05 13:30:00")
    sessions = pd.bdate_range(start.normalize(), periods=days)
    stamps = []
    for day in sessions:
        day_start = day + pd.Timedelta(hours=13, minutes=30)
        day_end = day + pd.Timedelta(hours=20)
        stamps.extend(pd.date_range(day_start, day_end, freq=interval))
    idx = pd.DatetimeIndex(stamps)

    n = len(idx)
    # random walk close with mild mean reversion so S/R levels actually form
    price = 100.0
    closes = np.empty(n)
    for i in range(n):
        shock = rng.normal(0, 0.15) - 0.01 * (price - 100.0)
        price += shock
        closes[i] = price
    opens = np.concatenate([[closes[0]], closes[:-1]])
    spread = np.abs(rng.normal(0, 0.12, n)) + 0.03
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    vol = rng.integers(50, 1500, n)
    buy = (vol * rng.uniform(0.3, 0.7, n)).astype(int)

    return pd.DataFrame({
        "Date": idx.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "Open": opens.round(2), "High": highs.round(2), "Low": lows.round(2),
        "Volume": vol, "Close": closes.round(2),
        "BuyVolume": buy, "SellVolume": vol - buy,
        "isNewCandle": 1.0,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="DEMO")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--interval", default="15min")
    ap.add_argument("--out", default="sample_data/SAMPLE_15min.xlsx")
    args = ap.parse_args()

    df = build(args.instrument, args.days, args.interval)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Data")
    print(f"Wrote {len(df)} synthetic {args.interval} bars -> {out}")


if __name__ == "__main__":
    main()
