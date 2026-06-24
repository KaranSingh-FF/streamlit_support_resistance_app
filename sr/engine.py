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
MAX_ZONE_ATR = 1.5   # merged zones wider than this (in ATR) are split into equal sub-zones


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
    tick_size: float = 0.01            # snap S/R levels to this price increment (0 = off)
    use_close_for_swings: bool = False  # detect swings on closes (ignore wicks) instead of high/low
    swing_windows: dict = field(default_factory=lambda: dict(DEFAULT_SWING_WINDOWS))
    timeframe_weights: dict = field(default_factory=lambda: dict(DEFAULT_TIMEFRAME_WEIGHTS))
    auto: bool = False                            # desktop sets True; engine/tests default False = no behavior change
    overrides: set = field(default_factory=set)   # tunable keys the user pinned (auto inference skips these)


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
def _parse_ohlcv(df: pd.DataFrame, instrument: str) -> pd.DataFrame:
    """Parse a raw QH-format (or lowercase OHLCV) frame to the clean schema and drop
    rows whose date/OHLC cannot be parsed. Does NOT apply OHLC logical checks or
    dedup — callers (normalize_ohlcv / normalize_ohlcv_split) decide that.

    Returns columns: datetime, open, high, low, close, volume, [buy/sell volume], instrument.
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
    return out.dropna(subset=["datetime", "open", "high", "low", "close"])


def ohlc_invalid_mask(out: pd.DataFrame) -> pd.Series:
    """True for bars that violate OHLC logic: High must be the bar's max and Low its
    min (High>=Low, High>=max(O,C), Low<=min(O,C)). Negative-price and zero-range
    bars are valid."""
    hi, lo = out["high"], out["low"]
    oc_max = out[["open", "close"]].max(axis=1)
    oc_min = out[["open", "close"]].min(axis=1)
    return ~((hi >= lo) & (hi >= oc_max) & (lo <= oc_min))


def ohlc_invalid_reason(row) -> str:
    """Human-readable explanation of why a bar fails the OHLC checks."""
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rs = []
    if h < l:
        rs.append("High < Low")
    if h < max(o, c):
        rs.append("High < " + ("Open" if o >= c else "Close"))
    if l > min(o, c):
        rs.append("Low > " + ("Open" if o <= c else "Close"))
    return "; ".join(rs) or "invalid OHLC"


def _finalize_frame(out: pd.DataFrame) -> pd.DataFrame:
    return (out.sort_values(["instrument", "datetime"])
               .drop_duplicates(["instrument", "datetime"], keep="last")
               .reset_index(drop=True))


def normalize_ohlcv(df: pd.DataFrame, instrument: str) -> pd.DataFrame:
    """Normalize a raw frame and drop OHLC-invalid bars (the non-interactive path)."""
    out = _parse_ohlcv(df, instrument)
    return _finalize_frame(out[~ohlc_invalid_mask(out)])


def normalize_ohlcv_split(df: pd.DataFrame, instrument: str):
    """Parse a raw frame and return (valid, invalid) OHLC bars separately, so a UI
    can ask the user whether to keep or remove the invalid ones."""
    out = _parse_ohlcv(df, instrument)
    bad = ohlc_invalid_mask(out)
    return _finalize_frame(out[~bad]), _finalize_frame(out[bad])


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


def detect_swings(df: pd.DataFrame, window: int, use_close: bool = False) -> pd.DataFrame:
    """Mark swing highs/lows. With use_close=True, pivots are found on the close
    series (ignoring wicks) — useful for spread instruments where wicks are noise."""
    d = df.copy().reset_index(drop=True)
    d["swing_high"] = False
    d["swing_low"] = False
    if len(d) < 2 * window + 1:
        return d
    highs = (d["close"] if use_close else d["high"]).to_numpy()
    lows = (d["close"] if use_close else d["low"]).to_numpy()
    for i in range(window, len(d) - window):
        if highs[i] >= max(highs[i - window:i].max(), highs[i + 1:i + window + 1].max()):
            d.loc[i, "swing_high"] = True
        if lows[i] <= min(lows[i - window:i].min(), lows[i + 1:i + window + 1].min()):
            d.loc[i, "swing_low"] = True
    return d


def extract_levels(df: pd.DataFrame, instrument: str, timeframe: str, use_close: bool = False) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame()
    current_price = float(df["close"].iloc[-1])
    current_atr = float(df["atr"].iloc[-1]) if "atr" in df.columns and pd.notna(df["atr"].iloc[-1]) else np.nan
    hi_src = "close" if use_close else "high"
    lo_src = "close" if use_close else "low"

    for _, r in df[df["swing_high"]].iterrows():
        rows.append({"instrument": instrument, "timeframe": timeframe, "datetime": r["datetime"], "level": float(r[hi_src]), "side": "resistance", "atr": float(r["atr"]), "current_price": current_price, "current_atr": current_atr})
    for _, r in df[df["swing_low"]].iterrows():
        rows.append({"instrument": instrument, "timeframe": timeframe, "datetime": r["datetime"], "level": float(r[lo_src]), "side": "support", "atr": float(r["atr"]), "current_price": current_price, "current_atr": current_atr})
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


def snap_to_tick(value, tick: float, mode: str = "round"):
    """Round a price to the instrument's tick grid (mode: round / floor / ceil)."""
    if not tick or tick <= 0 or value is None or not np.isfinite(value):
        return value
    q = value / tick
    q = np.floor(q) if mode == "floor" else np.ceil(q) if mode == "ceil" else np.round(q)
    return round(float(q) * tick, 10)


def score_and_merge(zones_df: pd.DataFrame, timeframe_weights: dict, min_score: float, max_distance_atr: float, cluster_atr_multiplier: float, tick_size: float = 0.0) -> pd.DataFrame:
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

    def _emit(members, inst):
        """Snap + score one (sub-)cluster and append the final-zone dict."""
        weights = np.array([max(x["base_score"], 0.1) for x in members])
        centers = np.array([x["zone_center"] for x in members])
        center = float(np.average(centers, weights=weights))
        raw_low = min(x["zone_low"] for x in members)
        raw_high = max(x["zone_high"] for x in members)
        if tick_size and tick_size > 0:
            center = snap_to_tick(center, tick_size, "round")
            zlow = snap_to_tick(raw_low, tick_size, "floor")
            zhigh = snap_to_tick(raw_high, tick_size, "ceil")
            if zhigh - zlow < 2 * tick_size:
                zlow = round(center - tick_size, 10)
                zhigh = round(center + tick_size, 10)
            center = min(max(center, zlow), zhigh)
        else:
            zlow, zhigh = raw_low, raw_high
        tfs = sorted(set(x["timeframe"] for x in members), key=lambda z: timeframe_weights.get(z, 0), reverse=True)
        score = float(sum(x["base_score"] for x in members)) + max(0, len(tfs) - 1) * 2.0
        current_price = float(members[-1]["current_price"])
        current_atr = float(members[-1]["current_atr"])
        final.append({
            "instrument": inst,
            # side from the SNAPPED center, so support<=price / resistance>=price holds
            # even when snapping nudges a zone across the current price.
            "side": "support" if center <= current_price else "resistance",
            "zone_low": zlow, "zone_high": zhigh, "zone_center": center,
            "score": round(score, 2),
            "touches": int(sum(x["touches"] for x in members)),
            "timeframes": ", ".join(tfs),
            "timeframe_count": len(tfs),
            "last_touch": max(x["last_touch"] for x in members),
            "current_price": current_price,
            "current_atr": current_atr,   # kept so the cards can measure edge-distance in ATR
            "distance_atr": round(abs(center - current_price) / current_atr, 2) if current_atr else np.nan,
        })

    for (inst, side), g in d.sort_values("zone_center").groupby(["instrument", "side"]):
        # Sort by zone_low (not center): the greedy running-max-high overlap sweep
        # below is only correct when intervals are visited in start-edge order.
        g = g.sort_values("zone_low").reset_index(drop=True)
        median_atr = g["atr"].median()
        if pd.isna(median_atr) or median_atr == 0:
            median_atr = 0.01
        merge_width = cluster_atr_multiplier * median_atr
        clusters = []
        cur = [g.iloc[0].to_dict()]
        cur_high = cur[0]["zone_high"]
        for i in range(1, len(g)):
            row = g.iloc[i].to_dict()
            center = np.average([x["zone_center"] for x in cur], weights=[max(x["base_score"], 0.1) for x in cur])
            # Merge if centers are close OR the ranges already overlap. The overlap
            # test is the real fix for stacked, unreadable bands: zone width is set
            # per-timeframe, so a wide higher-TF zone can overlap a neighbour whose
            # center sits beyond merge_width (which uses a single per-side median ATR).
            # g is sorted by zone_low, so row.zone_low <= running max high == overlap.
            if abs(row["zone_center"] - center) <= merge_width or row["zone_low"] <= cur_high:
                cur.append(row)
                cur_high = max(cur_high, row["zone_high"])
            else:
                clusters.append(cur)
                cur = [row]
                cur_high = row["zone_high"]
        clusters.append(cur)

        for cl in clusters:
            raw_low = min(x["zone_low"] for x in cl)
            raw_high = max(x["zone_high"] for x in cl)
            width_atr = (raw_high - raw_low) / median_atr if median_atr else 0.0
            n_parts = int(np.ceil(width_atr / MAX_ZONE_ATR)) if width_atr > MAX_ZONE_ATR else 1
            if n_parts > 1:   # split a too-wide zone into equal sub-zones, each member in exactly one
                edges = np.linspace(raw_low, raw_high, n_parts + 1)
                bins = np.clip(np.searchsorted(edges, [x["zone_center"] for x in cl], side="right") - 1,
                               0, n_parts - 1)
                for k in range(n_parts):
                    members = [cl[j] for j in range(len(cl)) if bins[j] == k]
                    if members:
                        _emit(members, inst)
            else:
                _emit(cl, inst)

    out = pd.DataFrame(final)
    if out.empty:
        return out

    # --- enrichment (additive columns): recency, confidence composite, distance bucket ---
    # All position/recency references are PER-INSTRUMENT (current_price/current_atr are per-row;
    # last_touch is maxed within each instrument) so a multi-instrument frame isn't cross-contaminated.
    cp = out["current_price"].to_numpy()
    catr = out["current_atr"].to_numpy()
    latest = out.groupby("instrument")["last_touch"].transform("max")
    out["days_since_touch"] = (latest - out["last_touch"]).dt.total_seconds() / 86400.0
    days = out["days_since_touch"].fillna(1e9).to_numpy()
    out["recency_weight"] = np.power(0.5, days / 30.0).clip(0.0, 1.0)   # 30-day half-life
    tf_conf = out["timeframe_count"].clip(upper=4) / 4.0
    tmax = out["touches"].max() or 1
    touch_n = np.log1p(out["touches"]) / np.log1p(tmax)
    srange = (out["score"].max() - out["score"].min()) or 1.0
    score_n = (out["score"] - out["score"].min()) / srange
    out["confidence_score"] = (0.35 * score_n + 0.28 * touch_n
                               + 0.22 * out["recency_weight"] + 0.15 * tf_conf).round(4)
    zh, zl = out["zone_high"].to_numpy(), out["zone_low"].to_numpy()
    edge = np.where(zh < cp, cp - zh, np.where(zl > cp, zl - cp, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        edge_atr = np.where((catr > 0) & np.isfinite(catr), edge / catr, np.inf)
    out["bucket"] = np.select([(edge_atr <= 1.5) & (days <= 30), days > 90],
                              ["active", "historical"], default="nearby")
    # validation: drop impossible inverted zones (negative prices stay valid for spreads)
    out = out[(out["zone_high"] - out["zone_low"]) > 0]
    if out.empty:
        return out

    # Tick-snapping can collapse two very-close clusters onto the same grid price;
    # keep the higher-scored one.
    if tick_size and tick_size > 0:
        out = out.sort_values("score", ascending=False).drop_duplicates(["instrument", "side", "zone_center"], keep="first")
    out = out[(out["score"] >= min_score) & (out["distance_atr"] <= max_distance_atr)]
    return out.sort_values(["instrument", "score"], ascending=[True, False]).reset_index(drop=True)


def attach_volume_at_level(final_zones: pd.DataFrame, tf_data_inst: dict) -> pd.DataFrame:
    """Add a ``volume_at_level`` column = total bar volume overlapping each zone, summed
    on the COARSEST used timeframe only (no multi-TF double count). 0.0 when volume is
    absent/zero — never NaN, so the strict-JSON bridge holds."""
    if final_zones is None or final_zones.empty:
        return final_zones
    out = final_zones.copy()
    bars = None
    if tf_data_inst:
        coarsest = max(tf_data_inst, key=lambda t: tf_to_timedelta(t) or pd.Timedelta(0))
        df = tf_data_inst.get(coarsest)
        if df is not None and len(df) and "volume" in df.columns and float(df["volume"].fillna(0).abs().sum()) > 0:
            bars = df
    if bars is None:
        out["volume_at_level"] = 0.0
        return out
    lo = bars["low"].to_numpy(); hi = bars["high"].to_numpy()
    vol = bars["volume"].fillna(0).clip(lower=0).to_numpy()

    def _vol(zl, zh):
        v = float(vol[(hi >= zl) & (lo <= zh)].sum())
        return v if np.isfinite(v) else 0.0

    out["volume_at_level"] = [_vol(r.zone_low, r.zone_high) for r in out.itertuples()]
    return out


def _finalize_zones(final_zones: pd.DataFrame, tf_data: dict) -> pd.DataFrame:
    """Attach volume-at-level, fold it into the confidence composite, and assign the
    High/Medium/Low confidence label by percentile across the run. Run once at the tail
    of the pipeline so volume and confidence are computed on the FINAL (filtered) zones."""
    if final_zones is None or final_zones.empty:
        return final_zones
    parts = [attach_volume_at_level(g, tf_data.get(inst, {})) for inst, g in final_zones.groupby("instrument")]
    out = pd.concat(parts, ignore_index=True)
    out["volume_at_level"] = out["volume_at_level"].fillna(0.0)
    if "confidence_score" in out.columns:
        cs = out["confidence_score"].astype(float)
        if float(out["volume_at_level"].sum()) > 0:
            vmax = float(out["volume_at_level"].max()) or 1.0
            cs = (0.90 * cs + 0.10 * (out["volume_at_level"] / vmax)).round(4)
        out["confidence_score"] = cs
        q = out["confidence_score"].rank(pct=True)
        out["confidence"] = np.select([q >= 0.66, q >= 0.33], ["High", "Medium"], default="Low")
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


# ---------------------------------------------------------------------------
# Zero-config parameter inference (used when SRConfig.auto and the key isn't pinned)
# ---------------------------------------------------------------------------
def _pooled_prices(df_master) -> np.ndarray:
    cols = [c for c in ("open", "high", "low", "close") if c in df_master.columns]
    if not cols:
        return np.array([])
    s = pd.concat([df_master[c] for c in cols], ignore_index=True).dropna()
    return pd.unique(s.to_numpy())


def infer_tick_size(df_master) -> float:
    """Tick = 10**-(decimals used by ~95% of prices). Robust to a few high-precision
    float-noise values; integer-priced data -> 1.0; <2 distinct prices -> 0.0 (off).
    Sign-agnostic, so negative-price spreads work."""
    p = _pooled_prices(df_master)
    if p.size < 2:
        return 0.0
    decs = []
    for v in p:
        s = f"{abs(float(v)):.8f}".rstrip("0").rstrip(".")
        decs.append(len(s.split(".")[1]) if "." in s else 0)
    d = int(np.percentile(np.array(decs), 95))
    d = min(max(d, 0), 6)
    return round(10.0 ** (-d), 6) if d > 0 else 1.0


def infer_lookback(df_master) -> int:
    """Chart bars per timeframe: ~400, capped to data length, floored at 50."""
    n = len(df_master)
    return 50 if n <= 0 else max(50, min(n, 400))


def infer_zone_width(df_master, tick_size) -> float:
    """Zone half-width as a fraction of ATR (the atr_multiplier). Keeps the 0.25 default
    but widens just enough that a zone always spans >= 2 ticks; clipped to [0.10, 0.50]."""
    if df_master.empty or not {"high", "low"}.issubset(df_master.columns):
        return 0.25
    rng = float((df_master["high"] - df_master["low"]).median())
    mult = 0.25
    if np.isfinite(rng) and rng > 0 and tick_size and tick_size > 0 and mult * rng < 2 * tick_size:
        mult = (2 * tick_size) / rng
    return float(min(max(mult, 0.10), 0.50))


def infer_min_score(merged_zones) -> float:
    """40th-percentile of the (unfiltered) merged scores, clamped to [1.0, 85th pct].
    <=3 zones -> 0.0 (keep everything; nothing to threshold on)."""
    if merged_zones is None or merged_zones.empty or "score" not in merged_zones.columns:
        return 0.0
    s = merged_zones["score"].dropna()
    if len(s) <= 3:
        return 0.0
    return float(min(max(float(np.percentile(s, 40)), 1.0), float(np.percentile(s, 85))))


def infer_max_distance(timeframe_zones) -> float:
    """Visible/keep window in ATR: (zone-center span / median ATR) * 0.6, clipped [4, 20];
    fallback 10. Levels beyond this are pruned so the default view stays uncluttered."""
    if timeframe_zones is None or timeframe_zones.empty:
        return 10.0
    catr = timeframe_zones["current_atr"].replace(0, np.nan).median()
    if not np.isfinite(catr) or catr <= 0:
        return 10.0
    span = float(timeframe_zones["zone_center"].max() - timeframe_zones["zone_center"].min())
    val = (span / float(catr)) * 0.6
    if not np.isfinite(val):
        return 10.0
    return round(float(min(max(val, 4.0), 20.0)), 1)


def infer_config(df_master, cfg):
    """Resolve auto parameters from the data, skipping any key in ``cfg.overrides``.
    Mutates and returns ``cfg`` plus a ``report`` {key: inferred_value}. Runs ONE
    permissive probe pass to read the score distribution; the caller then runs the
    real pipeline with the resolved config."""
    from dataclasses import replace

    rep, ov = {}, (cfg.overrides or set())
    if "tick_size" not in ov:
        cfg.tick_size = infer_tick_size(df_master); rep["tick_size"] = cfg.tick_size
    if "lookback" not in ov:
        cfg.lookback = infer_lookback(df_master); rep["lookback"] = cfg.lookback
    if "atr_multiplier" not in ov:
        cfg.atr_multiplier = infer_zone_width(df_master, cfg.tick_size); rep["atr_multiplier"] = cfg.atr_multiplier

    if {"min_score", "max_distance_atr"} - ov:   # only probe if something still needs the score distribution
        probe = replace(cfg, min_score=-1e9, max_distance_atr=1e9, auto=False)
        _, _, tfz, _, _ = run_sr(df_master, probe.timeframes, probe.swing_windows, probe.timeframe_weights,
                                 probe.atr_period, probe.atr_multiplier, probe.cluster_atr_multiplier,
                                 probe.min_score, probe.max_distance_atr, probe.min_zone_width,
                                 probe.min_bars, probe.tick_size, probe.use_close_for_swings)
        if tfz is not None and not tfz.empty:
            if "max_distance_atr" not in ov:
                cfg.max_distance_atr = infer_max_distance(tfz); rep["max_distance_atr"] = cfg.max_distance_atr
            if "min_score" not in ov:
                merged = score_and_merge(tfz, cfg.timeframe_weights, -1e9, 1e9,
                                         cfg.cluster_atr_multiplier, cfg.tick_size)
                cfg.min_score = infer_min_score(merged); rep["min_score"] = cfg.min_score
    return cfg, rep


def run_sr(df_master, timeframes, swing_windows, timeframe_weights, atr_period, atr_multiplier,
           cluster_atr_multiplier, min_score, max_distance_atr, min_zone_width, min_bars=30,
           tick_size=0.0, use_close_for_swings=False):
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
            df_tf = detect_swings(df_tf, swing_windows.get(tf, 5), use_close_for_swings)
            levels = extract_levels(df_tf, inst, tf, use_close_for_swings)
            n_zones = 0
            if not levels.empty:
                zones = cluster_levels(levels, atr_multiplier, cluster_atr_multiplier, min_zone_width)
                all_levels.append(levels)
                if not zones.empty:
                    all_tf_zones.append(zones)
                    n_zones = len(zones)
            tf_data[inst][tf] = df_tf
            last_atr = float(df_tf["atr"].iloc[-1]) if "atr" in df_tf.columns and pd.notna(df_tf["atr"].iloc[-1]) else None
            diagnostics.append({"instrument": inst, "timeframe": tf, "status": "used", "bars": len(df_tf),
                                "native": str(native), "atr": round(last_atr, 6) if last_atr is not None else None,
                                "reason": f"{n_zones} raw zones"})
    raw_levels = pd.concat(all_levels, ignore_index=True) if all_levels else pd.DataFrame()
    timeframe_zones = pd.concat(all_tf_zones, ignore_index=True) if all_tf_zones else pd.DataFrame()
    final_zones = score_and_merge(timeframe_zones, timeframe_weights, min_score, max_distance_atr, cluster_atr_multiplier, tick_size)
    final_zones = _finalize_zones(final_zones, tf_data)   # volume-at-level + High/Med/Low confidence
    return final_zones, raw_levels, timeframe_zones, tf_data, pd.DataFrame(diagnostics)


def compute_sr(df_master: pd.DataFrame, config: SRConfig):
    """Convenience wrapper that runs ``run_sr`` from an :class:`SRConfig`."""
    return run_sr(
        df_master, config.timeframes, config.swing_windows, config.timeframe_weights,
        config.atr_period, config.atr_multiplier, config.cluster_atr_multiplier,
        config.min_score, config.max_distance_atr, config.min_zone_width, config.min_bars,
        config.tick_size, config.use_close_for_swings,
    )
