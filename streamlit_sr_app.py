"""Legacy Streamlit UI for the support/resistance engine.

This is kept as a fallback interface; the primary app is the desktop terminal
(``python run_desktop.py``). Both are thin UIs over the shared modules in
``sr/`` (engine, storage, charting), so behaviour is identical.

Run:  streamlit run streamlit_sr_app.py
"""
import pandas as pd
import streamlit as st

from sr import charting, engine, storage

st.set_page_config(page_title="Multi-Timeframe S/R Engine", layout="wide")
storage.ensure_dirs()

# ---------------------------------------------------------------------------
# Sidebar settings
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    st.subheader("Timeframes")
    use = {
        "15min": st.checkbox("Include 15m", value=True),
        "1h": st.checkbox("Include 1h", value=True),
        "4h": st.checkbox("Include 4h", value=True),
        "1D": st.checkbox("Include 1D", value=True),
        "1W": st.checkbox("Include 1W", value=False),
    }
    timeframes = [tf for tf, on in use.items() if on]

    atr_period = st.number_input("ATR Period", 5, 50, 14)
    atr_multiplier = st.slider("Zone width ATR multiplier", 0.05, 1.00, 0.25, 0.05)
    cluster_atr_multiplier = st.slider("Cluster ATR multiplier", 0.05, 1.50, 0.35, 0.05)
    min_score = st.slider("Minimum score", 0.0, 20.0, 3.0, 0.5)
    max_distance_atr = st.slider("Keep zones within X ATR from current", 1.0, 30.0, 10.0, 1.0)
    min_zone_width = st.number_input("Minimum zone width", value=0.005, step=0.001, format="%.4f")
    lookback = st.number_input("Chart lookback bars", 50, 2000, 300)

config = engine.SRConfig(
    timeframes=timeframes, atr_period=int(atr_period), atr_multiplier=atr_multiplier,
    cluster_atr_multiplier=cluster_atr_multiplier, min_score=min_score,
    max_distance_atr=max_distance_atr, min_zone_width=min_zone_width, lookback=int(lookback),
)

st.title("Interactive Multi-Timeframe Support & Resistance Engine")
st.caption("Upload daily Excel files, auto-deduplicate old rows, maintain master history, and calculate support/resistance zones.")

# ---------------------------------------------------------------------------
# 1) Upload
# ---------------------------------------------------------------------------
st.subheader("1) Upload Excel files")
uploaded_files = st.file_uploader(
    "First upload can be full history; later uploads can be just the previous day. Duplicate timestamps are removed automatically.",
    type=["xlsx", "xls"], accept_multiple_files=True,
)
col_a, col_b = st.columns([1, 1])
with col_a:
    force_instrument = st.text_input("Optional: override instrument name for all uploaded files", value="")
with col_b:
    sheet_name = st.text_input("Sheet name", value="Data")

if uploaded_files:
    if force_instrument.strip() and len(uploaded_files) > 1:
        st.warning("Override is set, so ALL uploaded files will be saved under the same instrument name. Clear it or edit names below.")
    st.write("Review / correct the instrument name for each file before processing:")
    preview = [{
        "file": uf.name,
        "instrument": force_instrument.strip() or engine.clean_instrument_name(uf.name),
    } for uf in uploaded_files]
    edited = st.data_editor(pd.DataFrame(preview), use_container_width=True, disabled=["file"], hide_index=True, key="instrument_map_editor")
    name_for_file = {str(r["file"]): str(r["instrument"]).strip() for _, r in edited.iterrows()}

    if st.button("Process uploads and update master data", type="primary"):
        stats_rows = []
        for uf in uploaded_files:
            instrument = name_for_file.get(uf.name, "") or engine.clean_instrument_name(uf.name)
            raw_path = storage.save_raw_upload(uf.name, uf.getbuffer())
            stats_rows.append(storage.ingest_excel(raw_path, instrument, sheet_name.strip() or "Data"))
        st.success("Upload processing complete. Master data updated.")
        total_dropped = sum(r.get("rows_dropped_bad", 0) for r in stats_rows)
        if total_dropped:
            st.warning(f"{total_dropped} row(s) dropped because their date or OHLC values could not be parsed.")
        st.dataframe(pd.DataFrame(stats_rows), use_container_width=True)

# ---------------------------------------------------------------------------
# 2) Master data
# ---------------------------------------------------------------------------
st.subheader("2) Master data")
instruments = storage.list_instruments()
if not instruments:
    st.info("No master data yet. Upload Excel files first.")
    st.stop()

selected = st.selectbox("Select instrument", instruments)
master_df = storage.load_master(selected)
actual_instrument = master_df["instrument"].iloc[0]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Instrument", actual_instrument)
c2.metric("Rows", f"{len(master_df):,}")
c3.metric("Start", str(master_df["datetime"].min()))
c4.metric("End", str(master_df["datetime"].max()))

with st.expander("View master data sample"):
    st.dataframe(master_df.tail(200), use_container_width=True)
    st.download_button("Download master CSV", master_df.to_csv(index=False).encode("utf-8"),
                       file_name=f"{actual_instrument}_master.csv", mime="text/csv")

# ---------------------------------------------------------------------------
# 3) Run S/R
# ---------------------------------------------------------------------------
st.subheader("3) Calculate support/resistance")
if st.button("Run S/R Engine", type="primary"):
    with st.spinner("Calculating zones..."):
        final_zones, raw_levels, timeframe_zones, tf_data, diagnostics = engine.compute_sr(master_df, config)
        st.session_state["sr_result"] = (final_zones, raw_levels, timeframe_zones, tf_data, diagnostics, actual_instrument)
    st.success("Done.")

if "sr_result" in st.session_state:
    final_zones, raw_levels, timeframe_zones, tf_data, diagnostics, inst = st.session_state["sr_result"]

    if diagnostics is not None and not diagnostics.empty:
        skipped_n = int((diagnostics["status"] == "skipped").sum())
        with st.expander(f"Timeframe diagnostics — used vs skipped ({skipped_n} skipped)"):
            st.caption("Timeframes finer than an instrument's native bar interval are dropped to avoid double-counting; sparse timeframes are also skipped.")
            st.dataframe(diagnostics, use_container_width=True)

    st.markdown("### Final zones")
    if final_zones.empty:
        st.warning("No zones found with current settings. Lower min score or increase max distance ATR.")
    else:
        side_filter = st.multiselect("Filter side", ["support", "resistance"], default=["support", "resistance"])
        filtered = final_zones[final_zones["side"].isin(side_filter)]
        st.dataframe(filtered, use_container_width=True)

        fig = charting.build_sr_figure(tf_data.get(inst, {}), filtered, inst, config.lookback)
        st.plotly_chart(fig, use_container_width=True)

        excel_path = storage.output_dir() / f"{inst}_support_resistance_output.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            final_zones.to_excel(writer, index=False, sheet_name="final_zones")
            raw_levels.to_excel(writer, index=False, sheet_name="raw_swing_levels")
            timeframe_zones.to_excel(writer, index=False, sheet_name="timeframe_zones")
            master_df.to_excel(writer, index=False, sheet_name="master_data")
        with open(excel_path, "rb") as f:
            st.download_button("Download S/R Excel output", f.read(), file_name=excel_path.name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------------------------------------------------------------------------
# 4) Data management
# ---------------------------------------------------------------------------
st.subheader("4) Data management")
with st.expander("Danger zone"):
    st.warning("This deletes stored master data for the selected instrument.")
    if st.button("Delete selected instrument master data"):
        storage.delete_master(selected)
        st.success("Deleted. Refresh the app.")
