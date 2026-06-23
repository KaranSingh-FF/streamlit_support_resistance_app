"""Per-instrument master data on disk.

Master history is stored as **CSV** (one file per instrument) so there is no
binary dependency (pyarrow/parquet) — more portable and trivial to inspect or
ship inside a packaged .exe. The merge keeps exactly one row per
``instrument + datetime``; the most recently uploaded row wins on conflicts.

Storage location resolves (in order):
  1. ``$SR_DATA_DIR`` if set,
  2. ``./sr_data_store`` relative to the current working directory.
The desktop app pins this to a folder beside the executable.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

_BASE_DIR: Path | None = None


def get_base_dir() -> Path:
    global _BASE_DIR
    if _BASE_DIR is None:
        env = os.environ.get("SR_DATA_DIR")
        _BASE_DIR = Path(env) if env else Path("sr_data_store")
    return _BASE_DIR


def set_base_dir(path) -> None:
    """Override the storage root (called by the desktop app at startup)."""
    global _BASE_DIR
    _BASE_DIR = Path(path)
    ensure_dirs()


def raw_uploads_dir() -> Path:
    return get_base_dir() / "raw_uploads"


def master_dir() -> Path:
    return get_base_dir() / "master_data"


def output_dir() -> Path:
    return get_base_dir() / "outputs"


def ensure_dirs() -> None:
    for d in [raw_uploads_dir(), master_dir(), output_dir()]:
        d.mkdir(parents=True, exist_ok=True)


def safe_name(instrument: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", instrument).strip("_")


def master_path_for_instrument(instrument: str) -> Path:
    return master_dir() / f"{safe_name(instrument)}_master.csv"


def read_excel_any(file_obj_or_path, sheet_name=None) -> pd.DataFrame:
    if sheet_name:
        return pd.read_excel(file_obj_or_path, sheet_name=sheet_name, engine="openpyxl")
    return pd.read_excel(file_obj_or_path, engine="openpyxl")


def _read_master_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


def list_instruments() -> list[str]:
    """Display names of every instrument that has a saved master file."""
    ensure_dirs()
    names = []
    for p in sorted(master_dir().glob("*_master.csv")):
        try:
            df = pd.read_csv(p, usecols=["instrument"], nrows=1)
            names.append(str(df["instrument"].iloc[0]))
        except Exception:
            names.append(p.stem.replace("_master", ""))
    return names


def load_master(instrument: str) -> pd.DataFrame | None:
    p = master_path_for_instrument(instrument)
    if not p.exists():
        # tolerate being passed the safe name directly
        alt = master_dir() / f"{instrument}_master.csv"
        p = alt if alt.exists() else p
    if not p.exists():
        return None
    return _read_master_csv(p)


def save_raw_upload(name: str, data: bytes) -> Path:
    ensure_dirs()
    dest = raw_uploads_dir() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def delete_master(instrument: str) -> bool:
    p = master_path_for_instrument(instrument)
    if p.exists():
        p.unlink()
        return True
    return False


def merge_into_master(new_df: pd.DataFrame, instrument: str) -> tuple[pd.DataFrame, dict]:
    """First write creates the master; later writes append only new timestamps and
    overwrite duplicates with the latest uploaded values. Returns (master, stats)."""
    ensure_dirs()
    mp = master_path_for_instrument(instrument)
    before_rows = 0
    old_min = old_max = None

    if mp.exists():
        old_df = _read_master_csv(mp)
        before_rows = len(old_df)
        if before_rows:
            old_min, old_max = old_df["datetime"].min(), old_df["datetime"].max()
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df.copy()

    uploaded_rows = len(new_df)
    uploaded_min = new_df["datetime"].min() if uploaded_rows else None
    uploaded_max = new_df["datetime"].max() if uploaded_rows else None

    combined = combined.sort_values(["instrument", "datetime"])
    combined = combined.drop_duplicates(["instrument", "datetime"], keep="last").reset_index(drop=True)
    combined.to_csv(mp, index=False)

    after_rows = len(combined)
    stats = {
        "instrument": instrument,
        "uploaded_rows": uploaded_rows,
        "master_rows_before": before_rows,
        "master_rows_after": after_rows,
        "net_new_rows": after_rows - before_rows,
        "duplicates_removed_or_overwritten": before_rows + uploaded_rows - after_rows,
        "uploaded_min": uploaded_min,
        "uploaded_max": uploaded_max,
        "old_master_min": old_min,
        "old_master_max": old_max,
        "master_min": combined["datetime"].min() if after_rows else None,
        "master_max": combined["datetime"].max() if after_rows else None,
    }
    return combined, stats


def read_upload(file_path, sheet_name: str = "Data") -> pd.DataFrame:
    """Read an uploaded Excel file, falling back to the first sheet if the named
    sheet is absent."""
    try:
        return read_excel_any(file_path, sheet_name=sheet_name)
    except Exception:
        return read_excel_any(file_path, sheet_name=None)


def ingest_excel(file_path, instrument: str, sheet_name: str = "Data") -> dict:
    """Read an Excel file, normalize, and merge into the instrument's master.

    Returns the merge stats plus ``rows_in_file`` / ``rows_dropped_bad``.
    """
    from .engine import normalize_ohlcv

    raw = read_upload(file_path, sheet_name)
    normalized = normalize_ohlcv(raw, instrument)
    _, stats = merge_into_master(normalized, instrument)
    stats["rows_in_file"] = int(len(raw))
    stats["rows_dropped_bad"] = int(len(raw) - len(normalized))
    return stats
