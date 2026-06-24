"""Engine correctness + edge-case tests."""
import numpy as np
import pandas as pd
import pytest

from conftest import descending, normalized
from sr import engine


# --- normalization ----------------------------------------------------------
def test_normalize_iso_utc_no_drop():
    raw = pd.DataFrame({
        "Date": ["2026-02-18T13:30:00.000Z", "2026-02-18T13:45:00.000Z"],
        "Open": [1.0, 1.1], "High": [1.2, 1.2], "Low": [0.9, 1.0],
        "Volume": [10, 20], "Close": [1.1, 1.05],
    })
    out = engine.normalize_ohlcv(raw, "QH")
    assert len(out) == 2
    assert str(out["datetime"].iloc[0]) == "2026-02-18 13:30:00"


def test_normalize_lowercase_variant():
    raw = pd.DataFrame({"datetime": ["2026-01-01 09:00"], "open": [1], "high": [2], "low": [0.5], "close": [1.5]})
    out = engine.normalize_ohlcv(raw, "X")
    assert out["volume"].iloc[0] == 0  # missing volume defaults to 0


def test_normalize_missing_ohlc_raises():
    raw = pd.DataFrame({"Date": ["2026-01-01"], "Open": [1], "Close": [2]})  # no High/Low
    with pytest.raises(ValueError, match="Missing OHLC"):
        engine.normalize_ohlcv(raw, "X")


def test_normalize_missing_date_raises():
    raw = pd.DataFrame({"Open": [1], "High": [2], "Low": [0], "Close": [1]})
    with pytest.raises(ValueError, match="date"):
        engine.normalize_ohlcv(raw, "X")


def test_normalize_drops_bad_dates():
    raw = pd.DataFrame({
        "Date": ["2026-01-01T00:00:00Z", "not-a-date", "2026-01-01T00:15:00Z"],
        "Open": [1, 1, 1], "High": [2, 2, 2], "Low": [0, 0, 0], "Close": [1, 1, 1],
    })
    out = engine.normalize_ohlcv(raw, "X")
    assert len(out) == 2  # the bad row is dropped


def test_normalize_dedups_within_file_keep_last():
    raw = pd.DataFrame({
        "Date": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        "Open": [1, 9], "High": [2, 10], "Low": [0, 6], "Close": [1, 7],  # both valid OHLC
    })
    out = engine.normalize_ohlcv(raw, "X")
    assert len(out) == 1 and out["close"].iloc[0] == 7  # last wins


def test_normalize_preserves_negative_prices():
    raw = pd.DataFrame({"Date": ["2026-01-01T00:00:00Z"], "Open": [-0.03], "High": [-0.01],
                        "Low": [-0.05], "Close": [-0.02]})
    out = engine.normalize_ohlcv(raw, "QH")
    assert out["close"].iloc[0] == -0.02


# --- instrument naming ------------------------------------------------------
@pytest.mark.parametrize("fname,expected", [
    ("QH Charts22.6.26.xlsx", "QH"),
    ("QH Oct26 Charts.xlsx", "QH Oct26"),
    ("QH_15min_20260623.xlsx", "QH"),
])
def test_clean_instrument_name(fname, expected):
    assert engine.clean_instrument_name(fname) == expected


# --- timeframe adaptation ---------------------------------------------------
def test_hourly_drops_15min():
    eff, skipped = engine.select_effective_timeframes(["15min", "1h", "4h", "1D"], pd.Timedelta(hours=1))
    assert eff == ["1h", "4h", "1D"] and "15min" in skipped


def test_4h_native_drops_intraday():
    eff, skipped = engine.select_effective_timeframes(["15min", "1h", "4h", "1D"], pd.Timedelta(hours=4))
    assert eff == ["4h", "1D"] and set(skipped) == {"15min", "1h"}


def test_15min_keeps_all():
    eff, skipped = engine.select_effective_timeframes(["15min", "1h", "4h", "1D"], pd.Timedelta(minutes=15))
    assert eff == ["15min", "1h", "4h", "1D"] and not skipped


def test_native_none_keeps_all():
    eff, skipped = engine.select_effective_timeframes(["15min", "1h"], None)
    assert eff == ["15min", "1h"] and not skipped


def test_only_finer_than_native_fallback_to_coarsest():
    # all requested are finer than a daily native -> keep the coarsest requested
    eff, skipped = engine.select_effective_timeframes(["15min", "1h"], pd.Timedelta(days=1))
    assert eff == ["1h"] and "1h" not in skipped


@pytest.mark.parametrize("tf,td", [("15min", pd.Timedelta(minutes=15)), ("1h", pd.Timedelta(hours=1)),
                                   ("4h", pd.Timedelta(hours=4)), ("1D", pd.Timedelta(days=1)),
                                   ("1W", pd.Timedelta(days=7))])
def test_tf_to_timedelta(tf, td):
    assert engine.tf_to_timedelta(tf) == td


def test_native_interval_detection():
    assert engine.infer_native_interval(normalized("1h", 200)) == pd.Timedelta(hours=1)


def test_native_interval_single_row_is_none():
    assert engine.infer_native_interval(normalized("1h", 1)) is None


# --- ATR / swings -----------------------------------------------------------
def test_atr_no_nan():
    d = engine.add_atr(normalized("1h", 100), 14)
    assert d["atr"].notna().all() and (d["atr"] >= 0).all()


def test_swings_short_series_no_swings():
    d = engine.detect_swings(normalized("1h", 5), window=8)
    assert not d["swing_high"].any() and not d["swing_low"].any()


def test_swings_detect_known_peak():
    df = pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=7, freq="1h"),
        "high": [1, 2, 3, 9, 3, 2, 1], "low": [1, 2, 3, 9, 3, 2, 1],
        "open": 1.0, "close": 1.0,
    })
    d = engine.detect_swings(df, window=3)
    assert bool(d.loc[3, "swing_high"])


# --- clustering / scoring ---------------------------------------------------
def test_cluster_empty():
    assert engine.cluster_levels(pd.DataFrame(), 0.25, 0.35, 0.005).empty


def test_score_empty():
    assert engine.score_and_merge(pd.DataFrame(), engine.DEFAULT_TIMEFRAME_WEIGHTS, 3, 10, 0.35).empty


def test_overlapping_same_side_zones_merge():
    """Same-side zones whose [low, high] ranges overlap must collapse into ONE, even
    when a wide zone's center-order differs from its low-edge order. This guards the
    merge sort invariant: A[9.9,10.1] does not overlap B[10.4,10.6], but wide
    C[9.8,11.0] spans both. Visiting in center order (A,B,C) wrongly closes A before
    C arrives, leaving A overlapping the B+C zone; visiting in low-edge order merges
    all three. (Regression for the stacked-bands bug.)"""
    cp, ts = 5.0, pd.Timestamp("2026-01-01")
    rows = [(10.0, 9.9, 10.1), (10.5, 10.4, 10.6), (10.6, 9.8, 11.0)]
    tfz = pd.DataFrame([dict(
        instrument="X", timeframe="1h", side="resistance", zone_center=c, zone_low=lo,
        zone_high=hi, touches=2, first_touch=ts, last_touch=ts, atr=1.0,
        current_price=cp, current_atr=1.0) for c, lo, hi in rows])
    # merge_width = 0.35 * 1.0 = 0.35 < the 0.5 center gap A->B, so ONLY overlap merges them
    out = engine.score_and_merge(tfz, engine.DEFAULT_TIMEFRAME_WEIGHTS, 0.0, 1e9, 0.35)
    res = out[out.side == "resistance"]
    assert len(res) == 1, "overlapping zones must merge regardless of center vs low ordering"
    assert res.iloc[0]["zone_low"] == 9.8 and res.iloc[0]["zone_high"] == 11.0
    assert res.iloc[0]["touches"] == 6


# --- run_sr edge cases ------------------------------------------------------
def test_run_sr_empty_master():
    empty = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "instrument"])
    fz, rl, tfz, tfd, diag = engine.run_sr(
        empty, ["1h"], engine.DEFAULT_SWING_WINDOWS, engine.DEFAULT_TIMEFRAME_WEIGHTS,
        14, 0.25, 0.35, 3, 10, 0.005)
    assert fz.empty and rl.empty and tfz.empty


def test_run_sr_constant_prices_no_crash():
    df = normalized("1h", 400)
    for c in ["open", "high", "low", "close"]:
        df[c] = 50.0  # zero range -> ATR 0; must not divide-by-zero
    fz, *_ = engine.run_sr(df, ["1h", "4h"], engine.DEFAULT_SWING_WINDOWS,
                           engine.DEFAULT_TIMEFRAME_WEIGHTS, 14, 0.25, 0.35, 3, 10, 0.005)
    assert isinstance(fz, pd.DataFrame)  # ran to completion


def test_run_sr_too_short_skips_with_reason():
    df = normalized("1h", 20)  # < 30 bars
    fz, _, _, _, diag = engine.run_sr(df, ["1h"], engine.DEFAULT_SWING_WINDOWS,
                                      engine.DEFAULT_TIMEFRAME_WEIGHTS, 14, 0.25, 0.35, 3, 10, 0.005)
    assert fz.empty
    assert (diag["status"] == "skipped").all() and "bars" in diag.iloc[0]["reason"]


def test_run_sr_no_double_count_on_hourly():
    df = normalized("1h", 800)
    fz, _, _, _, diag = engine.run_sr(df, ["15min", "1h", "4h", "1D"], engine.DEFAULT_SWING_WINDOWS,
                                      engine.DEFAULT_TIMEFRAME_WEIGHTS, 14, 0.25, 0.35, 3, 10, 0.005)
    assert (diag.query("timeframe == '15min'")["status"] == "skipped").all()
    if not fz.empty:
        assert not fz["timeframes"].str.contains("15min").any()


def test_run_sr_respects_min_score_and_distance():
    df = normalized("1h", 1000)
    fz, *_ = engine.run_sr(df, ["1h", "4h", "1D"], engine.DEFAULT_SWING_WINDOWS,
                           engine.DEFAULT_TIMEFRAME_WEIGHTS, 14, 0.25, 0.35,
                           min_score=5.0, max_distance_atr=8.0, min_zone_width=0.005)
    if not fz.empty:
        assert (fz["score"] >= 5.0).all()
        assert (fz["distance_atr"] <= 8.0).all()


def test_compute_sr_matches_run_sr():
    df = normalized("1h", 500)
    cfg = engine.SRConfig(timeframes=["1h", "4h", "1D"])
    a = engine.compute_sr(df, cfg)[0]
    b = engine.run_sr(df, cfg.timeframes, cfg.swing_windows, cfg.timeframe_weights, cfg.atr_period,
                      cfg.atr_multiplier, cfg.cluster_atr_multiplier, cfg.min_score,
                      cfg.max_distance_atr, cfg.min_zone_width, cfg.min_bars,
                      cfg.tick_size, cfg.use_close_for_swings)[0]
    pd.testing.assert_frame_equal(a, b)


def test_sides_are_valid():
    df = normalized("1h", 1000)
    fz = engine.compute_sr(df, engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    if not fz.empty:
        assert set(fz["side"].unique()) <= {"support", "resistance"}
        assert (fz["zone_low"] <= fz["zone_high"]).all()


# --- side classification must be PRICE-RELATIVE (regression for the swing-type bug) ---
def test_side_is_price_relative_on_descending_series():
    fz = engine.compute_sr(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    assert not fz.empty
    cp = float(fz["current_price"].iloc[0])
    # every support is at/below price, every resistance at/above price
    assert (fz.loc[fz.side == "support", "zone_center"] <= cp).all()
    assert (fz.loc[fz.side == "resistance", "zone_center"] >= cp).all()


def test_every_zone_above_price_is_resistance():
    fz = engine.compute_sr(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    cp = float(fz["current_price"].iloc[0])
    above, below = fz[fz.zone_center > cp], fz[fz.zone_center < cp]
    if not above.empty:
        assert (above["side"] == "resistance").all()
    if not below.empty:
        assert (below["side"] == "support").all()


def test_score_and_merge_overrides_swing_side_with_price_relative():
    """Direct unit test: even when the input swing label contradicts price, the
    output side is determined by zone_center vs current_price."""
    cp, ts = 5.0, pd.Timestamp("2026-01-01")
    rows = [  # (center, deliberately-wrong swing side)
        (4.0, "support"), (4.5, "resistance"), (6.0, "support"), (7.0, "resistance")]
    tfz = pd.DataFrame([dict(
        instrument="X", timeframe="1h", side=s, zone_center=c, zone_low=c - 0.05,
        zone_high=c + 0.05, touches=3, first_touch=ts, last_touch=ts, atr=0.2,
        current_price=cp, current_atr=0.2) for c, s in rows])
    out = engine.score_and_merge(tfz, engine.DEFAULT_TIMEFRAME_WEIGHTS,
                                 min_score=0.0, max_distance_atr=1e9, cluster_atr_multiplier=0.05)
    for _, z in out.iterrows():
        if z.zone_center < cp:
            assert z.side == "support"
        if z.zone_center > cp:
            assert z.side == "resistance"


# --- OHLC sanity at ingest (#2) ---------------------------------------------
def test_normalize_drops_invalid_ohlc():
    raw = pd.DataFrame({
        "Date": ["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z",
                 "2026-01-01T00:30:00Z", "2026-01-01T00:45:00Z"],
        "Open": [10, 10, 10, 10], "High": [8, 12, 9, 11],   # row0 H<L, row2 H<L
        "Low": [9, 8, 10, 10], "Close": [10, 11, 10.5, 10],
    })
    out = engine.normalize_ohlcv(raw, "X")
    assert len(out) == 2  # only the two logically-valid bars survive
    ok = ((out.high >= out.low) & (out.high >= out[["open", "close"]].max(axis=1))
          & (out.low <= out[["open", "close"]].min(axis=1)))
    assert ok.all()


def test_normalize_keeps_negative_and_flat_bars():
    raw = pd.DataFrame({
        "Date": ["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"],
        "Open": [-0.03, -0.02], "High": [-0.01, -0.02], "Low": [-0.05, -0.02], "Close": [-0.02, -0.02],
    })
    assert len(engine.normalize_ohlcv(raw, "QH")) == 2  # negative spread + zero-range bar are valid


def test_normalize_ohlcv_split_separates_invalid():
    raw = pd.DataFrame({
        "Date": ["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z"],
        "Open": [10, 10, 10], "High": [12, 8, 11], "Low": [8, 9, 10], "Close": [11, 10, 10.5],
    })  # middle row: High 8 < Low 9 -> invalid
    valid, invalid = engine.normalize_ohlcv_split(raw, "T")
    assert len(valid) == 2 and len(invalid) == 1
    assert engine.ohlc_invalid_mask(invalid).all()
    assert not engine.ohlc_invalid_mask(valid).any()
    assert "High < Low" in engine.ohlc_invalid_reason(invalid.iloc[0])


# --- determinism (#5) -------------------------------------------------------
def test_engine_is_deterministic():
    m = descending()
    a = engine.compute_sr(m, engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    b = engine.compute_sr(m, engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    pd.testing.assert_frame_equal(a, b)


# --- tick size + close-based swings -----------------------------------------
def test_snap_to_tick_modes():
    assert engine.snap_to_tick(0.1234, 0.01) == 0.12
    assert engine.snap_to_tick(0.1234, 0.01, "ceil") == 0.13
    assert engine.snap_to_tick(0.1299, 0.01, "floor") == 0.12
    assert engine.snap_to_tick(5.0, 0.0) == 5.0  # tick off -> unchanged


def test_zones_snap_to_tick_grid():
    fz = engine.compute_sr(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"], tick_size=0.01))[0]
    assert not fz.empty
    for col in ["zone_center", "zone_low", "zone_high"]:
        off = (np.round(fz[col] / 0.01) - fz[col] / 0.01).abs().max()
        assert off < 1e-6, f"{col} off the 0.01 grid"
    assert (fz["zone_high"] - fz["zone_low"] >= 0.02 - 1e-9).all()  # >= one tick each side


def test_use_close_for_swings_changes_levels():
    df = descending()
    a = engine.compute_sr(df, engine.SRConfig(timeframes=["1h", "4h", "1D"], use_close_for_swings=False))[0]
    b = engine.compute_sr(df, engine.SRConfig(timeframes=["1h", "4h", "1D"], use_close_for_swings=True))[0]
    assert not a.empty and not b.empty
    assert not a[["zone_center", "score"]].reset_index(drop=True).equals(
        b[["zone_center", "score"]].reset_index(drop=True))


def test_tick_snapping_keeps_side_price_relative_off_grid():
    # current price is NOT on the 0.01 grid here; snapping must not flip sides
    fz = engine.compute_sr(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"], tick_size=0.01))[0]
    cp = float(fz["current_price"].iloc[0])
    assert (fz.loc[fz.side == "support", "zone_center"] <= cp).all()
    assert (fz.loc[fz.side == "resistance", "zone_center"] >= cp).all()


# --- zero-config inference --------------------------------------------------
def test_srconfig_defaults_auto_off():
    cfg = engine.SRConfig()
    assert cfg.auto is False and cfg.overrides == set()  # tests/engine unchanged unless auto is set


def test_infer_tick_size_decimals():
    two = pd.DataFrame({"open": [71.50], "high": [71.55], "low": [71.45], "close": [71.52]})
    assert engine.infer_tick_size(two) == 0.01
    ints = pd.DataFrame({"open": [10], "high": [12], "low": [8], "close": [11]})
    assert engine.infer_tick_size(ints) == 1.0
    one = pd.DataFrame({"open": [5.0], "high": [5.0], "low": [5.0], "close": [5.0]})
    assert engine.infer_tick_size(one) == 0.0  # <2 distinct prices -> snapping off


def test_infer_tick_size_negative_spread_positive():
    df = pd.DataFrame({"open": [-0.05, -0.03], "high": [-0.01, -0.02],
                       "low": [-0.08, -0.05], "close": [-0.04, -0.03]})
    t = engine.infer_tick_size(df)
    assert t > 0 and t <= 0.01


def test_infer_min_score_sparse_is_zero():
    assert engine.infer_min_score(pd.DataFrame({"score": [5.0, 6.0, 7.0]})) == 0.0
    assert engine.infer_min_score(pd.DataFrame()) == 0.0


def test_infer_max_distance_clamped():
    tfz = pd.DataFrame([dict(zone_center=c, current_atr=0.2) for c in (4.0, 4.5, 5.0, 5.5, 6.0)])
    assert 4.0 <= engine.infer_max_distance(tfz) <= 20.0
    assert engine.infer_max_distance(pd.DataFrame()) == 10.0
    zero_atr = pd.DataFrame([dict(zone_center=4.0, current_atr=0.0), dict(zone_center=5.0, current_atr=0.0)])
    assert engine.infer_max_distance(zero_atr) == 10.0


def test_infer_config_reports_and_respects_overrides():
    cfg, rep = engine.infer_config(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"], auto=True))
    assert "tick_size" in rep and "lookback" in rep
    assert cfg.lookback >= 50 and cfg.tick_size >= 0
    cfg2, rep2 = engine.infer_config(
        descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"], auto=True,
                                      overrides={"tick_size"}, tick_size=0.5))
    assert "tick_size" not in rep2 and cfg2.tick_size == 0.5  # pinned key is not overwritten


# --- enrichment: confidence, recency, bucket, volume, wide-zone split -------
def test_final_zones_have_enrichment_columns():
    fz = engine.compute_sr(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    assert not fz.empty
    for col in ("bucket", "confidence", "recency_weight", "confidence_score",
                "volume_at_level", "days_since_touch"):
        assert col in fz.columns
    assert set(fz["bucket"].unique()) <= {"active", "nearby", "historical"}
    assert set(fz["confidence"].unique()) <= {"High", "Medium", "Low"}
    assert fz["recency_weight"].between(0, 1).all()
    assert fz["confidence_score"].between(0, 1).all()
    assert (fz["volume_at_level"] >= 0).all()


def test_volume_absent_is_zero_not_null():
    df = descending()
    df["volume"] = 0
    fz = engine.compute_sr(df, engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    if not fz.empty:
        assert (fz["volume_at_level"] == 0.0).all() and fz["volume_at_level"].notna().all()


def test_wide_zone_splits_and_conserves_touches():
    cp, ts = 5.0, pd.Timestamp("2026-01-01")
    # 9 overlapping resistance levels spanning ~4.6 ATR (atr=1.0) -> one cluster, must split
    centers = [20.0, 20.5, 21.0, 21.5, 22.0, 22.5, 23.0, 23.5, 24.0]
    tfz = pd.DataFrame([dict(
        instrument="X", timeframe="1h", side="resistance", zone_center=c, zone_low=c - 0.3,
        zone_high=c + 0.3, touches=2, first_touch=ts, last_touch=ts, atr=1.0,
        current_price=cp, current_atr=1.0) for c in centers])
    out = engine.score_and_merge(tfz, engine.DEFAULT_TIMEFRAME_WEIGHTS, 0.0, 1e9, 0.35)
    res = out[out.side == "resistance"]
    assert len(res) >= 2                              # the wide span was split
    assert (res["zone_high"] > res["zone_low"]).all()  # no inverted sub-zones
    assert int(res["touches"].sum()) == 18             # touches conserved (9 levels x 2)


def test_bucket_active_and_historical():
    cp = 5.0
    recent, old = pd.Timestamp("2026-06-24"), pd.Timestamp("2026-01-01")  # ~175 days apart
    tfz = pd.DataFrame([
        dict(instrument="X", timeframe="1D", side="resistance", zone_center=5.3, zone_low=5.2,
             zone_high=5.4, touches=3, first_touch=recent, last_touch=recent, atr=0.5,
             current_price=cp, current_atr=0.5),
        dict(instrument="X", timeframe="1D", side="support", zone_center=4.0, zone_low=3.9,
             zone_high=4.1, touches=2, first_touch=old, last_touch=old, atr=0.5,
             current_price=cp, current_atr=0.5),
    ])
    out = engine.score_and_merge(tfz, engine.DEFAULT_TIMEFRAME_WEIGHTS, 0.0, 1e9, 0.35)
    by_side = {r["side"]: r for _, r in out.iterrows()}
    assert by_side["resistance"]["bucket"] == "active"      # near (0.4 ATR) + freshest touch
    assert by_side["support"]["bucket"] == "historical"     # ~175 days stale
