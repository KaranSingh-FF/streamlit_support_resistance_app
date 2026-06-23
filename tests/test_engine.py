"""Tests for the S/R engine + storage.

Runs under pytest, or standalone:  python tests/test_engine.py
Uses synthetic data only (no real market files).
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sr import engine, storage  # noqa: E402


def _synthetic(interval: str, periods: int, seed: int = 1) -> pd.DataFrame:
    """A normalized OHLC frame (datetime, open/high/low/close, volume, instrument)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01 00:00:00", periods=periods, freq=interval)
    close = 100 + np.cumsum(rng.normal(0, 0.2, periods))
    openp = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(openp, close) + np.abs(rng.normal(0, 0.1, periods))
    low = np.minimum(openp, close) - np.abs(rng.normal(0, 0.1, periods))
    return pd.DataFrame({
        "datetime": idx, "open": openp, "high": high, "low": low, "close": close,
        "volume": rng.integers(10, 100, periods), "instrument": "T",
    })


# --- normalization / parsing ------------------------------------------------
def test_normalize_iso_utc_no_drop():
    raw = pd.DataFrame({
        "Date": ["2026-02-18T13:30:00.000Z", "2026-02-18T13:45:00.000Z"],
        "Open": [1.0, 1.1], "High": [1.2, 1.2], "Low": [0.9, 1.0],
        "Volume": [10, 20], "Close": [1.1, 1.05],
    })
    out = engine.normalize_ohlcv(raw, "QH")
    assert len(out) == 2
    assert str(out["datetime"].iloc[0]) == "2026-02-18 13:30:00"


def test_clean_instrument_name():
    assert engine.clean_instrument_name("QH Charts22.6.26.xlsx") == "QH"
    assert engine.clean_instrument_name("QH Oct26 Charts.xlsx") == "QH Oct26"


# --- adaptive timeframes ----------------------------------------------------
def test_hourly_drops_15min():
    native = pd.Timedelta(hours=1)
    eff, skipped = engine.select_effective_timeframes(["15min", "1h", "4h", "1D"], native)
    assert "15min" not in eff and "15min" in skipped
    assert eff == ["1h", "4h", "1D"]


def test_15min_keeps_all():
    native = pd.Timedelta(minutes=15)
    eff, skipped = engine.select_effective_timeframes(["15min", "1h", "4h", "1D"], native)
    assert eff == ["15min", "1h", "4h", "1D"] and not skipped


def test_tf_to_timedelta():
    assert engine.tf_to_timedelta("15min") == pd.Timedelta(minutes=15)
    assert engine.tf_to_timedelta("1D") == pd.Timedelta(days=1)
    assert engine.tf_to_timedelta("1W") == pd.Timedelta(days=7)


def test_native_interval_detection():
    df = _synthetic("1h", 200)
    assert engine.infer_native_interval(df) == pd.Timedelta(hours=1)


def test_run_sr_no_double_count_on_hourly():
    df = _synthetic("1h", 800)
    fz, _, _, _, diag = engine.run_sr(
        df, ["15min", "1h", "4h", "1D"], engine.DEFAULT_SWING_WINDOWS, engine.DEFAULT_TIMEFRAME_WEIGHTS,
        14, 0.25, 0.35, 3.0, 10.0, 0.005,
    )
    # 15min must be skipped, never credited to a zone
    assert (diag.query("timeframe == '15min'")["status"] == "skipped").all()
    if not fz.empty:
        assert not fz["timeframes"].str.contains("15min").any()


# --- dedup / master-update --------------------------------------------------
def _with_tmp_store(fn):
    with tempfile.TemporaryDirectory() as d:
        storage.set_base_dir(d)
        try:
            fn()
        finally:
            storage.set_base_dir(Path("sr_data_store"))


def test_dedup_overwrite_and_append():
    def body():
        full = _synthetic("1h", 300)
        _, s1 = storage.merge_into_master(full, "T")
        assert s1["master_rows_after"] == 300 and s1["net_new_rows"] == 300

        # re-upload last day with a changed close -> overwrite, no duplication
        last_date = full["datetime"].max().date()
        last_day = full[full["datetime"].dt.date == last_date].copy()
        last_day["close"] = last_day["close"] + 99.0
        m2, s2 = storage.merge_into_master(last_day, "T")
        assert s2["net_new_rows"] == 0
        assert m2.duplicated(["instrument", "datetime"]).sum() == 0
        same_day = m2[m2["datetime"].dt.date == last_date]
        assert len(same_day) == len(last_day)
        assert abs(float(same_day["close"].iloc[-1]) - float(last_day["close"].iloc[-1])) < 1e-9

        # genuinely new day appends
        nxt = last_day.copy()
        nxt["datetime"] = nxt["datetime"] + pd.Timedelta(days=1)
        nxt["close"] = nxt["close"] - 99.0
        _, s3 = storage.merge_into_master(nxt, "T")
        assert s3["net_new_rows"] == len(nxt)

    _with_tmp_store(body)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed.")
