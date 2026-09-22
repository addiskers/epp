"""Contact list import for outbound campaigns.

- `normalize_phone` — India-centric E.164 normaliser that rejects the classic Excel
  scientific-notation corruption (e.g. `9.17619E+11`).
- `parse_upload` — read an .xlsx (openpyxl) or .csv into (name, e164, status, extra)
  rows, de-duplicated by phone within the batch. `extra` always carries `caller_type`
  and `notes` ('' when the sheet had no such column).
- `build_template` — a real .xlsx sample with a Text-formatted Phone column so users
  don't re-introduce scientific notation.
"""

import csv
import io
import re

from openpyxl import Workbook, load_workbook

import routing

_MAX_ROWS = 100_000
_NAME_HEADERS = {"name", "full name", "contact name", "contact", "person", "employee name",
                 "customer name", "vendor name"}
_PHONE_HINTS = ("phone", "mobile", "number", "contact no", "whatsapp", "cell", "msisdn")
_EXTRA_HINTS = {
    "caller_type": ("caller type", "type", "category", "who"),
    "notes": ("note", "remark", "comment", "department", "plant", "company", "location"),
}


def normalize_phone(raw):
    """Return (e164_or_None, is_valid). None => unparseable / rejected."""
    if raw is None:
        return None, False
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)                               # a numeric spreadsheet cell: 9876543210.0
    s = str(raw).strip()
    if not s:
        return None, False
    # A whole-valued float string is the same numeric cell after a text export.
    m = re.fullmatch(r"(\d+)\.0+", s)
    if m:
        s = m.group(1)
    # reject scientific-notation corruption ("9.17619E+11") and fractional junk ("98765.43")
    if re.search(r"[eE][+\-]?\d", s) or re.fullmatch(r"\d+\.\d+", s):
        return None, False
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None, False
    if plus:
        e164 = "+" + digits
    elif len(digits) == 10:
        e164 = "+91" + digits                       # bare Indian mobile
    elif len(digits) == 12 and digits.startswith("91"):
        e164 = "+" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        e164 = "+91" + digits[1:]                    # leading-0 domestic form
    else:
        e164 = "+" + digits
    ndig = len(e164) - 1
    return e164, (10 <= ndig <= 15)


def _pick_columns(header):
    """(name_idx, phone_idx, {extra_field: idx}, [unrecognised headers])."""
    name_idx = phone_idx = None
    extra_idx, unknown = {}, []
    for i, cell in enumerate(header):
        c = str(cell or "").strip().lower()
        if not c:
            continue
        if name_idx is None and c in _NAME_HEADERS:
            name_idx = i
            continue
        if phone_idx is None and any(k in c for k in _PHONE_HINTS):
            phone_idx = i
            continue
        matched = None
        for field, hints in _EXTRA_HINTS.items():
            if field in extra_idx:
                continue
            if any(h == c or h in c for h in hints):
                matched = field
                break
        if matched:
            extra_idx[matched] = i
        else:
            unknown.append(str(cell).strip())
    return name_idx, phone_idx, extra_idx, unknown


def _rows_from_matrix(matrix):
    """matrix: list of row-tuples. Returns ([(name_raw, phone_raw, extra)], unknown_headers)."""
    matrix = [r for r in matrix if r is not None and any(c not in (None, "") for c in r)]
    if not matrix:
        return [], []
    name_idx, phone_idx, extra_idx, unknown = _pick_columns(matrix[0])
    if phone_idx is not None:
        body = matrix[1:]                            # first row was a header
    else:
        ncol = max(len(r) for r in matrix)
        name_idx, phone_idx = (0, 1) if ncol >= 2 else (None, 0)
        extra_idx, unknown = {}, []
        body = matrix
    out = []
    for r in body:
        ph = r[phone_idx] if phone_idx is not None and phone_idx < len(r) else None
        nm = r[name_idx] if name_idx is not None and name_idx < len(r) else None
        if ph in (None, "") and nm in (None, ""):
            continue
        extra = {"caller_type": "", "notes": ""}
        for field, idx in extra_idx.items():
            val = r[idx] if idx < len(r) else None
            if val in (None, ""):
                continue
            extra[field] = (routing.normalize_caller_type(val) if field == "caller_type"
                            else str(val).strip())
        out.append((nm, ph, extra))
    return out, unknown


def _parse_xlsx(data):
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    matrix = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= _MAX_ROWS:
            break
        matrix.append(row)
    wb.close()
    return _rows_from_matrix(matrix)


def _parse_csv(data):
    text = data.decode("utf-8-sig", errors="replace")
    matrix = [tuple(r) for r in csv.reader(io.StringIO(text))]
    return _rows_from_matrix(matrix[:_MAX_ROWS])


def parse_upload(filename, data):
    """Return (rows, rejected, total, unknown_headers).

    rows: list of (name, e164, status, extra) de-duplicated by phone — a later row's name
    and extras fill blanks in an earlier one, never overwrite. unknown_headers is reported
    back so a mis-named column is visible rather than silently ignored."""
    name = (filename or "").lower()
    raw_rows, unknown = _parse_csv(data) if name.endswith(".csv") else _parse_xlsx(data)
    seen = {}                                        # e164 -> (name, status, extra)
    rejected = 0
    for nm, ph, extra in raw_rows:
        e164, valid = normalize_phone(ph)
        if not e164:
            rejected += 1
            continue
        nm = (str(nm).strip() if nm not in (None, "") else "")
        status = "valid" if valid else "invalid"
        prev = seen.get(e164)
        keep_name = nm or (prev[0] if prev else "")
        keep_status = "valid" if (status == "valid" or (prev and prev[1] == "valid")) else "invalid"
        merged = dict(prev[2]) if prev else {"caller_type": "", "notes": ""}
        for k, v in extra.items():
            if v:
                merged[k] = v
        seen[e164] = (keep_name, keep_status, merged)
    rows = [(nm, ph, st, x) for ph, (nm, st, x) in seen.items()]
    return rows, rejected, len(raw_rows), unknown


def build_template():
    """A minimal .xlsx sample: headers Name/Phone/Type/Notes, phone column Text-formatted."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Contacts"
    headers = ["Name", "Phone", "Type", "Notes"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h)
    samples = [
        ("Rahul Verma", "9876543210", "Employee", "Halol plant"),
        ("Meera Shah", "+919812345678", "Customer", "Acme Pipes"),
        ("Jay Patel", "9000000003", "Vendor", "V-0077"),
    ]
    for r, row in enumerate(samples, start=2):
        for i, val in enumerate(row, start=1):
            c = ws.cell(row=r, column=i, value=val)
            if i == 2:
                c.number_format = "@"                # Text — preserves leading digits
    for col, width in zip("ABCD", (24, 18, 12, 28)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
