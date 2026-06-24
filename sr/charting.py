"""Chart serialization + UI helpers for support/resistance.

The **desktop UI** consumes ``chart_payload`` (candles + reference price/ATR for
TradingView Lightweight Charts), ``summarize_zones`` (headline cards) and
``zones_to_records`` (table). ``build_sr_figure`` below is **legacy Streamlit only**.

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


def _tick_decimals(tick: float) -> int:
    """Decimal places implied by a tick size (0.01 -> 2)."""
    if not tick or tick <= 0:
        return 4
    import math

    return max(0, min(8, int(-math.floor(math.log10(tick)))))


def build_sr_figure(tf_data: dict, final_zones: pd.DataFrame, instrument: str, lookback: int = 300, tick_size: float = 0.01):
    """Build the multi-panel candlestick + S/R figure for ONE instrument."""
    dec = _tick_decimals(tick_size)
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
        y_lo = min(y_lo, float(d["low"].min()))
        y_hi = max(y_hi, float(d["high"].max()))
        current_price = float(d["close"].iloc[-1])
        x0, x1 = d["datetime"].iloc[0], d["datetime"].iloc[-1]

        # S/R zones FIRST, so the candlesticks (added below) draw ON TOP of the
        # bands. Plotly renders traces in add-order; bands added last would bury
        # the candles — which is what made the 15min panel unreadable.
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
                    text=(f"<b>{side.upper()}</b> @ {z['zone_center']:.{dec}f}<br>"
                          f"range {z['zone_low']:.{dec}f}–{z['zone_high']:.{dec}f}<br>"
                          f"score {z['score']} · touches {int(z['touches'])}<br>"
                          f"timeframes: {z['timeframes']}<br>"
                          f"distance: {z['distance_atr']} ATR"),
                    hoverlabel=dict(bgcolor="#10151f", bordercolor=color),
                ), row=i, col=1)

        fig.add_trace(
            go.Candlestick(
                x=d["datetime"], open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                name=tf, increasing_line_color=UP_COLOR, decreasing_line_color=DOWN_COLOR,
                increasing_fillcolor=UP_COLOR, decreasing_fillcolor=DOWN_COLOR,
                showlegend=False, whiskerwidth=0.3, line_width=1,
            ),
            row=i, col=1,
        )

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

        # current price line (a layout shape — always drawn above the traces)
        fig.add_hline(y=current_price, line=dict(color=PRICE_LINE_COLOR, width=1, dash="dot"), row=i, col=1)

    # zone labels on the right edge of the top panel (shared price axis)
    if not zones.empty:
        for _, z in zones.iterrows():
            color = SUPPORT_COLOR if z["side"] == "support" else RESISTANCE_COLOR
            tag = "S" if z["side"] == "support" else "R"
            fig.add_annotation(
                xref="paper", x=1.004, y=z["zone_center"], yref="y1",
                text=f"{tag} {z['zone_center']:.{dec}f} · {z['score']}",
                showarrow=False, xanchor="left", font=dict(size=10, color=color),
            )

    if np.isfinite(y_lo) and np.isfinite(y_hi) and y_hi > y_lo:
        pad = (y_hi - y_lo) * 0.04
        fig.update_yaxes(range=[y_lo - pad, y_hi + pad])

    title = f"{instrument}  ·  last {current_price:.{dec}f}" if current_price is not None else instrument
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=title, x=0.01, y=0.985, font=dict(size=18, color="#e6e9ef")),
        height=max(380, 320 * n + 90),
        margin=dict(l=55, r=110, t=70, b=64),
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


def summarize_zones(final_zones: pd.DataFrame, price_decimals: "int | None" = None) -> dict:
    """Headline summary for the UI: current price, the zone the price is inside (if any),
    the nearest ACTIONABLE support/resistance, the top zones per side, plus a plain-English
    blurb and an if/then map.

    Distance is to the zone's near EDGE (price - zone_high for support, zone_low - price for
    resistance). 'Actionable' = the closest zone that is non-Low confidence and recently
    touched (<=90d); if none qualify it falls back to the mathematically closest. A zone the
    price sits inside is reported as ``current_zone`` and excluded from the nearest cards."""
    empty = {"current_price": None, "current_atr": None, "nearest_support": None,
             "nearest_resistance": None, "current_zone": None, "n_support": 0, "n_resistance": 0,
             "top_support_zones": [], "top_resistance_zones": [], "plain_english": "", "if_then": []}
    if final_zones is None or final_zones.empty:
        return empty
    cp = float(final_zones["current_price"].iloc[0])
    if not np.isfinite(cp):   # never emit "Price is nan." or confuse the cards
        return empty
    atr = (float(final_zones["current_atr"].iloc[0]) if "current_atr" in final_zones.columns
           and pd.notna(final_zones["current_atr"].iloc[0]) else None)
    has = lambda c: c in final_zones.columns

    def pack(row, gap):  # gap = price-distance to the zone's near edge
        d = {"center": float(row["zone_center"]), "low": float(row["zone_low"]),
             "high": float(row["zone_high"]), "score": float(row["score"]),
             "distance_atr": round(gap / atr, 2) if atr else None}
        if has("confidence"): d["confidence"] = str(row["confidence"])
        if has("bucket"): d["bucket"] = str(row["bucket"])
        if has("touches"): d["touches"] = int(row["touches"])
        if has("volume_at_level") and pd.notna(row["volume_at_level"]):
            d["volume_at_level"] = float(row["volume_at_level"])
        if has("days_since_touch"):
            d["days_since_touch"] = None if pd.isna(row["days_since_touch"]) else round(float(row["days_since_touch"]), 1)
        return d

    inside = (final_zones["zone_low"] <= cp) & (final_zones["zone_high"] >= cp)
    cur = final_zones[inside]
    current_zone = None
    if not cur.empty:
        row = cur.iloc[cur["score"].argmax()]   # if price sits in several, show the strongest
        current_zone = {"center": float(row["zone_center"]), "low": float(row["zone_low"]),
                        "high": float(row["zone_high"]), "score": float(row["score"]),
                        "side": str(row["side"])}
        if has("confidence"): current_zone["confidence"] = str(row["confidence"])

    def actionable(cands, edge_key, pick):  # pick: 'max' (support top edge) / 'min' (resistance bottom edge)
        if cands.empty:
            return None
        use = cands
        if has("confidence"):  # prefer strong + recent; fall back to closest if none qualify
            strong = cands[cands["confidence"] != "Low"]
            if has("days_since_touch"):
                strong = strong[strong["days_since_touch"].fillna(0) <= 90]
            if not strong.empty:
                use = strong
        r = use.iloc[use[edge_key].argmax() if pick == "max" else use[edge_key].argmin()]
        gap = (cp - float(r["zone_high"])) if pick == "max" else (float(r["zone_low"]) - cp)
        return pack(r, gap)

    sup = final_zones[~inside & (final_zones["zone_high"] < cp)]
    res = final_zones[~inside & (final_zones["zone_low"] > cp)]
    nearest_support = actionable(sup, "zone_high", "max")
    nearest_resistance = actionable(res, "zone_low", "min")

    sort_key = "confidence_score" if has("confidence_score") else "score"

    def top(side):
        g = final_zones[final_zones["side"] == side]
        return zones_to_records(g.sort_values(sort_key, ascending=False).head(5)) if not g.empty else []

    dec = price_decimals if price_decimals is not None else (2 if abs(cp) >= 10 else 4)
    summary = {
        "current_price": cp, "current_atr": atr,
        "nearest_support": nearest_support, "nearest_resistance": nearest_resistance,
        "current_zone": current_zone,
        "n_support": int((final_zones["side"] == "support").sum()),
        "n_resistance": int((final_zones["side"] == "resistance").sum()),
        "top_support_zones": top("support"), "top_resistance_zones": top("resistance"),
    }
    # full per-side centers so if/then finds the genuine NEXT level by price (not just within top-5)
    all_sup = sorted(final_zones.loc[final_zones["side"] == "support", "zone_center"].astype(float))
    all_res = sorted(final_zones.loc[final_zones["side"] == "resistance", "zone_center"].astype(float))
    summary["plain_english"] = plain_english(summary, dec)
    summary["if_then"] = if_then(summary, dec, all_sup, all_res)
    return summary


def _atr_phrase(z):
    d = z.get("distance_atr") if z else None
    return "—" if d is None else f"{d:.2f} ATR away"


def plain_english(summary: dict, dec: int = 2) -> str:
    """One short paragraph describing where price sits and the nearest levels."""
    cp = summary.get("current_price")
    if cp is None or not np.isfinite(cp):
        return ""
    parts = [f"Price is {cp:.{dec}f}."]
    cz = summary.get("current_zone")
    if cz:
        parts.append(f"It is inside a {cz['side']} zone ({cz['low']:.{dec}f}–{cz['high']:.{dec}f}).")
    ns, nr = summary.get("nearest_support"), summary.get("nearest_resistance")
    if ns:
        c = ns.get("confidence")
        parts.append(f"Nearest support {ns['center']:.{dec}f} ({_atr_phrase(ns)}{', '+c+' confidence' if c else ''}).")
    if nr:
        c = nr.get("confidence")
        parts.append(f"Nearest resistance {nr['center']:.{dec}f} ({_atr_phrase(nr)}{', '+c+' confidence' if c else ''}).")
    if ns and nr and ns.get("distance_atr") is not None and nr.get("distance_atr") is not None:
        ds, dr = ns["distance_atr"], nr["distance_atr"]
        if dr < ds * 0.7:
            parts.append("Price is closer to resistance — limited room up.")
        elif ds < dr * 0.7:
            parts.append("Price is closer to support — limited room down.")
        else:
            parts.append("Price sits roughly mid-range between support and resistance.")
    elif ns and not nr:
        parts.append("No resistance above within range — open upside.")
    elif nr and not ns:
        parts.append("No support below within range — open downside.")
    return " ".join(parts)


def _next_above(centers, c):
    a = [x for x in centers if x > c + 1e-9]
    return min(a) if a else None


def _next_below(centers, c):
    b = [x for x in centers if x < c - 1e-9]
    return max(b) if b else None


def if_then(summary: dict, dec: int = 2, sup_centers=None, res_centers=None) -> list:
    """Conditional map off the nearest levels: break/hold triggers and their consequence.
    ``sup_centers``/``res_centers`` are the full per-side center lists (so 'next level' is the
    genuine next by price); they fall back to the top-zone records when not provided."""
    out = []
    cp = summary.get("current_price")
    if cp is None or not np.isfinite(cp):
        return out
    if res_centers is None:
        res_centers = [z["zone_center"] for z in (summary.get("top_resistance_zones") or [])]
    if sup_centers is None:
        sup_centers = [z["zone_center"] for z in (summary.get("top_support_zones") or [])]
    ns, nr = summary.get("nearest_support"), summary.get("nearest_resistance")
    if nr:
        nxt = _next_above(res_centers, nr["center"])
        cons = f"next resistance is {nxt:.{dec}f}" if nxt is not None else "upside opens with little overhead resistance"
        out.append({"trigger": f"price closes above {nr['high']:.{dec}f}", "action": cons, "kind": "bull"})
    if ns:
        nxt = _next_below(sup_centers, ns["center"])
        cons = f"next support is {nxt:.{dec}f}" if nxt is not None else "downside opens with little support below"
        out.append({"trigger": f"price closes below {ns['low']:.{dec}f}", "action": cons, "kind": "bear"})
    cz = summary.get("current_zone")
    if cz:
        out.append({"trigger": f"price holds {cz['low']:.{dec}f}–{cz['high']:.{dec}f}",
                    "action": "that zone should act as " + ("a floor" if cz["side"] == "support" else "a ceiling"),
                    "kind": "neutral"})
    return out


def chart_payload(tf_data: dict, final_zones: pd.DataFrame, lookback: int = 300) -> dict:
    """Serialize candles + reference price/ATR for the desktop chart (TradingView
    Lightweight Charts renders client-side from these arrays). One candle list per
    present timeframe so the UI can switch timeframe without re-running the engine.

    ``time`` is a UTC epoch-seconds integer (datetime64 is UTC-based). S/R levels
    are NOT included here — the client derives nearest-N from the ``zones`` records
    so the 'levels per side' stepper re-filters with no server round-trip."""
    timeframes = _ordered_timeframes(tf_data)
    candles: dict[str, list] = {}
    current_price = None
    for tf in timeframes:
        d = tf_data[tf].tail(lookback)[["datetime", "open", "high", "low", "close"]].dropna()
        # epoch SECONDS, resolution-independent: pandas 3.0 datetimes are us, not ns,
        # so astype("int64")//1e9 would undershoot 1000x (every bar lands in 1970).
        t = d["datetime"].values.astype("datetime64[s]").astype("int64").tolist()
        o, h, lo, c = d["open"].tolist(), d["high"].tolist(), d["low"].tolist(), d["close"].tolist()
        candles[tf] = [{"time": int(t[i]), "open": float(o[i]), "high": float(h[i]),
                        "low": float(lo[i]), "close": float(c[i])} for i in range(len(t))]
        if c:
            current_price = float(c[-1])

    has_zones = final_zones is not None and not final_zones.empty
    if has_zones and "current_price" in final_zones.columns:
        current_price = float(final_zones["current_price"].iloc[0])
    current_atr = None
    if has_zones and "current_atr" in final_zones.columns and pd.notna(final_zones["current_atr"].iloc[0]):
        current_atr = float(final_zones["current_atr"].iloc[0])

    default_tf = "1D" if "1D" in timeframes else (timeframes[-1] if timeframes else None)
    return {"timeframes": timeframes, "candles": candles, "current_price": current_price,
            "current_atr": current_atr, "default_tf": default_tf}


def zones_to_records(final_zones: pd.DataFrame) -> list[dict]:
    if final_zones is None or final_zones.empty:
        return []
    cols = ["side", "zone_center", "zone_low", "zone_high", "score", "touches",
            "volume_at_level", "days_since_touch", "confidence",
            "confidence_score", "bucket", "timeframes", "timeframe_count",
            "distance_atr", "current_price", "last_touch"]
    df = final_zones[[c for c in cols if c in final_zones.columns]].copy()
    if "volume_at_level" in df.columns:
        df["volume_at_level"] = df["volume_at_level"].fillna(0.0)
    if "last_touch" in df.columns:
        df["last_touch"] = df["last_touch"].astype(str)
    return df.to_dict(orient="records")
