"""Charting tests — figures must build for every shape of input and stay JSON-safe."""
import json

import pandas as pd

from conftest import normalized
from sr import charting, engine


def _result(periods=1000, tfs=("1h", "4h", "1D")):
    df = normalized("1h", periods)
    fz, _, _, tfd, _ = engine.compute_sr(df, engine.SRConfig(timeframes=list(tfs)))
    return fz, tfd.get("T", {})


def test_figure_builds_and_serializes():
    fz, tfd = _result()
    fig = charting.build_sr_figure(tfd, fz, "T", 300)
    assert len(fig.data) > 0
    s = charting.figure_to_json(fig)
    json.loads(s)                       # valid JSON
    json.dumps(json.loads(s), allow_nan=False)  # strict (no NaN/Inf)


def test_figure_empty_tf_data():
    fig = charting.build_sr_figure({}, pd.DataFrame(), "T", 300)
    assert fig is not None and len(fig.data) == 0  # placeholder, no crash


def test_figure_no_zones():
    _, tfd = _result()
    fig = charting.build_sr_figure(tfd, pd.DataFrame(), "T", 300)
    assert any(t.type == "candlestick" for t in fig.data)


def test_figure_single_timeframe():
    fz, tfd = _result(tfs=("1D",))
    fig = charting.build_sr_figure(tfd, fz, "T", 300)
    assert sum(t.type == "candlestick" for t in fig.data) == 1


def test_figure_constant_prices():
    df = normalized("1h", 400)
    for c in ["open", "high", "low", "close"]:
        df[c] = 50.0
    fz, _, _, tfd, _ = engine.compute_sr(df, engine.SRConfig(timeframes=["1h", "4h"]))
    fig = charting.build_sr_figure(tfd.get("T", {}), fz, "T", 300)
    assert len(fig.data) > 0  # no crash on zero range


def test_summarize_empty():
    s = charting.summarize_zones(pd.DataFrame())
    assert s["current_price"] is None and s["n_support"] == 0 and s["n_resistance"] == 0


def test_summarize_nearest_sides():
    fz, _ = _result()
    s = charting.summarize_zones(fz)
    assert s["current_price"] is not None
    cp = s["current_price"]
    if s["nearest_support"]:
        # nearest support should be at or below current price when one exists below
        below = fz[(fz.side == "support") & (fz.zone_center <= cp)]
        if not below.empty:
            assert s["nearest_support"]["center"] <= cp + 1e-9
    if s["nearest_resistance"]:
        above = fz[(fz.side == "resistance") & (fz.zone_center >= cp)]
        if not above.empty:
            assert s["nearest_resistance"]["center"] >= cp - 1e-9


def test_zones_to_records_empty():
    assert charting.zones_to_records(pd.DataFrame()) == []


def test_zones_to_records_serializable():
    fz, _ = _result()
    recs = charting.zones_to_records(fz)
    assert isinstance(recs, list) and recs
    assert set(["side", "zone_center", "score", "timeframes"]).issubset(recs[0])
