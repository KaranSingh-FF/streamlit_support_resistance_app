"""Multi-timeframe support & resistance toolkit.

Layers:
- ``sr.engine``   : pure S/R math (no IO, no UI) — verified logic.
- ``sr.storage``  : per-instrument master data on disk (CSV) + dedup/merge.
- ``sr.charting`` : interactive Plotly candlestick + S/R zone figures.
- ``sr.desktop``  : local-server app (Flask) that opens the UI in the browser.

The Streamlit app and the desktop app are both thin UIs over these modules.
"""

from . import engine, storage, charting  # noqa: F401

__all__ = ["engine", "storage", "charting"]
__version__ = "1.0.0"
