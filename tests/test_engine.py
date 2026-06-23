"""Engine correctness + edge-case tests."""
import numpy as np
import pandas as pd
import pytest

from conftest import normalized
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
        "Open": [1, 9], "High": [2, 9], "Low": [0, 9], "Close": [1, 7],
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
                      cfg.max_distance_atr, cfg.min_zone_width, cfg.min_bars)[0]
    pd.testing.assert_frame_equal(a, b)


def test_sides_are_valid():
    df = normalized("1h", 1000)
    fz = engine.compute_sr(df, engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    if not fz.empty:
        assert set(fz["side"].unique()) <= {"support", "resistance"}
        assert (fz["zone_low"] <= fz["zone_high"]).all()
