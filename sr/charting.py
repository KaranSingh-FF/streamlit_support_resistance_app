"""Interactive Plotly visuals for support/resistance.

``build_sr_figure`` produces a stacked, multi-timeframe candlestick chart with
the (instrument-level) S/R zones overlaid as colour/opacity-graded bands so the
strongest levels stand out. Support is green, resistance is red. Every panel
shares one price axis so zones line up across timeframes, and hovering a level
shows its score, touches, contributing timeframes, and distance-to-price.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SUPPORT_COLOR = "#26a69a"
RESISTANCE_COLOR = "#ef5350"
UP_COLOR = "#26a69a"
DOWN_COLOR = "#ef5350"
PRICE_LINE_COLOR = "#f5d142"

# Display order, finest first (only those present in tf_data are drawn).
_TF_ORDER = ["15min", "1h", "4h", "1D", "1W"]


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.3f})"


def _ordered_timeframes(tf_data: dict) -> list[str]:
    present = [tf for tf in _TF_ORDER if tf in tf_data and tf_data[tf] is not None and len(tf_data[tf])]
    # include any non-standard timeframes at the end
    present += [tf for tf in tf_data if tf not in present and tf_data[tf] is not None and len(tf_data[tf])]
    return present


def build_sr_figure(tf_data: dict, final_zones: pd.DataFrame, instrument: str, lookback: int = 300):
    """Build the multi-panel candlestick + S/R figure for ONE instrument.

    tf_data: {timeframe -> resampled OHLC DataFrame with a 'datetime' column}.
    final_zones: the merged/scored zones for this instrument (already filtered).
    """
    timeframes = _ordered_timeframes(tf_data)
    if not timeframes:
        return go.Figure(layout={"template": "plotly_dark", "title": f"{instrument}: no chartable data"})

    zones = final_zones.copy() if final_zones is not None else pd.DataFrame()
    if not zones.empty:
        smin, smax = float(zones["score"].min()), float(zones["score"].max())
        span = (smax - smin) or 1.0
    current_price = None

    n = len(timeframes)
    fig = make_subplots(
        rows=n, cols=1, shared_xaxes=False,
        vertical_spacing=0.06 if n > 1 else 0.0,
        subplot_titles=[f"{instrument} — {tf}" for tf in timeframes],
    )

    y_lo, y_hi = np.inf, -np.inf
    for i, tf in enumerate(timeframes, start=1):
        d = tf_data[tf].tail(lookback).copy()
        fig.add_trace(
            go.Candlestick(
                x=d["datetime"], open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                name=tf, increasing_line_color=UP_COLOR, decreasing_line_color=DOWN_COLOR,
                increasing_fillcolor=UP_COLOR, decreasing_fillcolor=DOWN_COLOR,
                showlegend=False, whiskerwidth=0.4,
            ),
            row=i, col=1,
        )
        y_lo = min(y_lo, float(d["low"].min()))
        y_hi = max(y_hi, float(d["high"].max()))
        current_price = float(d["close"].iloc[-1])
        # current price reference line on each panel
        fig.add_hline(y=current_price, line=dict(color=PRICE_LINE_COLOR, width=1, dash="dot"), row=i, col=1)

        # S/R zones (same levels on every panel; bands graded by score)
        if not zones.empty:
            x0, x1 = d["datetime"].iloc[0], d["datetime"].iloc[-1]
            for _, z in zones.iterrows():
                color = SUPPORT_COLOR if z["side"] == "support" else RESISTANCE_COLOR
                norm = (float(z["score"]) - smin) / span
                opacity = 0.10 + 0.40 * norm
                y_lo = min(y_lo, float(z["zone_low"]))
                y_hi = max(y_hi, float(z["zone_high"]))
                fig.add_hrect(
                    y0=z["zone_low"], y1=z["zone_high"], line_width=0,
                    fillcolor=_hex_to_rgba(color, opacity), layer="below", row=i, col=1,
                )
                # invisible hover line at the zone centre carrying the full detail
                fig.add_trace(
                    go.Scatter(
                        x=[x0, x1], y=[z["zone_center"], z["zone_center"]],
                        mode="lines",
                        line=dict(color=color, width=1.4, dash="solid"),
                        opacity=min(1.0, 0.35 + 0.6 * norm),
                        showlegend=False, hoverinfo="text",
                        text=(
                            f"<b>{z['side'].upper()}</b> @ {z['zone_center']:.4f}<br>"
                            f"range {z['zone_low']:.4f}–{z['zone_high']:.4f}<br>"
                            f"score {z['score']} · touches {int(z['touches'])}<br>"
                            f"timeframes: {z['timeframes']}<br>"
                            f"distance: {z['distance_atr']} ATR"
                        ),
                    ),
                    row=i, col=1,
                )

    # label each zone once, on the top panel (price axis is shared visually)
    if not zones.empty:
        for _, z in zones.iterrows():
            tag = "S" if z["side"] == "support" else "R"
            fig.add_annotation(
                xref="paper", x=1.0, y=z["zone_center"], yref="y1",
                text=f"{tag} {z['zone_center']:.4f} ({z['score']})",
                showarrow=False, xanchor="left", font=dict(size=10, color=(SUPPORT_COLOR if z["side"] == "support" else RESISTANCE_COLOR)),
            )

    if np.isfinite(y_lo) and np.isfinite(y_hi) and y_hi > y_lo:
        pad = (y_hi - y_lo) * 0.04
        fig.update_yaxes(range=[y_lo - pad, y_hi + pad])

    title = f"{instrument}  ·  last {current_price:.4f}" if current_price is not None else instrument
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=title, x=0.01, font=dict(size=18)),
        height=max(360, 300 * n + 80),
        margin=dict(l=50, r=90, t=60, b=30),
        hovermode="closest",
        dragmode="zoom",
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
    )
    fig.update_xaxes(rangeslider_visible=False, showgrid=True, gridcolor="#1c2230")
    fig.update_yaxes(showgrid=True, gridcolor="#1c2230")
    return fig


def zones_to_records(final_zones: pd.DataFrame) -> list[dict]:
    """JSON-serializable rows for the side table (sorted strongest first)."""
    if final_zones is None or final_zones.empty:
        return []
    cols = ["side", "zone_center", "zone_low", "zone_high", "score", "touches",
            "timeframes", "distance_atr", "current_price", "last_touch"]
    df = final_zones[[c for c in cols if c in final_zones.columns]].copy()
    if "last_touch" in df.columns:
        df["last_touch"] = df["last_touch"].astype(str)
    return df.to_dict(orient="records")


def figure_to_json(fig) -> str:
    return fig.to_json()


def figure_to_html(fig, include_plotlyjs="cdn", full_html=True) -> str:
    """Standalone HTML (used by the browser fallback / saved reports).

    include_plotlyjs=True embeds plotly.js for fully offline files; 'cdn' is lighter.
    """
    return fig.to_html(include_plotlyjs=include_plotlyjs, full_html=full_html)


def plotlyjs_script() -> str:
    """The plotly.js library as an inline <script> (for offline embedding)."""
    from plotly.offline import get_plotlyjs

    return f"<script type='text/javascript'>{get_plotlyjs()}</script>"
