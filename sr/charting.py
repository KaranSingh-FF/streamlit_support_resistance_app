"""Interactive Plotly visuals for support/resistance.

``build_sr_figure`` produces a stacked, multi-timeframe candlestick chart with:
- S/R zones as colour/opacity-graded filled bands (green support, red
  resistance) that are **legend-toggleable** and hover anywhere on the band;
- swing-high / swing-low markers showing where the levels came from;
- a current-price line, a crosshair (spikes), and range buttons.

Everything is grouped into four legend entries — Support, Resistance,
Swing High, Swing Low — so a click shows/hides a whole class across all panels.
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
GRID_COLOR = "#1c2230"
BG = "#0e1117"

_TF_ORDER = ["15min", "1h", "4h", "1D", "1W"]


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.3f})"


def _ordered_timeframes(tf_data: dict) -> list[str]:
    present = [tf for tf in _TF_ORDER if tf in tf_data and tf_data[tf] is not None and len(tf_data[tf])]
    present += [tf for tf in tf_data if tf not in present and tf_data[tf] is not None and len(tf_data[tf])]
    return present


def build_sr_figure(tf_data: dict, final_zones: pd.DataFrame, instrument: str, lookback: int = 300):
    """Build the multi-panel candlestick + S/R figure for ONE instrument."""
    timeframes = _ordered_timeframes(tf_data)
    if not timeframes:
        return go.Figure(layout={"template": "plotly_dark", "paper_bgcolor": BG, "plot_bgcolor": BG,
                                 "title": f"{instrument}: no chartable data"})

    zones = final_zones.copy() if final_zones is not None else pd.DataFrame()
    if not zones.empty:
        smin, smax = float(zones["score"].min()), float(zones["score"].max())
        span = (smax - smin) or 1.0

    seen_groups: set[str] = set()

    def first(group: str) -> bool:
        if group in seen_groups:
            return False
        seen_groups.add(group)
        return True

    n = len(timeframes)
    fig = make_subplots(
        rows=n, cols=1, shared_xaxes=False,
        vertical_spacing=0.07 if n > 1 else 0.0,
        subplot_titles=[f"<b>{tf}</b>" for tf in timeframes],
    )

    y_lo, y_hi = np.inf, -np.inf
    current_price = None

    for i, tf in enumerate(timeframes, start=1):
        d = tf_data[tf].tail(lookback).copy()
        fig.add_trace(
            go.Candlestick(
                x=d["datetime"], open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                name=tf, increasing_line_color=UP_COLOR, decreasing_line_color=DOWN_COLOR,
                increasing_fillcolor=UP_COLOR, decreasing_fillcolor=DOWN_COLOR,
                showlegend=False, whiskerwidth=0.3, line_width=1,
            ),
            row=i, col=1,
        )
        y_lo = min(y_lo, float(d["low"].min()))
        y_hi = max(y_hi, float(d["high"].max()))
        current_price = float(d["close"].iloc[-1])
        x0, x1 = d["datetime"].iloc[0], d["datetime"].iloc[-1]

        # swing markers (where levels come from)
        if "swing_high" in d.columns:
            sh = d[d["swing_high"]]
            if len(sh):
                fig.add_trace(go.Scatter(
                    x=sh["datetime"], y=sh["high"], mode="markers", name="Swing High",
                    legendgroup="swing_high", showlegend=first("swing_high"),
                    marker=dict(symbol="triangle-down", size=7, color=RESISTANCE_COLOR,
                                line=dict(width=0.5, color="#000")),
                    hovertemplate="swing high %{y:.4f}<extra></extra>",
                ), row=i, col=1)
        if "swing_low" in d.columns:
            sl = d[d["swing_low"]]
            if len(sl):
                fig.add_trace(go.Scatter(
                    x=sl["datetime"], y=sl["low"], mode="markers", name="Swing Low",
                    legendgroup="swing_low", showlegend=first("swing_low"),
                    marker=dict(symbol="triangle-up", size=7, color=SUPPORT_COLOR,
                                line=dict(width=0.5, color="#000")),
                    hovertemplate="swing low %{y:.4f}<extra></extra>",
                ), row=i, col=1)

        # current price line
        fig.add_hline(y=current_price, line=dict(color=PRICE_LINE_COLOR, width=1, dash="dot"), row=i, col=1)

        # S/R zones as legend-toggleable filled bands (hover anywhere on the band)
        if not zones.empty:
            for _, z in zones.iterrows():
                side = z["side"]
                color = SUPPORT_COLOR if side == "support" else RESISTANCE_COLOR
                norm = (float(z["score"]) - smin) / span
                alpha = 0.10 + 0.40 * norm
                y_lo = min(y_lo, float(z["zone_low"]))
                y_hi = max(y_hi, float(z["zone_high"]))
                grp = "support" if side == "support" else "resistance"
                fig.add_trace(go.Scatter(
                    x=[x0, x1, x1, x0, x0],
                    y=[z["zone_low"], z["zone_low"], z["zone_high"], z["zone_high"], z["zone_low"]],
                    fill="toself", fillcolor=_hex_to_rgba(color, alpha),
                    line=dict(width=0), mode="lines", hoveron="fills",
                    name=("Support" if side == "support" else "Resistance"),
                    legendgroup=grp, showlegend=first(grp),
                    text=(f"<b>{side.upper()}</b> @ {z['zone_center']:.4f}<br>"
                          f"range {z['zone_low']:.4f}–{z['zone_high']:.4f}<br>"
                          f"score {z['score']} · touches {int(z['touches'])}<br>"
                          f"timeframes: {z['timeframes']}<br>"
                          f"distance: {z['distance_atr']} ATR"),
                    hoverlabel=dict(bgcolor="#10151f", bordercolor=color),
                ), row=i, col=1)

    # zone labels on the right edge of the top panel (shared price axis)
    if not zones.empty:
        for _, z in zones.iterrows():
            color = SUPPORT_COLOR if z["side"] == "support" else RESISTANCE_COLOR
            tag = "S" if z["side"] == "support" else "R"
            fig.add_annotation(
                xref="paper", x=1.004, y=z["zone_center"], yref="y1",
                text=f"{tag} {z['zone_center']:.4f} · {z['score']}",
                showarrow=False, xanchor="left", font=dict(size=10, color=color),
            )

    if np.isfinite(y_lo) and np.isfinite(y_hi) and y_hi > y_lo:
        pad = (y_hi - y_lo) * 0.04
        fig.update_yaxes(range=[y_lo - pad, y_hi + pad])

    title = f"{instrument}  ·  last {current_price:.4f}" if current_price is not None else instrument
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=title, x=0.01, y=0.985, font=dict(size=18, color="#e6e9ef")),
        height=max(380, 320 * n + 90),
        margin=dict(l=55, r=110, t=70, b=40),
        hovermode="x",
        dragmode="zoom",
        paper_bgcolor=BG, plot_bgcolor=BG,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
        font=dict(family="Segoe UI, system-ui, sans-serif", color="#c7cdda"),
    )
    # crosshair + range buttons
    fig.update_xaxes(rangeslider_visible=False, showgrid=True, gridcolor=GRID_COLOR,
                     showspikes=True, spikemode="across", spikesnap="cursor",
                     spikethickness=1, spikedash="dot", spikecolor="#5a6577")
    fig.update_yaxes(showgrid=True, gridcolor=GRID_COLOR, showspikes=True,
                     spikethickness=1, spikedash="dot", spikecolor="#5a6577", side="right")
    # range buttons on the bottom panel
    fig.update_xaxes(rangeselector=dict(
        buttons=[dict(count=7, label="7d", step="day", stepmode="backward"),
                 dict(count=1, label="1m", step="month", stepmode="backward"),
                 dict(count=3, label="3m", step="month", stepmode="backward"),
                 dict(step="all", label="all")],
        bgcolor="#161b25", activecolor="#4c8dff", font=dict(color="#c7cdda"), x=0, y=1.0,
    ), row=n, col=1)
    return fig


def summarize_zones(final_zones: pd.DataFrame) -> dict:
    """Headline numbers for the UI cards: current price + nearest S/R."""
    if final_zones is None or final_zones.empty:
        return {"current_price": None, "nearest_support": None, "nearest_resistance": None,
                "n_support": 0, "n_resistance": 0}
    cp = float(final_zones["current_price"].iloc[0])

    # `side` is price-relative (support below price, resistance above), so the nearest
    # of each side is simply the closest zone of that side to the current price.
    def nearest(side):
        g = final_zones[final_zones["side"] == side]
        if g.empty:
            return None
        row = g.iloc[(g["zone_center"] - cp).abs().argmin()]
        return {"center": float(row["zone_center"]), "low": float(row["zone_low"]),
                "high": float(row["zone_high"]), "score": float(row["score"]),
                "distance_atr": float(row["distance_atr"]) if pd.notna(row["distance_atr"]) else None}

    return {
        "current_price": cp,
        "nearest_support": nearest("support"),
        "nearest_resistance": nearest("resistance"),
        "n_support": int((final_zones["side"] == "support").sum()),
        "n_resistance": int((final_zones["side"] == "resistance").sum()),
    }


def zones_to_records(final_zones: pd.DataFrame) -> list[dict]:
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
    return fig.to_html(include_plotlyjs=include_plotlyjs, full_html=full_html)


def plotlyjs_script() -> str:
    from plotly.offline import get_plotlyjs

    return f"<script type='text/javascript'>{get_plotlyjs()}</script>"
