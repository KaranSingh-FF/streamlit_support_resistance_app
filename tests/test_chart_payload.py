"""chart_payload feeds the desktop Lightweight-Charts UI; it must produce
strictly-ascending integer candle times and survive strict-JSON (the HTTP bridge
invariant). Nearest-N level selection is client-side JS, not covered here."""
import json

from sr import charting, engine
from conftest import descending


def _payload():
    master = descending(800)
    final_zones, _, _, tf_data, _ = engine.compute_sr(master, engine.SRConfig())
    inst = master["instrument"].iloc[0]
    return charting.chart_payload(tf_data.get(inst, {}), final_zones, lookback=300), final_zones


def test_chart_payload_shape_and_times():
    cd, _ = _payload()
    assert cd["timeframes"], "expected at least one chartable timeframe"
    assert cd["default_tf"] in cd["timeframes"]
    assert isinstance(cd["current_price"], float)
    for tf, candles in cd["candles"].items():
        assert candles, f"{tf} has no candles"
        times = [c["time"] for c in candles]
        assert all(isinstance(t, int) for t in times)
        assert times == sorted(times) and len(times) == len(set(times)), f"{tf} times not strictly ascending"
        # epoch SECONDS, not us/ms — guards the pandas-3.0 datetime64[us] 1970 regression
        assert all(t > 1_000_000_000 for t in times), f"{tf} times look like wrong-resolution epochs (1970)"
        for c in candles:  # Lightweight Charts requires a real OHLC per bar (no nulls)
            assert c["high"] >= c["low"]


def test_chart_payload_is_strict_json():
    cd, _ = _payload()
    json.dumps(cd, allow_nan=False)  # raises if any NaN/Inf slipped through


def test_chart_payload_handles_empty_zones():
    master = descending(800)
    _, _, _, tf_data, _ = engine.compute_sr(master, engine.SRConfig())
    inst = master["instrument"].iloc[0]
    cd = charting.chart_payload(tf_data.get(inst, {}), final_zones=None, lookback=300)
    assert cd["current_atr"] is None
    assert isinstance(cd["current_price"], float)  # falls back to last candle close
    json.dumps(cd, allow_nan=False)
