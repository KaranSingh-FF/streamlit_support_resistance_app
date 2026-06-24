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
    # 'actionable' may skip the mathematically-closest edge for a stronger/recent one,
    # but it must still be a real below/above-price zone with a positive edge distance.
    if s["nearest_support"]:
        assert s["nearest_support"]["high"] < cp + 1e-9
        assert s["nearest_support"]["distance_atr"] > 0
        assert any(abs(s["nearest_support"]["high"] - h) < 1e-9 for h in below["zone_high"])
    if s["nearest_resistance"]:
        assert s["nearest_resistance"]["low"] > cp - 1e-9
        assert s["nearest_resistance"]["distance_atr"] > 0
        assert any(abs(s["nearest_resistance"]["low"] - lo) < 1e-9 for lo in above["zone_low"])
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


# --- zero-config / actionable additions -------------------------------------
def test_summary_has_plain_english_and_if_then():
    fz, _ = _result()
    s = charting.summarize_zones(fz)
    assert isinstance(s["plain_english"], str) and s["plain_english"]
    assert isinstance(s["if_then"], list)
    for r in s["if_then"]:
        assert set(["trigger", "action", "kind"]).issubset(r) and r["kind"] in {"bull", "bear", "neutral"}
    # whole summary must be strict-JSON (HTTP bridge contract)
    json.dumps(s, allow_nan=False)


def test_summary_empty_has_all_keys():
    s = charting.summarize_zones(pd.DataFrame())
    for k in ("plain_english", "if_then", "top_support_zones", "top_resistance_zones",
              "nearest_support", "nearest_resistance", "current_zone"):
        assert k in s
    assert s["plain_english"] == "" and s["if_then"] == []


def test_top_zones_sorted_and_capped():
    fz, _ = _result()
    s = charting.summarize_zones(fz)
    for key, side in (("top_support_zones", "support"), ("top_resistance_zones", "resistance")):
        recs = s[key]
        assert len(recs) <= 5
        assert all(r["side"] == side for r in recs)
        if len(recs) > 1 and "confidence_score" in recs[0]:
            cs = [r["confidence_score"] for r in recs]
            assert cs == sorted(cs, reverse=True)


def test_actionable_nearest_skips_low_confidence():
    # a closer Low-confidence support is skipped for a farther non-Low one
    cp, ts = 5.0, pd.Timestamp("2026-01-01")
    fz = pd.DataFrame([
        dict(side="support", zone_center=4.8, zone_low=4.7, zone_high=4.9, score=2.0,
             confidence="Low", days_since_touch=5.0, current_price=cp, current_atr=0.2),
        dict(side="support", zone_center=4.2, zone_low=4.1, zone_high=4.3, score=12.0,
             confidence="High", days_since_touch=3.0, current_price=cp, current_atr=0.2),
    ])
    s = charting.summarize_zones(fz)
    assert s["nearest_support"]["center"] == 4.2  # the High one, not the closer Low one


def test_actionable_falls_back_when_all_low():
    cp = 5.0
    fz = pd.DataFrame([
        dict(side="support", zone_center=4.8, zone_low=4.7, zone_high=4.9, score=1.0,
             confidence="Low", days_since_touch=5.0, current_price=cp, current_atr=0.2),
        dict(side="support", zone_center=4.2, zone_low=4.1, zone_high=4.3, score=1.0,
             confidence="Low", days_since_touch=3.0, current_price=cp, current_atr=0.2),
    ])
    s = charting.summarize_zones(fz)
    assert s["nearest_support"] is not None and s["nearest_support"]["center"] == 4.8  # closest fallback
