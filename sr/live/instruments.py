"""Map a feed ``instrument_id`` to a human display name used as the S/R/master key.

Name = ``"{display_name} {tenor}"``; a non-OUTRIGHT type is appended (``"Brent M26-N26 1MS"``)
because ``tenor`` is unique per id in the real feed while ``label`` collides across ids — so
this guarantees exactly one master file per instrument_id."""
from __future__ import annotations

import json
from pathlib import Path

from . import paths


def load_instrument_map(instruments_json: dict) -> dict:
    """{bare instrument_id: display name} for every enabled product+instrument."""
    out: dict[str, str] = {}
    for prod in (instruments_json.get("products") or {}).values():
        if not prod.get("enabled"):
            continue
        display = str(prod.get("display_name") or "").strip()
        for entry in prod.get("instruments", []):
            if not entry.get("enabled"):
                continue
            raw = str(entry.get("instrument_id", "")).strip()
            if not raw or raw.lower() == "none":
                continue
            iid = raw.split("-")[0]                      # snapshot keys are the bare id
            tenor = str(entry.get("tenor") or entry.get("label") or "").strip()
            itype = str(entry.get("instrument_type") or "OUTRIGHT").strip()
            name = f"{display} {tenor}".strip()
            if itype and itype.upper() != "OUTRIGHT":
                name = f"{name} {itype}".strip()
            if name:
                out[iid] = name
    return out


def load_instrument_map_from_disk(path: "Path | None" = None) -> dict:
    p = path or paths.instruments_json_path()
    with open(p, "r", encoding="utf-8") as fh:
        return load_instrument_map(json.load(fh))


def resolve_name(instrument_id, mapping: dict) -> "str | None":
    return mapping.get(str(instrument_id).split("-")[0])
