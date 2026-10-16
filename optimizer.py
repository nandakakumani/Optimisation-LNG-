from __future__ import annotations
import io, re, zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, date, timedelta
import numpy as np
from scipy.optimize import linprog
from scipy import sparse

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EPOCH = datetime(1899, 12, 30)

COLS = {
    "timestamp": 4, "gas_day": 5,
    "conversion": 21, "min_inventory": 22,
    "firm_storage": 23, "interruptible_storage": 24,
    "transitional_storage": 25, "temporary_storage": 26,
    "min_with_compression": 27, "min_without_compression": 28,
    "max_total_sendout": 29, "peg_capacity": 31, "ztp_capacity": 32,
    "peg_price": 33, "ztp_price": 34, "ebb_minimum": 37,
    "ebb_inventory": 38, "booked_cargo": 44, "booked_unloading": 45,
    "booked_in_tank": 46, "booked_peg": 47, "booked_ztp": 48,
    "booked_total": 49, "booked_inventory": 50,
    "optim_cargo": 53, "optim_unloading": 54, "optim_in_tank": 55,
    "optim_peg": 56, "optim_ztp": 57, "optim_total": 58,
    "optim_inventory": 59, "optim_variance": 60,
}
READ_COLS = set(COLS.values())

@dataclass
class ModelResult:
    rows: list[int]
    timestamps: list[datetime]
    peg: np.ndarray
    ztp: np.ndarray
    total: np.ndarray
    inventory: np.ndarray
    cargo: np.ndarray
    unloading: np.ndarray
    in_tank: np.ndarray
    variance: np.ndarray
    objective: float
    opening_inventory: float
    final_inventory: float
    message: str


def _colnum(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _sheet_path(z: zipfile.ZipFile, sheet_name: str) -> str:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rid = next(s.attrib[DOC_REL + "id"] for s in wb.find(NS + "sheets") if s.attrib["name"] == sheet_name)
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = next(r.attrib["Target"] for r in rels if r.attrib["Id"] == rid)
    return "xl/" + target.lstrip("/") if not target.startswith("xl/") else target


def _number(cell) -> float | None:
    v = cell.find(NS + "v")
    if v is None or v.text in (None, ""):
        return None
    try:
        return float(v.text)
    except ValueError:
        return None


def extract_hourly(workbook_bytes: bytes) -> tuple[list[dict], str]:
    records = []
    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as z:
        path = _sheet_path(z, "Hourly Profile")
        for _, row in ET.iterparse(z.open(path), events=("end",)):
            if row.tag != NS + "row":
                continue
            values = {}
            for cell in row.findall(NS + "c"):
                col = _colnum(cell.attrib["r"])
                if col in READ_COLS:
                    value = _number(cell)
                    if value is not None:
                        values[col] = value
            serial = values.get(COLS["timestamp"])
            if serial is not None and serial > 40000:
                records.append({"row": int(row.attrib["r"]), **{k: values.get(v) for k, v in COLS.items()}})
            row.clear()
    if not records:
        raise ValueError("No hourly records were found on the 'Hourly Profile' sheet.")
    return records, path


def available_dates(records: list[dict]) -> tuple[date, date, date, date]:
    dates = [(EPOCH + timedelta(days=r["timestamp"])).date() for r in records]
    priced = [d for d, r in zip(dates, records) if (r.get("peg_price") or 0) != 0 or (r.get("ztp_price") or 0) != 0]
    if not priced:
        priced = dates
    preferred_start = date(2026, 10, 16) if min(dates) <= date(2026, 10, 16) <= max(dates) else min(priced)
    preferred_end = min(date(2026, 10, 31), max(dates)) if preferred_start <= date(2026, 10, 31) else min(max(dates), preferred_start + timedelta(days=15))
    return min(dates), max(dates), preferred_start, preferred_end


def optimise(records: list[dict], start_date: date, end_date: date,
             opening_inventory: float | None = None,
             final_inventory_target: float | None = None,
             objective_mode: str = "Hub price",
             enforce_minimum_sendout: bool = False) -> ModelResult:
    selected_idx = [i for i, r in enumerate(records)
                    if start_date <= (EPOCH + timedelta(days=r["timestamp"])).date() <= end_date]
    if not selected_idx:
        raise ValueError("The selected date range contains no hourly rows.")
    selected = [records[i] for i in selected_idx]
    n = len(selected)

    def arr(name, default=0.0):
        return np.array([float(r.get(name) if r.get(name) is not None else default) for r in selected], dtype=float)

    peg_price, ztp_price = arr("peg_price"), arr("ztp_price")
    if objective_mode == "PEG/ZTP premium versus average hub":
        common = (peg_price + ztp_price) / 2.0
        peg_value, ztp_value = peg_price - common, ztp_price - common
    else:
        peg_value, ztp_value = peg_price, ztp_price

    conversion = np.where(arr("conversion", 6.79) > 0, arr("conversion", 6.79), 6.79)
    min_inv = np.maximum(arr("min_inventory"), 0)
    max_inv = arr("firm_storage") + arr("interruptible_storage") + arr("transitional_storage") + arr("temporary_storage")
    max_inv = np.where(max_inv >= min_inv, max_inv, min_inv)

    peg_cap = np.maximum(arr("peg_capacity"), 0)
    ztp_cap = np.maximum(arr("ztp_capacity"), 0)
    max_total = np.maximum(arr("max_total_sendout"), 0)
    max_total = np.minimum(max_total, peg_cap + ztp_cap)
    static_min = np.maximum(arr("min_with_compression"), 0)
    min_total = np.where((max_total > 0) & enforce_minimum_sendout, np.minimum(static_min, max_total), 0)

    cargo = arr("optim_cargo")
    unloading = arr("optim_unloading")
    in_tank = arr("optim_in_tank")
    booked_cargo, booked_unloading, booked_in_tank = arr("booked_cargo"), arr("booked_unloading"), arr("booked_in_tank")
    cargo = np.where(np.abs(cargo) > 1e-9, cargo, booked_cargo)
    unloading = np.where(np.abs(unloading) > 1e-9, unloading, booked_unloading)
    in_tank = np.where(np.abs(in_tank) > 1e-9, in_tank, booked_in_tank)
    inflow = unloading + in_tank

    first_global = selected_idx[0]
    if opening_inventory is None:
        candidates = []
        if first_global > 0:
            prev = records[first_global - 1]
            candidates.extend([prev.get("optim_inventory"), prev.get("booked_inventory"), prev.get("ebb_inventory")])
        candidates.extend([selected[0].get("ebb_inventory"), selected[0].get("booked_inventory")])
        opening_inventory = next((float(x) for x in candidates if x is not None and min_inv[0] <= float(x) <= max_inv[0]), float(min_inv[0]))

    if final_inventory_target is None:
        candidates = [selected[-1].get("booked_inventory"), selected[-1].get("optim_inventory"), opening_inventory]
        preferred_target = next((float(x) for x in candidates if x is not None and min_inv[-1] <= float(x) <= max_inv[-1]), float(opening_inventory))
        feasible_final_low = max(float(min_inv[-1]), float(opening_inventory + inflow.sum() - max_total.sum()))
        feasible_final_high = min(float(max_inv[-1]), float(opening_inventory + inflow.sum() - min_total.sum()))
        if feasible_final_low > feasible_final_high + 1e-6:
            raise ValueError("No feasible final inventory exists with the workbook inflows and sendout capacities.")
        final_inventory_target = min(max(preferred_target, feasible_final_low), feasible_final_high)

    # Variables: PEG[0:n], ZTP[n:2n], Inventory[2n:3n]
    c = -np.r_[peg_value, ztp_value, np.zeros(n)]
    bounds = ([(0.0, float(peg_cap[i])) for i in range(n)] +
              [(0.0, float(ztp_cap[i])) for i in range(n)] +
              [(float(min_inv[i]), float(max_inv[i])) for i in range(n)])
    bounds[-1] = (float(final_inventory_target), float(final_inventory_target))

    # Inventory recurrence: peg_t + ztp_t + inv_t - inv_(t-1) = inflow_t
    rows, cols, vals = [], [], []
    b_eq = np.array(inflow, dtype=float)
    b_eq[0] += float(opening_inventory)
    for t in range(n):
        rows += [t, t, t]
        cols += [t, n + t, 2 * n + t]
        vals += [1.0, 1.0, 1.0]
        if t > 0:
            rows.append(t); cols.append(2 * n + t - 1); vals.append(-1.0)
    A_eq = sparse.coo_matrix((vals, (rows, cols)), shape=(n, 3 * n)).tocsr()

    # Total sendout upper and lower bounds.
    eye = sparse.eye(n, format="csr")
    zeros = sparse.csr_matrix((n, n))
    total_matrix = sparse.hstack([eye, eye, zeros], format="csr")
    A_ub = sparse.vstack([total_matrix, -total_matrix], format="csr")
    b_ub = np.r_[max_total, -min_total]

    result = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                     bounds=bounds, method="highs")
    if not result.success:
        raise ValueError(
            "The model is infeasible for the selected period and targets. "
            f"Solver message: {result.message}. Try using the booked final inventory or a shorter period."
        )

    peg = result.x[:n]
    ztp = result.x[n:2*n]
    inventory = result.x[2*n:]
    total = peg + ztp
    booked_total = arr("booked_total")
    variance = total - booked_total
    return ModelResult(
        rows=[r["row"] for r in selected],
        timestamps=[EPOCH + timedelta(days=r["timestamp"]) for r in selected],
        peg=peg, ztp=ztp, total=total, inventory=inventory,
        cargo=cargo, unloading=unloading, in_tank=in_tank, variance=variance,
        objective=float(-result.fun), opening_inventory=float(opening_inventory),
        final_inventory=float(inventory[-1]), message=result.message,
    )


def _format_number(x: float) -> str:
    if abs(x) < 5e-10:
        return "0"
    return format(float(x), ".15g")


def write_output(original_bytes: bytes, sheet_path: str, result: ModelResult) -> bytes:
    values = {}
    for i, row in enumerate(result.rows):
        values.update({
            f"BA{row}": result.cargo[i], f"BB{row}": result.unloading[i], f"BC{row}": result.in_tank[i],
            f"BD{row}": result.peg[i], f"BE{row}": result.ztp[i], f"BF{row}": result.total[i],
            f"BG{row}": result.inventory[i], f"BH{row}": result.variance[i],
        })

    # Patch only existing OPTIM cells in the worksheet XML. This avoids rebuilding the
    # workbook and preserves the uploaded package, styles, drawings, links and formulas.
    refs = sorted(values, key=len, reverse=True)
    cell_pattern = re.compile(
        rb'<c\s+([^>]*?\br=")(' + b'|'.join(re.escape(x.encode()) for x in refs) + rb')("[^>]*)>(.*?)</c>',
        re.DOTALL,
    )
    def replace_cell(match):
        ref = match.group(2).decode()
        attrs_before, attrs_after = match.group(1), match.group(3)
        attrs_after = re.sub(rb'\s+t="[^"]*"', b'', attrs_after)
        return b'<c ' + attrs_before + match.group(2) + attrs_after + b'><v>' + _format_number(values[ref]).encode() + b'</v></c>'

    source = io.BytesIO(original_bytes)
    output = io.BytesIO()
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(output, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == sheet_path:
                data, count = cell_pattern.subn(replace_cell, data)
                remaining = [ref for ref in refs if (b'r="' + ref.encode() + b'"') in data and (b'<v>' + _format_number(values[ref]).encode() + b'</v>') not in data]
                if remaining:
                    empty_pattern = re.compile(rb'<c\s+([^>]*?\br=")(' + b'|'.join(re.escape(x.encode()) for x in remaining) + rb')("[^>]*)/?>')
                    def replace_empty(match):
                        ref = match.group(2).decode()
                        attrs_after = re.sub(rb'\s+t="[^"]*"', b'', match.group(3))
                        return b'<c ' + match.group(1) + match.group(2) + attrs_after.rstrip(b'/') + b'><v>' + _format_number(values[ref]).encode() + b'</v></c>'
                    data, extra = empty_pattern.subn(replace_empty, data)
                    count += extra
                if count != len(values):
                    raise ValueError(f"Could not update {len(values)-count} OPTIM cells because the workbook layout differs from the expected template.")
            zout.writestr(item, data)
    return output.getvalue()
