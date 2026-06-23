"""Shared pytest fixtures and helpers."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sr import storage  # noqa: E402


@pytest.fixture
def tmp_store(tmp_path):
    """Point storage at an isolated temp dir; restore the default afterward."""
    storage.set_base_dir(tmp_path)
    yield tmp_path
    storage.set_base_dir(Path("sr_data_store"))


def normalized(interval: str, periods: int, seed: int = 1, instrument: str = "T",
               start: str = "2026-01-01 00:00:00") -> pd.DataFrame:
    """A clean normalized OHLC frame (engine output schema)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=periods, freq=interval)
    close = 100 + np.cumsum(rng.normal(0, 0.2, periods))
    openp = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(openp, close) + np.abs(rng.normal(0, 0.1, periods))
    low = np.minimum(openp, close) - np.abs(rng.normal(0, 0.1, periods))
    return pd.DataFrame({
        "datetime": idx, "open": openp, "high": high, "low": low, "close": close,
        "volume": rng.integers(10, 100, periods), "instrument": instrument,
    })


def descending(n: int = 1300, instrument: str = "SPRD") -> pd.DataFrame:
    """A normalized descending zigzag: old swing lows end up ABOVE the final price,
    which is what exposes price-relative vs swing-type side classification."""
    t = np.arange(n)
    close = 8.0 - 3.0 * t / n + 0.6 * np.sin(t / 3.0) + 0.25 * np.sin(t / 11.0)
    openp = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(openp, close) + 0.05
    low = np.minimum(openp, close) - 0.05
    return pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": openp, "high": high, "low": low, "close": close,
        "volume": 100, "instrument": instrument,
    })


def qh_excel_frame(periods: int, seed: int = 3) -> pd.DataFrame:
    """A raw QH-format frame (pre-normalization), ISO-UTC dates."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-05 13:30:00", periods=periods, freq="15min")
    close = 100 + np.cumsum(rng.normal(0, 0.15, periods))
    openp = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.1, periods)) + 0.03
    return pd.DataFrame({
        "Date": idx.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "Open": openp.round(2), "High": (np.maximum(openp, close) + spread).round(2),
        "Low": (np.minimum(openp, close) - spread).round(2),
        "Volume": rng.integers(50, 1500, periods), "Close": close.round(2),
        "BuyVolume": rng.integers(0, 500, periods), "SellVolume": rng.integers(0, 500, periods),
        "isNewCandle": 1.0,
    })
