"""Pure support/resistance engine — no file IO, no UI framework.

This is the verified core: instrument-name parsing, OHLCV normalization, per
-instrument native-interval detection, multi-timeframe resampling, ATR, swing
detection, level clustering, and confluence scoring. All functions are pure
(DataFrame in / DataFrame out) so they can be reused by the desktop app, the
Streamlit app, and the tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Defaults (shared by every UI so behaviour is identical everywhere)
# ---------------------------------------------------------------------------
DEFAULT_TIMEFRAMES = ["15min", "1h", "4h", "1D", "1W"]
DEFAULT_SWING_WINDOWS = {"15min": 8, "1h": 6, "4h": 5, "1D": 4, "1W": 3}
DEFAULT_TIMEFRAME_WEIGHTS = {"15min": 1.0, "1h": 2.0, "4h": 3.0, "1D": 4.0, "1W": 5.0}


@dataclass
class SRConfig:
    """All tunables for one S/R run, with the same defaults the UI exposes."""
    timeframes: list = field(default_factory=lambda: ["15min", "1h", "4h", "1D"])
    atr_period: int = 14
    atr_multiplier: float = 0.25
    cluster_atr_multiplier: float = 0.35
    min_score: float = 3.0
    max_distance_atr: float = 10.0
    min_zone_width: float = 0.005
    min_bars: int = 30
    lookback: int = 300
    swing_windows: dict = field(default_factory=lambda: dict(DEFAULT_SWING_WINDOWS))
    timeframe_weights: dict = field(default_factory=lambda: dict(DEFAULT_TIMEFRAME_WEIGHTS))


# ---------------------------------------------------------------------------
# Instrument naming
# ---------------------------------------------------------------------------
def clean_instrument_name(file_name: str) -> str:
    """Convert an Excel file name into an instrument name.

    'QH Charts22.6.26.xlsx'        -> 'QH'
    'BRN_Oct26_15min_20260622.xlsx'-> 'BRN Oct26'
    """
    from pathlib import Path

    stem = Path(file_name).stem
    stem = re.sub(r"(?i)charts?", " ", stem)
    stem = re.sub(r"(?i)15\s*min|15m|hourly|daily|data", " ", stem)
    stem = re.sub(r"\d{1,2}[\.\-_]\d{1,2}[\.\-_]\d{2,4}", " ", stem)
    stem = re.sub(r"\d{6,8}", " ", stem)
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem if stem else Path(file_name).stem


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_ohlcv(df: pd.DataFrame, instrument: str) -> pd.DataFrame:
    """Normalize a raw QH-format (or lowercase OHLCV) frame to a clean schema.

    Returns columns: datetime, open, high, low, close, volume, [buy/sell volume],
    instrument. Rows whose date or OHLC cannot be parsed are dropped (the caller
    is expected to report how many were lost).
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    lower_map = {c.lower().strip(): c for c in df.columns}

    date_candidates = ["datetime", "date", "time", "timestamp"]
    date_col = next((lower_map[c] for c in date_candidates if c in lower_map), None)
    if date_col is None:
        raise ValueError(f"No date/datetime column found. Columns: {list(df.columns)}")

    def find_col(names):
        for n in names:
            if n in lower_map:
                return lower_map[n]
        return None

    col_map = {
        "datetime": date_col,
        "open": find_col(["open", "o"]),
        "high": find_col(["high", "h"]),
        "low": find_col(["low", "l"]),
        "close": find_col(["close", "c", "last"]),
        "volume": find_col(["volume", "vol"]),
        "buy_volume": find_col(["buyvolume", "buy_volume", "buy vol", "buyvol"]),
        "sell_volume": find_col(["sellvolume", "sell_volume", "sell vol", "sellvol"]),
    }

    missing = [k for k in ["open", "high", "low", "close"] if col_map[k] is None]
    if missing:
        raise ValueError(f"Missing OHLC columns: {missing}. Columns found: {list(df.columns)}")

    out = pd.DataFrame()
    out["datetime"] = pd.to_datetime(df[col_map["datetime"]], utc=True, errors="coerce").dt.tz_convert(None)
    for c in ["open", "high", "low", "close"]:
        out[c] = pd.to_numeric(df[col_map[c]], errors="coerce")

    if col_map["volume"] is not None:
        out["volume"] = pd.to_numeric(df[col_map["volume"]], errors="coerce").fillna(0)
    else:
        out["volume"] = 0
    if col_map["buy_volume"] is not None:
        out["buy_volume"] = pd.to_numeric(df[col_map["buy_volume"]], errors="coerce")
    if col_map["sell_volume"] is not None:
        out["sell_volume"] = pd.to_numeric(df[col_map["sell_volume"]], errors="coerce")

    out["instrument"] = instrument
    out = out.dropna(subset=["datetime", "open", "high", "low", "close"])
    out = out.sort_values(["instrument", "datetime"]).drop_duplicates(["instrument", "datetime"], keep="last")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# S/R math
# ---------------------------------------------------------------------------
def resample_ohlcv(df_inst: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    d = df_inst.copy().set_index("datetime").sort_index()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return d.resample(timeframe).agg(agg).dropna(subset=["open", "high", "low", "close"]).reset_index()


def add_atr(df: pd.DataFrame, period: int) -> pd.DataFrame:
    d = df.copy()
    prev_close = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["tr"] = tr
    d["atr"] = tr.rolling(period, min_periods=max(2, period // 2)).mean().bfill().ffill()
    return d


def detect_swings(df: pd.DataFrame, window: int) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    d["swing_high"] = False
    d["swing_low"] = False
    if len(d) < 2 * window + 1:
        return d
    highs = d["high"].to_numpy()
    lows = d["low"].to_numpy()
    for i in range(window, len(d) - window):
        if highs[i] >= max(highs[i - window:i].max(), highs[i + 1:i + window + 1].max()):
            d.loc[i, "swing_high"] = True
        if lows[i] <= min(lows[i - window:i].min(), lows[i + 1:i + window + 1].min()):
            d.loc[i, "swing_low"] = True
    return d


def extract_levels(df: pd.DataFrame, instrument: str, timeframe: str) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame()
    current_price = float(df["close"].iloc[-1])
    current_atr = float(df["atr"].iloc[-1]) if "atr" in df.columns and pd.notna(df["atr"].iloc[-1]) else np.nan

    for _, r in df[df["swing_high"]].iterrows():
        rows.append({"instrument": instrument, "timeframe": timeframe, "datetime": r["datetime"], "level": float(r["high"]), "side": "resistance", "atr": float(r["atr"]), "current_price": current_price, "current_atr": current_atr})
    for _, r in df[df["swing_low"]].iterrows():
        rows.append({"instrument": instrument, "timeframe": timeframe, "datetime": r["datetime"], "level": float(r["low"]), "side": "support", "atr": float(r["atr"]), "current_price": current_price, "current_atr": current_atr})
    return pd.DataFrame(rows)


def cluster_levels(levels_df: pd.DataFrame, atr_multiplier: float, cluster_atr_multiplier: float, min_zone_width: float) -> pd.DataFrame:
    if levels_df.empty:
        return pd.DataFrame()
    out = []
    for (inst, tf, side), g in levels_df.groupby(["instrument", "timeframe", "side"]):
        g = g.sort_values("level").reset_index(drop=True)
        median_atr = g["atr"].replace([np.inf, -np.inf], np.nan).median()
        if pd.isna(median_atr) or median_atr == 0:
            median_atr = min_zone_width * 4
        cluster_width = cluster_atr_multiplier * median_atr
        zone_width = max(atr_multiplier * median_atr, min_zone_width)

        clusters = []
        cur = [g.iloc[0].to_dict()]
        for i in range(1, len(g)):
            row = g.iloc[i].to_dict()
            center = np.mean([x["level"] for x in cur])
            if abs(row["level"] - center) <= cluster_width:
                cur.append(row)
            else:
                clusters.append(cur)
                cur = [row]
        clusters.append(cur)

        for cl in clusters:
            center = float(np.mean([x["level"] for x in cl]))
            out.append({
                "instrument": inst, "timeframe": tf, "side": side,
                "zone_center": center,
                "zone_low": center - zone_width,
                "zone_high": center + zone_width,
                "touches": len(cl),
                "first_touch": min(x["datetime"] for x in cl),
                "last_touch": max(x["datetime"] for x in cl),
                "atr": median_atr,
                "current_price": float(cl[-1]["current_price"]),
                "current_atr": float(cl[-1]["current_atr"]),
            })
    return pd.DataFrame(out)


def score_and_merge(zones_df: pd.DataFrame, timeframe_weights: dict, min_score: float, max_distance_atr: float, cluster_atr_multiplier: float) -> pd.DataFrame:
    if zones_df.empty:
        return zones_df
    d = zones_df.copy()
    latest = d.groupby("instrument")["last_touch"].transform("max")
    days_since = (latest - d["last_touch"]).dt.total_seconds() / 86400
    d["recency_score"] = np.select([days_since <= 7, days_since <= 30, days_since <= 90], [3.0, 2.0, 1.0], default=0.5)
    d["timeframe_score"] = d["timeframe"].map(timeframe_weights).fillna(1.0)
    d["touch_score"] = np.minimum(d["touches"], 5) * 0.8
    d["distance_atr"] = (d["zone_center"] - d["current_price"]).abs() / d["current_atr"].replace(0, np.nan)
    d["distance_penalty"] = np.where(d["distance_atr"] > max_distance_atr, 2.0, 0.0)
    d["base_score"] = d["timeframe_score"] + d["touch_score"] + d["recency_score"] - d["distance_penalty"]

    # Classify each zone by its position relative to the CURRENT price, not by the
    # historical swing type. A former swing low that now sits above price is acting
    # as resistance today (broken support -> resistance), and vice-versa. This makes
    # the zone table, chart bands, and summary cards agree: every 'support' is below
    # price and every 'resistance' is above it. (Swing *markers* keep their type.)
    d["side"] = np.where(d["zone_center"] <= d["current_price"], "support", "resistance")

    final = []
    for (inst, side), g in d.sort_values("zone_center").groupby(["instrument", "side"]):
        g = g.sort_values("zone_center").reset_index(drop=True)
        median_atr = g["atr"].median()
        if pd.isna(median_atr) or median_atr == 0:
            median_atr = 0.01
        merge_width = cluster_atr_multiplier * median_atr
        clusters = []
        cur = [g.iloc[0].to_dict()]
        for i in range(1, len(g)):
            row = g.iloc[i].to_dict()
            center = np.average([x["zone_center"] for x in cur], weights=[max(x["base_score"], 0.1) for x in cur])
            if abs(row["zone_center"] - center) <= merge_width:
                cur.append(row)
            else:
                clusters.append(cur)
                cur = [row]
        clusters.append(cur)

        for cl in clusters:
            weights = np.array([max(x["base_score"], 0.1) for x in cl])
            centers = np.array([x["zone_center"] for x in cl])
            center = float(np.average(centers, weights=weights))
            tfs = sorted(set(x["timeframe"] for x in cl), key=lambda z: timeframe_weights.get(z, 0), reverse=True)
            score = float(sum(x["base_score"] for x in cl)) + max(0, len(tfs) - 1) * 2.0
            current_price = float(cl[-1]["current_price"])
            current_atr = float(cl[-1]["current_atr"])
            final.append({
                "instrument": inst,
                "side": side,
                "zone_low": min(x["zone_low"] for x in cl),
                "zone_high": max(x["zone_high"] for x in cl),
                "zone_center": center,
                "score": round(score, 2),
                "touches": int(sum(x["touches"] for x in cl)),
                "timeframes": ", ".join(tfs),
                "timeframe_count": len(tfs),
                "last_touch": max(x["last_touch"] for x in cl),
                "current_price": current_price,
                "distance_atr": round(abs(center - current_price) / current_atr, 2) if current_atr else np.nan,
            })

    out = pd.DataFrame(final)
    if out.empty:
        return out
    out = out[(out["score"] >= min_score) & (out["distance_atr"] <= max_distance_atr)]
    return out.sort_values(["instrument", "score"], ascending=[True, False]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Per-instrument timeframe adaptation
# ---------------------------------------------------------------------------
def infer_native_interval(df_inst: pd.DataFrame):
    """Most common positive gap between consecutive bars for one instrument."""
    s = df_inst["datetime"].sort_values().diff().dropna()
    s = s[s > pd.Timedelta(0)]
    if s.empty:
        return None
    return s.mode().iloc[0]


def tf_to_timedelta(tf: str):
    """Approximate a timeframe string ('15min','1h','4h','1D','1W') as a Timedelta.

    pandas 3.0 cannot cast Day/Week offsets to Timedelta directly, so map by name.
    """
    try:
        off = pd.tseries.frequencies.to_offset(tf)
    except Exception:
        return None
    unit = off.name.split("-")[0]  # 'min', 'h', 'D', 'W' (e.g. 'W-SUN')
    days_per = {"D": 1, "B": 1, "W": 7}.get(unit)
    if days_per is not None:
        return pd.Timedelta(days=off.n * days_per)
    try:
        return pd.Timedelta(off)  # tick offsets: min / h / s
    except Exception:
        return None


def select_effective_timeframes(requested, native):
    """Drop timeframes finer than the instrument's native bar interval.

    Resampling hourly data to '15min' merely reproduces the hourly bars under a
    different label, so a level would be counted under both '15min' and '1h' and
    double-count in scoring. Returns (effective_list, skipped_dict{tf: reason}).
    """
    if native is None:
        return list(requested), {}
    effective, skipped = [], {}
    tol = native * 0.99
    for tf in requested:
        td = tf_to_timedelta(tf)
        if td is not None and td < tol:
            skipped[tf] = f"finer than native bar interval ({native}); would duplicate a coarser timeframe"
        else:
            effective.append(tf)
    if not effective:  # only finer-than-native were requested: fall back to the coarsest one
        coarsest = max(requested, key=lambda t: tf_to_timedelta(t) or pd.Timedelta(0))
        effective = [coarsest]
        skipped.pop(coarsest, None)
    return effective, skipped


def run_sr(df_master, timeframes, swing_windows, timeframe_weights, atr_period, atr_multiplier,
           cluster_atr_multiplier, min_score, max_distance_atr, min_zone_width, min_bars=30):
    """Full pipeline. Returns (final_zones, raw_levels, timeframe_zones, tf_data, diagnostics)."""
    all_levels, all_tf_zones, tf_data = [], [], {}
    diagnostics = []
    for inst, df_inst in df_master.groupby("instrument"):
        tf_data[inst] = {}
        native = infer_native_interval(df_inst)
        effective_tfs, skipped = select_effective_timeframes(timeframes, native)
        for tf, reason in skipped.items():
            diagnostics.append({"instrument": inst, "timeframe": tf, "status": "skipped", "bars": 0, "native": str(native), "reason": reason})
        for tf in effective_tfs:
            df_tf = resample_ohlcv(df_inst, tf)
            if len(df_tf) < min_bars:
                diagnostics.append({"instrument": inst, "timeframe": tf, "status": "skipped", "bars": len(df_tf), "native": str(native), "reason": f"only {len(df_tf)} bars (need >= {min_bars})"})
                continue
            df_tf = add_atr(df_tf, atr_period)
            df_tf = detect_swings(df_tf, swing_windows.get(tf, 5))
            levels = extract_levels(df_tf, inst, tf)
            n_zones = 0
            if not levels.empty:
                zones = cluster_levels(levels, atr_multiplier, cluster_atr_multiplier, min_zone_width)
                all_levels.append(levels)
                if not zones.empty:
                    all_tf_zones.append(zones)
                    n_zones = len(zones)
            tf_data[inst][tf] = df_tf
            diagnostics.append({"instrument": inst, "timeframe": tf, "status": "used", "bars": len(df_tf), "native": str(native), "reason": f"{n_zones} raw zones"})
    raw_levels = pd.concat(all_levels, ignore_index=True) if all_levels else pd.DataFrame()
    timeframe_zones = pd.concat(all_tf_zones, ignore_index=True) if all_tf_zones else pd.DataFrame()
    final_zones = score_and_merge(timeframe_zones, timeframe_weights, min_score, max_distance_atr, cluster_atr_multiplier)
    return final_zones, raw_levels, timeframe_zones, tf_data, pd.DataFrame(diagnostics)


def compute_sr(df_master: pd.DataFrame, config: SRConfig):
    """Convenience wrapper that runs ``run_sr`` from an :class:`SRConfig`."""
    return run_sr(
        df_master, config.timeframes, config.swing_windows, config.timeframe_weights,
        config.atr_period, config.atr_multiplier, config.cluster_atr_multiplier,
        config.min_score, config.max_distance_atr, config.min_zone_width, config.min_bars,
    )
