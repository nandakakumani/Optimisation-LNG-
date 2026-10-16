import io
import pandas as pd
import streamlit as st
from optimizer import extract_hourly, available_dates, optimise, write_output

st.set_page_config(page_title="DNK LNG Optimiser", page_icon="📈", layout="wide")
st.title("DNK LNG Sendout Optimiser")
st.caption("Upload the standard DNK LNG master workbook. The downloaded workbook keeps the original workbook structure and formatting, and only fills the Hourly Profile OPTIM section BA:BH for the selected period.")

uploaded = st.file_uploader("Upload DNK LNG master workbook", type=["xlsx"], max_upload_size=250)
if uploaded is None:
    st.info("Upload the exact DNK LNG master .xlsx file to begin.")
    st.stop()

raw = uploaded.getvalue()
try:
    records, sheet_path = extract_hourly(raw)
    min_date, max_date, default_start, default_end = available_dates(records)
except Exception as exc:
    st.error(f"Workbook validation failed: {exc}")
    st.stop()

with st.sidebar:
    st.header("Optimisation settings")
    start_date = st.date_input("Start date", value=default_start, min_value=min_date, max_value=max_date)
    end_date = st.date_input("End date", value=default_end, min_value=min_date, max_value=max_date)
    objective_mode = st.selectbox("Objective", ["Hub price", "PEG/ZTP premium versus average hub"], index=0)
    enforce_minimum = st.checkbox("Enforce hourly minimum sendout", value=False, help="Enable only when the displayed technical minimum applies throughout the selected period.")
    use_auto_opening = st.checkbox("Use workbook opening inventory", value=True)
    opening = None if use_auto_opening else st.number_input("Opening inventory (MWh)", min_value=0.0, value=100000.0, step=1000.0)
    use_auto_final = st.checkbox("Use workbook booked final inventory", value=True)
    final_target = None if use_auto_final else st.number_input("Final inventory target (MWh)", min_value=0.0, value=100000.0, step=1000.0)

if start_date > end_date:
    st.error("Start date must be on or before end date.")
    st.stop()

selected_hours = sum(start_date <= (pd.Timestamp("1899-12-30") + pd.to_timedelta(r["timestamp"], unit="D")).date() <= end_date for r in records)
col1, col2, col3 = st.columns(3)
col1.metric("Selected hours", f"{selected_hours:,}")
col2.metric("Decision variables", f"{selected_hours * 2:,}")
col3.metric("Workbook rows detected", f"{len(records):,}")

if st.button("Run optimisation", type="primary", use_container_width=True):
    try:
        with st.spinner("Solving the hourly PEG/ZTP and inventory model..."):
            result = optimise(records, start_date, end_date, opening, final_target, objective_mode, enforce_minimum)
            output = write_output(raw, sheet_path, result)
        st.session_state["output"] = output
        st.session_state["result"] = result
        st.success("Optimisation completed. The output workbook is ready.")
    except Exception as exc:
        st.error(str(exc))

if "result" in st.session_state:
    result = st.session_state["result"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Objective value", f"{result.objective:,.0f}")
    c2.metric("Opening inventory", f"{result.opening_inventory:,.0f} MWh")
    c3.metric("Final inventory", f"{result.final_inventory:,.0f} MWh")
    c4.metric("Total sendout", f"{result.total.sum():,.0f} MWh")
    preview = pd.DataFrame({
        "Timestamp": result.timestamps,
        "Optim PEG (MWh)": result.peg,
        "Optim ZTP (MWh)": result.ztp,
        "Optim total (MWh)": result.total,
        "Optim inventory (MWh)": result.inventory,
        "Optim vs booked (MWh)": result.variance,
    })
    st.line_chart(preview.set_index("Timestamp")[["Optim PEG (MWh)", "Optim ZTP (MWh)"]])
    st.dataframe(preview.head(168), use_container_width=True, hide_index=True)
    stem = uploaded.name.rsplit(".", 1)[0]
    st.download_button(
        "Download optimised workbook",
        data=st.session_state["output"],
        file_name=f"{stem} - OPTIMISED.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )

with st.expander("Model logic"):
    st.markdown("""
- Decision variables: hourly **PEG sendout** and **ZTP sendout**.
- Objective: maximise the selected hourly PEG and ZTP price value.
- Inventory balance: previous inventory + unloading + in-tank volume - PEG - ZTP.
- Constraints: PEG capacity, ZTP capacity, total sendout minimum/maximum, minimum inventory, maximum available storage and final inventory target.
- Output: values are written only to `Hourly Profile` columns `BA:BH`. Other sheets, formatting and workbook package content are copied from the uploaded workbook.
""")
