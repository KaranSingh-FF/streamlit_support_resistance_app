"""Desktop API + storage tests, including the strict-JSON contract for pywebview."""
import json

import numpy as np
import pandas as pd
import pytest

from conftest import normalized, qh_excel_frame
from sr import desktop, storage


def _write_xlsx(path, frame):
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        frame.to_excel(w, index=False, sheet_name="Data")
    return path


# --- storage ----------------------------------------------------------------
def test_ingest_and_roundtrip(tmp_store):
    p = _write_xlsx(tmp_store / "QH.xlsx", qh_excel_frame(300))
    stats = storage.ingest_excel(p, "QH", "Data")
    assert stats["master_rows_after"] == 300 and stats["rows_dropped_bad"] == 0
    m = storage.load_master("QH")
    assert m is not None and pd.api.types.is_datetime64_any_dtype(m["datetime"])
    assert storage.list_instruments() == ["QH"]


def test_dedup_overwrite_and_append(tmp_store):
    full = normalized("1h", 300)
    _, s1 = storage.merge_into_master(full, "T")
    assert s1["master_rows_after"] == 300 and s1["net_new_rows"] == 300

    last_date = full["datetime"].max().date()
    last_day = full[full["datetime"].dt.date == last_date].copy()
    last_day["close"] = last_day["close"] + 99.0
    m2, s2 = storage.merge_into_master(last_day, "T")
    assert s2["net_new_rows"] == 0
    assert m2.duplicated(["instrument", "datetime"]).sum() == 0
    same = m2[m2["datetime"].dt.date == last_date]
    assert len(same) == len(last_day)
    assert abs(float(same["close"].iloc[-1]) - float(last_day["close"].iloc[-1])) < 1e-9

    nxt = last_day.copy()
    nxt["datetime"] = nxt["datetime"] + pd.Timedelta(days=1)
    nxt["close"] = nxt["close"] - 99.0
    _, s3 = storage.merge_into_master(nxt, "T")
    assert s3["net_new_rows"] == len(nxt)


def test_delete_master(tmp_store):
    storage.merge_into_master(normalized("1h", 50), "T")
    assert storage.delete_master("T") is True
    assert storage.list_instruments() == []
    assert storage.delete_master("T") is False  # already gone


def test_safe_name():
    assert storage.safe_name("BRN Oct26 / spread") == "BRN_Oct26_spread"


# --- desktop helpers --------------------------------------------------------
def test_jsonsafe_handles_special_types():
    out = desktop._jsonsafe({
        "ts": pd.Timestamp("2026-01-01"), "nat": pd.NaT, "npint": np.int64(3),
        "npflt": np.float64(1.5), "nan": float("nan"), "lst": [np.int64(1), pd.NaT],
    })
    json.dumps(out, allow_nan=False)  # must be strict-serializable
    assert out["nat"] is None and out["nan"] is None and out["npint"] == 3


def test_config_from_settings_overrides_and_types():
    cfg = desktop._config_from_settings({"timeframes": ["1h"], "min_score": 7, "lookback": 500})
    assert cfg.timeframes == ["1h"]
    assert cfg.min_score == 7.0 and isinstance(cfg.min_score, float)
    assert cfg.lookback == 500 and isinstance(cfg.lookback, int)


def test_config_from_settings_empty_defaults():
    cfg = desktop._config_from_settings(None)
    assert cfg.timeframes and cfg.atr_period == 14


# --- desktop API ------------------------------------------------------------
def test_api_update_and_run(tmp_store):
    api = desktop.Api()
    p = _write_xlsx(tmp_store / "QH.xlsx", qh_excel_frame(1000))
    upd = api.update_master(str(p), "QH", "Data")
    assert upd["ok"] and "QH" in upd["instruments"]

    res = api.run_sr("QH", {"timeframes": ["15min", "1h", "4h", "1D"], "min_score": 3.0,
                            "max_distance_atr": 10.0, "atr_multiplier": 0.25, "lookback": 300})
    assert res["ok"]
    # the entire payload must be strict-JSON (pywebview bridge contract)
    json.dumps(res, allow_nan=False)
    assert "figure" in res and "zones" in res and "summary" in res and "diagnostics" in res


def test_api_update_bad_path_returns_error():
    api = desktop.Api()
    res = api.update_master("does_not_exist.xlsx", "Z", "Data")
    assert res["ok"] is False and res["error"]


def test_api_run_unknown_instrument(tmp_store):
    api = desktop.Api()
    res = api.run_sr("NOPE", {})
    assert res["ok"] is False and "No master data" in res["error"]


def _invalid_xlsx(path):
    df = pd.DataFrame({
        "Date": ["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z"],
        "Open": [10, 10, 10], "High": [12, 8, 11], "Low": [8, 9, 10], "Close": [11, 10, 10.5],
    })  # middle row invalid (High 8 < Low 9)
    return _write_xlsx(path, df)


def test_preview_then_commit_remove_invalid(tmp_store):
    api = desktop.Api()
    p = _invalid_xlsx(tmp_store / "x.xlsx")
    pv = api.preview_upload(str(p), "T", "Data")
    assert pv["ok"] and pv["n_total"] == 3 and pv["n_valid"] == 2 and pv["n_invalid"] == 1
    json.dumps(pv, allow_nan=False)  # strict-JSON for the bridge
    res = api.commit_upload(pv["token"], [])  # keep none -> drop the invalid row
    assert res["ok"]
    assert res["stats"]["master_rows_after"] == 2 and res["stats"]["invalid_removed"] == 1


def test_preview_then_commit_keep_invalid(tmp_store):
    api = desktop.Api()
    p = _invalid_xlsx(tmp_store / "x.xlsx")
    pv = api.preview_upload(str(p), "T", "Data")
    res = api.commit_upload(pv["token"], [pv["invalid_rows"][0]["key"]])  # keep the invalid row
    assert res["ok"] and res["stats"]["master_rows_after"] == 3 and res["stats"]["invalid_kept"] == 1


def test_commit_with_expired_token_errors(tmp_store):
    assert desktop.Api().commit_upload("nope", [])["ok"] is False


def test_preview_clean_file_has_no_invalid(tmp_store):
    api = desktop.Api()
    p = _write_xlsx(tmp_store / "ok.xlsx", qh_excel_frame(200))
    pv = api.preview_upload(str(p), "QH", "Data")
    assert pv["ok"] and pv["n_invalid"] == 0


def test_build_page_inlines_plotly():
    page = desktop.build_page()
    assert "<!--PLOTLY_JS-->" not in page and "Plotly" in page


def test_selftest_passes():
    assert desktop.selftest(verbose=False) is True
