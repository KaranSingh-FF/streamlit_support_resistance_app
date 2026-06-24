"""Roll captured ticks into 1-minute bars and merge them into the per-instrument master.

Idempotent by three independent layers: a per-file byte-offset cursor (rewound to BEFORE the
first still-forming-minute tick, so the open minute and a mid-minute restart never lose ticks),
``merge_into_master`` dedupe (re-merge = no-op), and atomic cursor writes."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pandas as pd

from .. import storage
from . import bars, instruments, paths, ticks

_DAY_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2}$")


class BarBuilder:
    def __init__(self, mapping=None, cursor_path=None):
        self.mapping = mapping if mapping is not None else instruments.load_instrument_map_from_disk()
        self.name_by_safe = {storage.safe_name(n): n for n in set(self.mapping.values())}
        self.cursor_path = cursor_path or paths.bars_cursor_path()
        self.cursor, self.last_minute = self._load()

    def _load(self):
        try:
            with open(self.cursor_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            offsets = {k: int(v) for k, v in (d.get("offsets") or {}).items()}
            last_min = {k: pd.Timestamp(v) for k, v in (d.get("last_minute") or {}).items()}
            return offsets, last_min
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return {}, {}

    def _save(self) -> None:
        storage.write_json_atomic(self.cursor_path, {
            "offsets": self.cursor,
            "last_minute": {k: str(v) for k, v in self.last_minute.items()},
        })

    @staticmethod
    def _safe_from_filename(fn: str) -> str:
        stem = fn[:-6] if fn.endswith(".jsonl") else fn      # drop .jsonl
        return _DAY_SUFFIX.sub("", stem)                     # drop trailing _YYYY-MM-DD

    def build_once(self, now_utc=None) -> dict:
        """Merge all newly-CLOSED 1-min bars across every tick log. Returns small stats."""
        now = now_utc or datetime.now(timezone.utc)
        ts_now = pd.Timestamp(now)
        if ts_now.tzinfo is not None:
            ts_now = ts_now.tz_convert("UTC").tz_localize(None)
        cutoff = ts_now.floor("1min")
        merged = 0
        instruments_touched = 0
        for f in sorted(paths.ticks_dir().glob("*.jsonl")):
            name = self.name_by_safe.get(self._safe_from_filename(f.name))
            if name is None:
                continue
            off = self.cursor.get(f.name, 0)
            pairs, end_off = ticks.read_ticks_indexed(f, off)
            if not pairs:
                continue
            new_cursor = end_off
            closed = []
            for start, rec in pairs:
                t = bars._tick_time(rec)
                if t is not None and t.floor("1min") >= cutoff:
                    new_cursor = min(new_cursor, start)     # rewind to before the forming minute
                else:
                    closed.append(rec)
            if closed:
                df = bars.bars_from_ticks(closed, name, drop_unclosed_after=now)
                # Only merge minutes strictly newer than the last one we merged for this instrument.
                # A late/out-of-order tick for an already-closed minute would otherwise rebuild that
                # minute's bar from a single price and overwrite the good bar (merge keeps last) -> corrupt.
                lm = self.last_minute.get(name)
                if lm is not None and not df.empty:
                    df = df[df["datetime"] > lm]
                if not df.empty:
                    storage.merge_into_master(df, name)
                    self.last_minute[name] = max(lm, df["datetime"].max()) if lm is not None else df["datetime"].max()
                    merged += len(df)
                    instruments_touched += 1
            self.cursor[f.name] = new_cursor
        self._save()
        return {"bars_merged": merged, "instruments": instruments_touched, "cutoff": str(cutoff)}
