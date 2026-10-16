# DNK LNG Streamlit Optimiser

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

Upload `Copy of DNK LNG MASTER - 15102026 try again gain v1.xlsx`, choose the optimisation dates, run the model and download the optimised workbook.

## Workbook mapping

The app reads `Hourly Profile` and writes only the existing OPTIM block:

- BA: SEFE Cargoes Arrival
- BB: Unloading Profile
- BC: In Tank
- BD: Sendout PEG
- BE: Sendout ZTP
- BF: Total Sendout
- BG: Inventory
- BH: Optim total sendout minus booked total sendout

The workbook is updated at OOXML cell-value level, avoiding a full workbook rebuild.
