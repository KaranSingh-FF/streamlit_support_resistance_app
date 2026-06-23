"""Charting tests — figures must build for every shape of input and stay JSON-safe."""
import json

import pandas as pd

from conftest import descending, normalized
from sr import charting, engine


def _result(periods=1000, tfs=("1h", "4h", "1D")):
    df = normalized("1h", periods)
    fz, _, _, tfd, _ = engine.compute_sr(df, engine.SRConfig(timeframes=list(tfs)))
    return fz, tfd.get("T", {})


def test_figure_builds_and_serializes():
    fz, tfd = _result()
    fig = charting.build_sr_figure(tfd, fz, "T", 300)
    assert len(fig.data) > 0
    s = fig.to_json()
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


def test_summary_nearest_uses_edges_and_excludes_current_zone():
    # Nearest cards measure to the zone EDGE and skip any zone the price sits inside
    # (that one is reported separately as current_zone, not as "0.00 ATR away").
    fz = engine.compute_sr(descending(), engine.SRConfig(timeframes=["1h", "4h", "1D"]))[0]
    s = charting.summarize_zones(fz)
    cp = s["current_price"]
    below = fz[fz.zone_high < cp]   # fully below the price
    above = fz[fz.zone_low > cp]    # fully above the price
    if s["nearest_support"]:
        assert s["nearest_support"]["high"] < cp + 1e-9
        assert abs(s["nearest_support"]["high"] - float(below["zone_high"].max())) < 1e-9
    if s["nearest_resistance"]:
        assert s["nearest_resistance"]["low"] > cp - 1e-9
        assert abs(s["nearest_resistance"]["low"] - float(above["zone_low"].min())) < 1e-9
    if s["current_zone"]:  # if the price is inside a zone, it is flagged, not "nearest"
        assert s["current_zone"]["low"] <= cp <= s["current_zone"]["high"]


def test_summary_current_zone_excluded_from_nearest():
    # Price sits inside a support zone; that zone must NOT be the nearest support.
    cp, ts = 5.0, pd.Timestamp("2026-01-01")
    rows = [(4.98, 4.90, 5.10), (4.40, 4.30, 4.50), (6.00, 5.90, 6.10)]  # 1st straddles cp
    tfz = pd.DataFrame([dict(
        instrument="X", timeframe="1h", side="support", zone_center=c, zone_low=lo,
        zone_high=hi, touches=3, first_touch=ts, last_touch=ts, atr=0.2,
        current_price=cp, current_atr=0.2) for c, lo, hi in rows])
    fz = engine.score_and_merge(tfz, engine.DEFAULT_TIMEFRAME_WEIGHTS, 0.0, 1e9, 0.05)
    s = charting.summarize_zones(fz)
    assert s["current_zone"] is not None and s["current_zone"]["low"] <= cp <= s["current_zone"]["high"]
    assert s["nearest_support"]["high"] < cp        # the 4.30–4.50 zone, not the straddling one
    assert s["nearest_support"]["distance_atr"] > 0  # real edge distance, never 0


def test_zones_to_records_empty():
    assert charting.zones_to_records(pd.DataFrame()) == []


def test_zones_to_records_serializable():
    fz, _ = _result()
    recs = charting.zones_to_records(fz)
    assert isinstance(recs, list) and recs
    assert set(["side", "zone_center", "score", "timeframes"]).issubset(recs[0])
