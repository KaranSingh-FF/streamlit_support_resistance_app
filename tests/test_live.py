"""Live-feed pure-seam tests. Zero network: every function here is offline/deterministic."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from sr import engine, storage
from sr.live import (alerts, bar_builder, bars, config, hits, instruments,
                     monitor, paths, ticks, zones)
from sr.live.hits import Hit


def _master(name, rows=3):
    base = pd.Timestamp("2026-06-20 10:00:00")
    df = pd.DataFrame({
        "datetime": [base + pd.Timedelta(minutes=i) for i in range(rows)],
        "open": 70.0, "high": 70.3, "low": 69.9, "close": 70.1, "volume": 10, "instrument": name})
    storage.merge_into_master(df, name)


# --- instrument mapping -----------------------------------------------------
def test_instrument_map_real_json():
    m = instruments.load_instrument_map_from_disk()
    assert m["805633113204488608"] == "Brent N26"          # known OUTRIGHT id
    assert len(set(m.values())) == len(m)                  # names unique per id (tenor, not label)
    assert all(v and not v.endswith(" ") for v in m.values())


def test_instrument_map_type_suffix_and_disabled():
    j = {"products": {"Brent": {"display_name": "Brent", "enabled": True, "instruments": [
        {"tenor": "N26", "instrument_id": "111-2", "instrument_type": "OUTRIGHT", "enabled": True},
        {"tenor": "M26-N26", "instrument_id": "222-2", "instrument_type": "1MS", "enabled": True},
        {"tenor": "Q26", "instrument_id": "333-2", "instrument_type": "OUTRIGHT", "enabled": False},
    ]}, "WTI": {"display_name": "WTI", "enabled": False, "instruments": [
        {"tenor": "N26", "instrument_id": "444-2", "enabled": True}]}}}
    m = instruments.load_instrument_map(j)
    assert m == {"111": "Brent N26", "222": "Brent M26-N26 1MS"}   # disabled instr + disabled product skipped
    assert instruments.resolve_name("222-2", m) == "Brent M26-N26 1MS"
    assert instruments.resolve_name("999", m) is None


# --- bars -------------------------------------------------------------------
def test_bar_price_rules():
    assert bars.bar_price({"trade": 70.5, "bid": 70.4, "ask": 70.6}) == 70.5      # trade wins
    assert bars.bar_price({"bid": 70.0, "ask": 70.2}) == 70.1                     # mid
    assert bars.bar_price({"bid": 70.0, "ask": 69.0}) is None                     # crossed -> unusable
    assert bars.bar_price({"ask": 70.2}) == 70.2                                  # one side
    # spreads (FLY/1MS) legitimately quote negative/zero -> valid, NOT dropped
    assert bars.bar_price({"bid": -0.37, "ask": -0.35, "trade": -0.37}) == -0.37  # negative trade
    assert abs(bars.bar_price({"bid": -0.37, "ask": -0.35}) - (-0.36)) < 1e-9     # negative mid
    assert bars.bar_price({"bid": 0.0, "ask": 0.03}) == 0.015                     # zero bid valid
    assert bars.bar_price({"trade": float("nan"), "bid": None, "ask": None}) is None  # nothing finite


def test_negative_spread_builds_bar_and_fires_hit():
    tk = [_tick(0, -0.37, total_traded_qty=10), _tick(30, -0.35, total_traded_qty=12)]
    df = bars.bars_from_ticks(tk, "Brent N26 FLY")
    assert len(df) == 1 and df.iloc[0]["low"] == -0.37 and df.iloc[0]["close"] == -0.35
    z = [{"instrument": "Brent N26 FLY", "side": "support", "zone_low": -0.40, "zone_high": -0.36,
          "zone_center": -0.38, "confidence": "High", "bucket": "active", "touches": 3, "score": 9.0}]
    h = hits.detect_hits(bid=-0.39, ask=-0.36, zones=z)          # ask -0.36 <= support edge -0.36 -> fire
    assert len(h) == 1 and h[0].side == "support" and h[0].edge == -0.36


def _tick(minute_sec, price=None, **kw):
    base = pd.Timestamp("2026-06-24 10:00:00")
    t = base + pd.Timedelta(seconds=minute_sec)
    rec = {"exchange_time_ns": int(t.value), "ts": t.isoformat()}
    if price is not None:
        rec["trade"] = price
    rec.update(kw)
    return rec


def test_bars_from_ticks_ohlc_volume_and_unclosed():
    tk = [
        _tick(0, 70.0, total_traded_qty=100), _tick(20, 70.5, total_traded_qty=130),
        _tick(40, 69.8, total_traded_qty=160),                          # minute 10:00 o70 h70.5 l69.8 c69.8 vol60
        _tick(70, 71.0, total_traded_qty=200),                          # minute 10:01 (in-progress)
    ]
    # drop the still-forming 10:01 minute
    df = bars.bars_from_ticks(tk, "Brent N26", drop_unclosed_after=pd.Timestamp("2026-06-24 10:01:30"))
    assert list(df["datetime"]) == [pd.Timestamp("2026-06-24 10:00:00")]
    r = df.iloc[0]
    assert (r.open, r.high, r.low, r.close) == (70.0, 70.5, 69.8, 69.8)
    assert r.volume == 60.0 and r.instrument == "Brent N26"
    assert not engine.ohlc_invalid_mask(df).any()


def test_bars_quote_only_volume_zero_and_empty():
    tk = [_tick(0, bid=70.0, ask=70.2), _tick(30, bid=70.1, ask=70.3)]   # no trades, no ttq
    df = bars.bars_from_ticks(tk, "X")
    assert len(df) == 1 and df.iloc[0]["volume"] == 0.0
    assert bars.bars_from_ticks([], "X").empty
    assert list(bars.bars_from_ticks([], "X").columns) == ["datetime", "open", "high", "low", "close", "volume", "instrument"]


def test_bars_merge_into_master_idempotent(tmp_store):
    tk = [_tick(0, 70.0), _tick(40, 70.4), _tick(70, 70.2)]
    df = bars.bars_from_ticks(tk, "Brent N26", drop_unclosed_after=pd.Timestamp("2026-06-24 10:05"))
    m1, s1 = storage.merge_into_master(df, "Brent N26")
    m2, s2 = storage.merge_into_master(df, "Brent N26")     # re-merge same bars
    assert len(m1) == len(m2) and s2["net_new_rows"] == 0   # dedupe -> no-op


# --- hits -------------------------------------------------------------------
_ZONES = [
    {"instrument": "Brent N26", "side": "support", "zone_low": 70.10, "zone_high": 70.20,
     "zone_center": 70.15, "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0},
    {"instrument": "Brent N26", "side": "resistance", "zone_low": 71.00, "zone_high": 71.10,
     "zone_center": 71.05, "confidence": "Medium", "bucket": "nearby", "touches": 3, "score": 8.0},
    {"instrument": "Brent N26", "side": "support", "zone_low": 69.0, "zone_high": 69.1,
     "zone_center": 69.05, "confidence": "Low", "bucket": "historical", "touches": 1, "score": 2.0},
]


def test_detect_hits_execution_aware():
    # ask reaches support near-edge (70.20) -> support hit; bid below resistance -> no res hit
    h = hits.detect_hits(bid=70.18, ask=70.20, zones=_ZONES)
    assert len(h) == 1 and h[0].side == "support" and h[0].edge == 70.20 and h[0].hit_price == 70.20
    # bid reaches resistance near-edge (71.00) -> resistance hit
    h2 = hits.detect_hits(bid=71.00, ask=71.02, zones=_ZONES)
    assert len(h2) == 1 and h2[0].side == "resistance" and h2[0].edge == 71.00 and h2[0].hit_price == 71.00


def test_detect_hits_rejects_crossed_book():
    # a transient crossed/half-updated quote (bid>ask) must fire NOTHING (else false support+resistance)
    crossed = [
        {"instrument": "X", "side": "support", "zone_low": 70.10, "zone_high": 70.20, "zone_center": 70.15,
         "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0},
        {"instrument": "X", "side": "resistance", "zone_low": 70.30, "zone_high": 70.40, "zone_center": 70.35,
         "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0}]
    assert hits.detect_hits(bid=70.50, ask=70.00, zones=crossed) == []


def test_detect_hits_skips_low_and_bad_prices():
    low_only = [{"instrument": "X", "side": "support", "zone_low": 69.0, "zone_high": 69.1,
                 "zone_center": 69.05, "confidence": "Low", "bucket": "historical", "touches": 1, "score": 2.0}]
    assert hits.detect_hits(bid=69.0, ask=69.05, zones=low_only) == []      # Low confidence skipped
    assert hits.detect_hits(bid=float("nan"), ask=None, zones=_ZONES) == []  # no usable prices -> no fire
    assert hits.detect_hits(bid=0, ask=-1, zones=_ZONES) == []


# --- alerts: dedupe, staleness, card ---------------------------------------
def test_zone_key_stable_under_jitter():
    a = alerts.zone_key("Brent N26", "support", 70.1000001, 70.2000003)
    b = alerts.zone_key("Brent N26", "support", 70.0999998, 70.1999999)
    assert a == b


def test_zone_key_fine_tick_distinguishes_subcent_zones():
    # on a 0.001-tick instrument, two zones 0.001 apart must NOT collapse to one dedupe key
    t = 0.001
    a = alerts.zone_key("Fly", "support", 0.120, 0.122, tick=t)
    b = alerts.zone_key("Fly", "support", 0.123, 0.124, tick=t)
    assert a != b
    # default 0.01 tick WOULD have collapsed them (regression guard)
    assert alerts.zone_key("Fly", "support", 0.120, 0.122) == alerts.zone_key("Fly", "support", 0.123, 0.124)


def test_should_fire_and_restart_safety(tmp_store):
    state = alerts.load_state()
    day, key = "2026-06-24", "Brent N26|support|70.10|70.20"
    assert alerts.should_fire(state, day, key) is True
    state = alerts.record_fire(state, day, key, {"ts": "t", "bid": 70.1, "ask": 70.2, "hit_price": 70.2})
    alerts.save_state(state)
    assert alerts.should_fire(state, day, key) is False
    reloaded = alerts.load_state()                          # restart mid-day
    assert alerts.should_fire(reloaded, day, key) is False  # must NOT re-fire
    assert alerts.should_fire(reloaded, "2026-06-25", key) is True   # next UTC day re-arms


def test_is_stale_cases():
    now = datetime(2026, 6, 24, 10, 0, 30, tzinfo=timezone.utc)
    fresh = {"written_at": "2026-06-24T10:00:20+00:00"}
    assert alerts.is_stale({"status": "CONNECTED"}, fresh, now, max_age_sec=30)[0] is False
    assert alerts.is_stale({"status": "DISCONNECTED"}, fresh, now)[0] is True
    assert alerts.is_stale({"status": "CONNECTED"}, {"written_at": "2026-06-24T09:50:00+00:00"}, now)[0] is True
    assert alerts.is_stale(None, fresh, now)[0] is True
    assert alerts.is_stale({"status": "CONNECTED"}, {"written_at": "nope"}, now)[0] is True


def test_build_alert_and_card_strict_json():
    h = hits.detect_hits(bid=70.18, ask=70.20, zones=_ZONES)[0]
    a = alerts.build_alert(h, "805633113204488608", datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc))
    assert a["side"] == "support" and a["edge"] == 70.20 and a["hit_price"] == 70.20
    json.dumps(a, allow_nan=False)
    card = alerts.teams_card(a, decimals=2)
    assert card["themeColor"] == "2EB67D"           # green for support
    json.dumps(card, allow_nan=False)               # card is strict-JSON


# --- config -----------------------------------------------------------------
def test_config_roundtrip_and_env_override(tmp_store, monkeypatch):
    monkeypatch.delenv("SR_TEAMS_WEBHOOK", raising=False)
    cfg = config.read_config()
    assert config.effective_webhook(cfg) is None and config.webhook_source(cfg) == "none"
    config.write_config({"teams_webhook": "https://x.webhook.office.com/abc", "monitored": {"Brent N26": True}})
    cfg2 = config.read_config()
    assert config.effective_webhook(cfg2) == "https://x.webhook.office.com/abc"
    assert cfg2["monitored"]["Brent N26"] is True and cfg2["monitor"]["poll_sec"] == 2
    monkeypatch.setenv("SR_TEAMS_WEBHOOK", "https://env.example/hook")
    assert config.effective_webhook(cfg2) == "https://env.example/hook" and config.webhook_source(cfg2) == "env"


# --- tick log + atomic write ------------------------------------------------
def test_tick_log_offset_idempotent(tmp_store):
    p = storage.get_base_dir() / "ticks" / "t.jsonl"
    ticks.append_tick(p, {"ts": "a", "trade": 1.0})
    ticks.append_tick(p, {"ts": "b", "trade": 2.0})
    recs, off = ticks.read_ticks(p, 0)
    assert [r["ts"] for r in recs] == ["a", "b"]
    recs2, off2 = ticks.read_ticks(p, off)
    assert recs2 == [] and off2 == off                      # nothing new
    ticks.append_tick(p, {"ts": "c", "trade": 3.0})
    recs3, _ = ticks.read_ticks(p, off)
    assert [r["ts"] for r in recs3] == ["c"]                 # only the new line


def test_write_json_atomic(tmp_store):
    p = storage.get_base_dir() / "x" / "y.json"
    storage.write_json_atomic(p, {"a": 1, "b": [1, 2.5], "c": None})
    assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2.5], "c": None}
    with pytest.raises(ValueError):                          # strict-JSON: NaN rejected
        storage.write_json_atomic(p, {"bad": float("nan")})


# --- bar builder: closed-minute merge, forming-minute retention, idempotency ---
def _btick(sec, price, ttq):
    t = pd.Timestamp("2026-06-24 10:00:00") + pd.Timedelta(seconds=sec)
    return {"ts": t.isoformat() + "+00:00", "exchange_time_ns": int(t.value), "trade": price, "total_traded_qty": ttq}


def test_bar_builder_closed_forming_idempotent(tmp_store):
    name = "Brent N26"
    path = paths.tick_log_path(storage.safe_name(name), "2026-06-24")
    for sec, p, q in [(0, 70.0, 100), (40, 70.4, 140), (330, 70.2, 200)]:   # 10:00 closed; 10:05 forming
        ticks.append_tick(path, _btick(sec, p, q))
    bb = bar_builder.BarBuilder(mapping={"805633113204488608": name})
    now = datetime(2026, 6, 24, 10, 5, 30, tzinfo=timezone.utc)
    bb.build_once(now_utc=now)
    mins = list(storage.load_master(name)["datetime"])
    assert pd.Timestamp("2026-06-24 10:00:00") in mins
    assert pd.Timestamp("2026-06-24 10:05:00") not in mins        # forming minute withheld
    n_after_first = len(mins)
    bb.build_once(now_utc=now)                                    # no new ticks -> idempotent
    assert len(storage.load_master(name)) == n_after_first
    ticks.append_tick(path, _btick(350, 70.3, 210))              # close the 10:05 minute
    bb.build_once(now_utc=datetime(2026, 6, 24, 10, 6, 5, tzinfo=timezone.utc))
    assert pd.Timestamp("2026-06-24 10:05:00") in list(storage.load_master(name)["datetime"])


def test_bar_builder_late_tick_does_not_overwrite_bar(tmp_store):
    name = "Brent N26"
    path = paths.tick_log_path(storage.safe_name(name), "2026-06-24")
    for sec, p, q in [(0, 70.0, 100), (20, 70.5, 130), (40, 69.8, 160)]:   # full 10:00 bar
        ticks.append_tick(path, _btick(sec, p, q))
    bb = bar_builder.BarBuilder(mapping={"805633113204488608": name})
    bb.build_once(now_utc=datetime(2026, 6, 24, 10, 5, tzinfo=timezone.utc))
    good = storage.load_master(name).set_index("datetime").loc[pd.Timestamp("2026-06-24 10:00:00")]
    assert (good["open"], good["high"], good["low"], good["close"]) == (70.0, 70.5, 69.8, 69.8)
    # a LATE single tick for the already-merged 10:00 minute must NOT overwrite the good bar
    ticks.append_tick(path, _btick(10, 99.0, 999))                # late, minute 10:00
    bb.build_once(now_utc=datetime(2026, 6, 24, 10, 6, tzinfo=timezone.utc))
    again = storage.load_master(name).set_index("datetime").loc[pd.Timestamp("2026-06-24 10:00:00")]
    assert (again["open"], again["high"], again["low"], again["close"]) == (70.0, 70.5, 69.8, 69.8)


def test_monitor_real_shaped_snapshot(tmp_store, monkeypatch):
    # real production snapshot shape: bare-id keys, full records, ~12% with no live quote (None bid/ask)
    monkeypatch.delenv("SR_TEAMS_WEBHOOK", raising=False)
    n26, m26 = "Brent N26", "Brent M26"   # real ids 805633113204488608 / 8914890790769645064
    _master(n26); _master(m26)
    config.write_config({"teams_webhook": "https://x.webhook.office.com/hook"})
    posted = []
    monkeypatch.setattr(zones.ZoneCache, "get", lambda self, name: ([
        {"instrument": name, "side": "support", "zone_low": 70.10, "zone_high": 70.20, "zone_center": 70.15,
         "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0}], 0.01))
    monkeypatch.setattr(monitor.teams, "post_teams", lambda url, card, **k: (posted.append(card) or True))
    now = datetime.now(timezone.utc)
    storage.write_json_atomic(paths.snapshot_path("l1"), {"sequence": 99, "written_at": now.isoformat(), "latest": {
        "805633113204488608": {"instrumentId": "805633113204488608", "bidPrice": 70.18, "askPrice": 70.20,
                               "tradePrice": 70.19, "_recv_ts": now.isoformat()},
        "8914890790769645064": {"instrumentId": "8914890790769645064", "bidPrice": None, "askPrice": None,
                               "tradePrice": None, "_recv_ts": now.isoformat()},
    }})
    storage.write_json_atomic(paths.feed_status_path("l1"), {"status": "CONNECTED"})
    monitor.Monitor()._tick()
    assert len(posted) == 1                          # the quoted instrument fires; the no-quote one is skipped, no crash
    assert alerts.read_recent_alerts(5)[0]["instrument"] == n26


def test_monitor_per_instrument_staleness(tmp_store, monkeypatch):
    monkeypatch.delenv("SR_TEAMS_WEBHOOK", raising=False)
    name = "Brent N26"
    _master(name)
    config.write_config({"teams_webhook": "https://x.webhook.office.com/hook"})
    posted = []
    monkeypatch.setattr(zones.ZoneCache, "get", lambda self, n: ([
        {"instrument": name, "side": "support", "zone_low": 70.10, "zone_high": 70.20, "zone_center": 70.15,
         "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0}], 0.01))
    monkeypatch.setattr(monitor.teams, "post_teams", lambda url, card, **k: (posted.append(card) or True))
    now = datetime.now(timezone.utc)
    # snapshot is fresh overall (written_at now), but THIS instrument's quote is 10 min old -> must NOT fire
    storage.write_json_atomic(paths.snapshot_path("l1"), {"sequence": 1, "written_at": now.isoformat(),
        "latest": {"805633113204488608": {"bidPrice": 70.18, "askPrice": 70.20, "tradePrice": 70.19,
                                          "_recv_ts": (now - timedelta(seconds=600)).isoformat()}}})
    storage.write_json_atomic(paths.feed_status_path("l1"), {"status": "CONNECTED"})
    monitor.Monitor()._tick()
    assert posted == []   # frozen per-instrument quote suppressed despite a fresh snapshot


# --- zone cache filter ------------------------------------------------------
def test_cached_zones_filters_low():
    fz = pd.DataFrame([
        {"instrument": "X", "side": "support", "zone_low": 1.0, "zone_high": 1.1, "zone_center": 1.05,
         "confidence": "High", "bucket": "active", "touches": 3, "score": 9.0},
        {"instrument": "X", "side": "support", "zone_low": 0.5, "zone_high": 0.6, "zone_center": 0.55,
         "confidence": "Low", "bucket": "historical", "touches": 1, "score": 2.0}])
    z = zones.cached_zones_from_frame(fz)
    assert len(z) == 1 and z[0]["confidence"] == "High" and isinstance(z[0]["zone_low"], float)
    assert zones.cached_zones_from_frame(pd.DataFrame()) == []


# --- monitor end-to-end (network + S/R stubbed) -----------------------------
def test_monitor_fires_once_then_dedups_and_gates_stale(tmp_store, monkeypatch):
    monkeypatch.delenv("SR_TEAMS_WEBHOOK", raising=False)
    name = "Brent N26"
    _master(name)                                   # so the instrument "has a master"
    config.write_config({"teams_webhook": "https://x.webhook.office.com/hook"})
    posted = []
    monkeypatch.setattr(zones.ZoneCache, "get", lambda self, n: ([
        {"instrument": name, "side": "support", "zone_low": 70.10, "zone_high": 70.20, "zone_center": 70.15,
         "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0}], 0.01))
    monkeypatch.setattr(monitor.teams, "post_teams", lambda url, card, **k: (posted.append(card) or True))

    def snap(written_at, ask=70.20):
        storage.write_json_atomic(paths.snapshot_path("l1"), {"sequence": 1, "written_at": written_at,
            "latest": {"805633113204488608": {"bidPrice": 70.18, "askPrice": ask, "tradePrice": 70.19}}})
        storage.write_json_atomic(paths.feed_status_path("l1"), {"status": "CONNECTED"})

    now = datetime.now(timezone.utc)
    snap(now.isoformat())
    m = monitor.Monitor()
    m._tick()
    assert len(posted) == 1                          # ask 70.20 <= support edge 70.20 -> fire
    logged = alerts.read_recent_alerts(10)           # monitor persists to disk (routes read it)
    assert logged and logged[0]["side"] == "support" and logged[0]["instrument"] == name
    m._tick()
    assert len(posted) == 1                          # same zone, same day -> deduped

    # stale snapshot must NEVER fire (even though a fresh hit would)
    posted.clear()
    monkeypatch.setattr(zones.ZoneCache, "get", lambda self, n: ([
        {"instrument": name, "side": "resistance", "zone_low": 70.00, "zone_high": 70.05, "zone_center": 70.02,
         "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0}], 0.01))
    snap("2026-01-01T00:00:00+00:00")                # ancient -> stale
    monitor.Monitor()._tick()
    assert posted == []                              # stale feed -> never fire
