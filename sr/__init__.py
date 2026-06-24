"""Multi-timeframe support & resistance toolkit.

Layers:
- ``sr.engine``   : pure S/R math (no IO, no UI) — verified logic.
- ``sr.storage``  : per-instrument master data on disk (CSV) + dedup/merge.
- ``sr.charting`` : chart serialization (desktop candle/price JSON) + S/R zone Plotly figures (legacy Streamlit).
- ``sr.desktop``  : local-server app (Flask) that opens the UI in the browser.

The Streamlit app and the desktop app are both thin UIs over these modules.
"""

from . import engine, storage, charting  # noqa: F401

__all__ = ["engine", "storage", "charting"]
__version__ = "2.0.0"
