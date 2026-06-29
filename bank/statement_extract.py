"""Tier 4 statement extraction (T4-01): bank statement → canonical bank_transactions.

Two layers:

1. `normalize_register` — the deterministic, tested core. Takes already-extracted
   register rows (a DataFrame from a CSV/Excel online-banking export, or the PDF
   adapter below) and maps them to the `bank_transactions` model: signed amounts
   (negative = money out), a hashed account fingerprint (never the raw number),
   parsed dates, and normalized check numbers. It validates before returning.
   `extract_export` is the thin file wrapper around it (CSV/Excel — the common
   path, since most banks export the register directly).

2. `extract_pdf` — a format-specific adapter that pulls register rows out of a PDF
   statement with pdfplumber (an optional dependency, imported lazily like the
   Anthropic/Supabase adapters) and feeds them to layer 1. Statement layouts vary,
   so this dispatches to a per-bank `layout` parser (`PDF_LAYOUTS`); with no layout
   it falls back to a best-effort ruled-table read. First Service Bank — the pilot
   bank, which only offers PDF statements — is parsed positionally
   (`parse_first_service_words`): no ruled lines, three sections (credits, debits,
   a 3-up checks grid), superscripted break markers, and the occasional split
   amount glyph ('7 91. 15' → 791.15). That parser is pure (operates on extracted
   word boxes), so it is unit-tested against synthetic word layouts, never a real
   statement.

Hard rules (CLAUDE.md): the raw account number is hashed on the way in and never
stored; check/statement images stay in SharePoint — only `image_ref` paths here.
Amounts handle accounting formats: '$1,234.56', '(1,234.56)' (parenthesis-
negative), and debit/credit column pairs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from bank.model import validate_bank_transactions
from core.fingerprint import account_fingerprint

# Canonical field -> default source-column name. Override per bank via `columns`;
# for separate withdrawal/deposit columns, pass {"debit": "...", "credit": "..."}
# (and drop "amount").
DEFAULT_COLUMNS = {
    "date": "date",
    "description": "description",
    "amount": "amount",      # signed; OR set "debit"/"credit" instead
    "check_no": "check_no",
}


def _norm_check(value) -> str:
    """Statement check numbers arrive as text, ints, or floats ('1001.0' from a
    CSV). Normalize to a clean string; blanks/NaN → ''."""
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _to_amount(series: pd.Series) -> pd.Series:
    """Parse a money column to float, tolerating '$', thousands commas, and
    accounting-style '(123.45)' negatives. Unparseable cells → NaN."""
    s = series.astype("string").str.strip()
    neg = s.str.startswith("(") & s.str.endswith(")")
    s = s.str.replace(r"[(),$\s]", "", regex=True)
    out = pd.to_numeric(s, errors="coerce")
    return out.where(~neg.fillna(False), -out)


def _signed_amount(rows: pd.DataFrame, columns: dict) -> pd.Series:
    """Resolve the signed amount (negative = disbursement) from either a single
    signed `amount` column or a `debit`/`credit` pair (positive magnitudes;
    signed = credit − debit)."""
    amount_col, debit_col, credit_col = (columns.get("amount"),
                                         columns.get("debit"), columns.get("credit"))
    present = [c for c in (amount_col, debit_col, credit_col) if c and c in rows.columns]
    if not present:
        raise ValueError(
            f"register has no amount column — expected one of {amount_col!r} "
            f"(signed) or {debit_col!r}/{credit_col!r}; got {list(rows.columns)}")
    if amount_col and amount_col in rows.columns:
        return _to_amount(rows[amount_col])
    zero = pd.Series(0.0, index=rows.index)
    credit = (_to_amount(rows[credit_col]).abs().fillna(0.0)
              if credit_col and credit_col in rows.columns else zero)
    debit = (_to_amount(rows[debit_col]).abs().fillna(0.0)
             if debit_col and debit_col in rows.columns else zero)
    return credit - debit


def normalize_register(
    rows: pd.DataFrame,
    *,
    entity_id: str,
    account_number: str,
    known_entity_ids: set[str],
    columns: dict | None = None,
    salt: str | None = None,
    image_ref: str | None = None,
) -> pd.DataFrame:
    """Map one entity + one account's raw register rows to canonical
    bank_transactions. `account_number` is hashed immediately and never stored;
    rows with no usable date or a zero/blank amount (subtotals, opening balances,
    memo lines) are dropped."""
    if entity_id not in known_entity_ids:
        raise ValueError(
            f"Unknown entity_id '{entity_id}' — add it to config/entities.yaml first")
    columns = {**DEFAULT_COLUMNS, **(columns or {})}

    out = pd.DataFrame(index=rows.index)
    out["entity_id"] = entity_id
    out["account_fingerprint"] = account_fingerprint(account_number, salt=salt)
    out["date"] = pd.to_datetime(rows[columns["date"]], errors="coerce")
    out["description"] = (rows[columns["description"]].astype("string").str.strip()
                          if columns["description"] in rows.columns else pd.NA)
    out["amount"] = _signed_amount(rows, columns)
    out["check_no"] = (rows[columns["check_no"]].map(_norm_check)
                       if columns["check_no"] in rows.columns else "")
    # Vision-read fields are populated by the later check-image slice (T4-03/04/05).
    out["payee_read"] = pd.NA
    out["amount_read"] = pd.NA
    out["read_confidence"] = pd.NA
    out["image_ref"] = image_ref

    out = out[out["date"].notna() & out["amount"].notna() & (out["amount"] != 0)]
    out = out.reset_index(drop=True)
    return validate_bank_transactions(out, known_entity_ids)


def extract_export(
    path: str | Path,
    *,
    entity_id: str,
    account_number: str,
    known_entity_ids: set[str],
    columns: dict | None = None,
    salt: str | None = None,
    image_ref: str | None = None,
    sheet: int | str = 0,
) -> pd.DataFrame:
    """Extract a CSV or Excel online-banking register export. The common path —
    most banks export the register directly, no PDF parsing needed. Cells are read
    as text so check numbers and accounting-formatted amounts survive intact."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        raw = pd.read_excel(path, sheet_name=sheet, dtype=object)
    else:
        raw = pd.read_csv(path, dtype=object)
    return normalize_register(
        raw, entity_id=entity_id, account_number=account_number,
        known_entity_ids=known_entity_ids, columns=columns, salt=salt,
        image_ref=image_ref or str(path))


def _slug(header) -> str:
    return str(header or "").strip().lower().replace(" ", "_")


def _read_pdf_tables(path: str | Path, *, pages=None) -> pd.DataFrame:
    """Pull every table on the requested pages into one DataFrame (best effort).
    pdfplumber is an optional dependency, imported lazily."""
    try:
        import pdfplumber  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "extract_pdf needs pdfplumber — `pip install pdfplumber`, or pre-extract "
            "the register to a DataFrame and call normalize_register directly.") from exc
    frames: list[pd.DataFrame] = []
    with pdfplumber.open(path) as pdf:
        chosen = pdf.pages if pages is None else [pdf.pages[i] for i in pages]
        for page in chosen:
            for table in page.extract_tables():
                if table and len(table) > 1:
                    header, *body = table
                    frames.append(pd.DataFrame(body, columns=[_slug(h) for h in header]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def extract_pdf(
    path: str | Path,
    *,
    entity_id: str,
    account_number: str,
    known_entity_ids: set[str],
    columns: dict | None = None,
    salt: str | None = None,
    image_ref: str | None = None,
    pages=None,
    layout: str | None = None,
) -> pd.DataFrame:
    """Extract a PDF statement's register into canonical bank_transactions.

    `layout` selects a per-bank positional parser from `PDF_LAYOUTS` (e.g.
    'first_service_bank'); these produce canonical date/description/amount/check_no
    columns directly, so `columns` is ignored for them. With no layout, fall back to
    a best-effort ruled-table read — advisory only; map the bank's headers via
    `columns` and review before trusting. The deterministic normalization (signing,
    hashing, validation) is always `normalize_register`."""
    if layout:
        try:
            parser = PDF_LAYOUTS[layout]
        except KeyError:
            raise ValueError(
                f"Unknown PDF statement layout {layout!r} — known layouts: "
                f"{sorted(PDF_LAYOUTS)}")
        raw = parser(path)
        columns = None      # the layout parser already emits canonical columns
    else:
        raw = _read_pdf_tables(path, pages=pages)
    return normalize_register(
        raw, entity_id=entity_id, account_number=account_number,
        known_entity_ids=known_entity_ids, columns=columns, salt=salt,
        image_ref=image_ref or str(path))


# --------------------------------------------------------------------------- #
# First Service Bank — positional PDF parser (pilot bank; PDF-only statements)
# --------------------------------------------------------------------------- #
# First Service Bank statements have no ruled table lines, so pdfplumber's
# extract_tables() finds nothing. We parse positionally from word boxes instead.
# The layout, per page, is three stacked sections — "Deposits and Other Credits",
# "Other Debits and Withdrawals", then a 3-column "Checks" grid — ending at the
# "Daily Balance Summary". Column geometry (points from the left edge):
_FSB_DATE_MAX_X0 = 90      # the M/D date sits at x0 ~50
_FSB_AMT_MAX_X0 = 170      # amount fragments fall in [90,170); description begins
                           #   at ~172 (credits) / ~188 (debits)
_FSB_GRID_SPLITS = (230, 410)   # checks grid: three column groups, split by x0
# Within each checks-grid column, the amount sub-column begins at this x0. Tokens
# left of it are the check number, right of it the amount — so a check number split
# across word fragments ('7' '568' → 7568) and a split amount ('7' '91.' '15' →
# 791.15) are each reassembled from their own band instead of by token order.
_FSB_CHECK_AMT_X0 = (140, 322, 505)
_FSB_LINE_GAP = 4          # rows are ~10pt apart; a superscripted break-marker '*'
                           #   sits ~0.25pt off its row. Cluster words by vertical
                           #   gap (4 > intra-row jitter, < inter-row spacing) so a
                           #   '*' never splits its check onto a phantom line.

_FSB_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}$")
_FSB_AMT_RE = re.compile(r"^[\d,]*\.\d{2}$")     # allows sub-dollar '.25' (no lead digit)
_FSB_STMT_DATE_RE = re.compile(r"Statement Date:\s*(\d{1,2})-(\d{1,2})-(\d{2})")
# Printed summary totals, used to self-check the parse: "Deposits / Misc Credits
# 37 2,661,881.17" and "Withdrawals / Misc Debits 196 2,661,888.02".
_FSB_SUMMARY_CREDITS = re.compile(r"Deposits\s*/?\s*Misc\s*Credits\s+(\d+)\s+([\d,]+\.\d{2})", re.I)
_FSB_SUMMARY_DEBITS = re.compile(r"Withdrawals\s*/?\s*Misc\s*Debits\s+(\d+)\s+([\d,]+\.\d{2})", re.I)


def _fsb_cluster_rows(words: list[dict]) -> list[list[dict]]:
    """Group a page's word boxes into visual rows by vertical proximity, then sort
    each row left-to-right. Gap clustering (not round(top/tol)) so a superscripted
    glyph a fraction of a point off the baseline stays on its own row."""
    rows: list[list[dict]] = []
    cur: list[dict] = []
    base = None
    for w in sorted(words, key=lambda d: d["top"]):
        if base is None or w["top"] - base <= _FSB_LINE_GAP:
            cur.append(w)
            base = cur[0]["top"]
        else:
            rows.append(sorted(cur, key=lambda d: d["x0"]))
            cur, base = [w], w["top"]
    if cur:
        rows.append(sorted(cur, key=lambda d: d["x0"]))
    return rows


def _fsb_amount(frags: list[dict]) -> str | None:
    """Reconstruct an amount from its column fragments, dropping the stray internal
    spaces the PDF sometimes inserts ('7','91.','15' → '791.15'). None if the result
    is not a money value."""
    text = "".join(f["text"] for f in frags).replace(" ", "")
    return text if _FSB_AMT_RE.match(text) else None


def _fsb_year_for(month: int, stmt_month: int, stmt_year: int) -> int:
    """A statement spans at most ~a month, so a transaction month later than the
    statement month belongs to the prior year (Dec lines on a January statement)."""
    return stmt_year - 1 if month > stmt_month else stmt_year


def parse_first_service_words(pages: list[list[dict]]) -> pd.DataFrame:
    """Parse First Service Bank statement word boxes into register rows.

    `pages` is one list of word dicts (keys: text, x0, x1, top) per page, in order —
    exactly what pdfplumber's `extract_words` returns. Pure and deterministic, so it
    is unit-tested against synthetic layouts. Returns a DataFrame with canonical
    `date` (ISO), `description`, `amount` (signed: credits +, debits/checks −) and
    `check_no`; `normalize_register` does the hashing, validation and noise drop."""
    stmt_month = stmt_year = None
    records: list[dict] = []
    section = None
    for words in pages:
        for row in _fsb_cluster_rows(words):
            text = " ".join(w["text"] for w in row)
            if stmt_month is None:
                m = _FSB_STMT_DATE_RE.search(text)
                if m:
                    stmt_month, _, yy = (int(g) for g in m.groups())
                    stmt_year = 2000 + yy
            # Reject the older pre-2025-05 First Service layout (a single "Credits
            # and Miscellaneous Debits" section, no embedded check images) — its
            # geometry differs and is not yet supported. Raising (rather than
            # returning zero rows) means extract_account logs the file as skipped,
            # so legacy statements never silently vanish from the run.
            if "Credits and Miscellaneous Debits" in text:
                raise ValueError(
                    "First Service legacy statement layout (pre-2025-05, 'Credits and "
                    "Miscellaneous Debits') is not yet supported by the "
                    "first_service_bank parser")
            # Section transitions. The checks grid and the daily-balance table both
            # look like dated rows, so the markers are what keep them apart.
            if "Deposits and Other Credits" in text:
                section = "credit"; continue
            if "Other Debits and Withdrawals" in text:
                section = "debit"; continue
            if text.strip() == "Checks":
                section = "checks"; continue
            if "Daily Balance Summary" in text:
                return _fsb_frame(records)         # statement body ends here
            if section in ("credit", "debit"):
                rec = _fsb_parse_txn_row(row, section, stmt_month, stmt_year)
                if rec:
                    records.append(rec)
            elif section == "checks":
                records.extend(_fsb_parse_check_row(row, stmt_month, stmt_year))
    return _fsb_frame(records)


def _fsb_iso(date_md: str, stmt_month, stmt_year) -> str:
    month, day = (int(p) for p in date_md.split("/"))
    if not stmt_month:                  # no statement date seen — leave year to pandas
        return f"{month:02d}/{day:02d}"
    return f"{_fsb_year_for(month, stmt_month, stmt_year):04d}-{month:02d}-{day:02d}"


def _fsb_parse_txn_row(row, section, stmt_month, stmt_year) -> dict | None:
    """A credit/debit line: M/D in the date band, amount in the amount band,
    description to the right. Continuation lines (no date+amount) return None."""
    date = next((w for w in row
                 if w["x0"] < _FSB_DATE_MAX_X0 and _FSB_DATE_RE.match(w["text"])), None)
    amt = _fsb_amount([w for w in row if _FSB_DATE_MAX_X0 <= w["x0"] < _FSB_AMT_MAX_X0])
    if not date or not amt:
        return None
    value = float(amt.replace(",", ""))
    desc = " ".join(w["text"] for w in row if w["x0"] >= _FSB_AMT_MAX_X0)
    return {
        "date": _fsb_iso(date["text"], stmt_month, stmt_year),
        "description": desc,
        "amount": value if section == "credit" else -value,   # debit = money out
        "check_no": "",
    }


def _fsb_parse_check_row(row, stmt_month, stmt_year) -> list[dict]:
    """A checks-grid line carries up to three (date, check no, amount) triples, one
    per column group. Within a group the check number and amount are split by their
    own x-sub-column (not token order), because either can arrive as spaced word
    fragments ('7' '568' → check 7568; '7' '91.' '15' → $791.15). Checks are
    disbursements → negative amounts."""
    groups: list[list[dict]] = [[], [], []]
    for w in row:
        g = 0 if w["x0"] < _FSB_GRID_SPLITS[0] else (1 if w["x0"] < _FSB_GRID_SPLITS[1] else 2)
        groups[g].append(w)
    out: list[dict] = []
    for gi, grp in enumerate(groups):
        if len(grp) < 3:
            continue
        date = grp[0]
        if not _FSB_DATE_RE.match(date["text"]):
            continue
        amt_x0 = _FSB_CHECK_AMT_X0[gi]
        number = "".join(w["text"] for w in grp[1:] if w["x0"] < amt_x0).replace(" ", "").rstrip("*")
        amt = _fsb_amount([w for w in grp[1:] if w["x0"] >= amt_x0])
        if not number.isdigit() or not amt:
            continue
        out.append({
            "date": _fsb_iso(date["text"], stmt_month, stmt_year),
            "description": f"Check {number}",
            "amount": -float(amt.replace(",", "")),
            "check_no": number,
        })
    return out


def _fsb_frame(records: list[dict]) -> pd.DataFrame:
    cols = ["date", "description", "amount", "check_no"]
    return pd.DataFrame(records, columns=cols)


def first_service_summary_totals(pages: list[list[dict]]) -> dict:
    """The statement's printed control totals, for self-checking the parse:
    {credit_count, credit_total, debit_count, debit_total} (missing keys if a line
    isn't found). Pure — operates on word boxes."""
    out: dict = {}
    for words in pages:
        for row in _fsb_cluster_rows(words):
            text = " ".join(w["text"] for w in row)
            mc = _FSB_SUMMARY_CREDITS.search(text)
            if mc and "credit_total" not in out:
                out["credit_count"] = int(mc.group(1))
                out["credit_total"] = float(mc.group(2).replace(",", ""))
            md = _FSB_SUMMARY_DEBITS.search(text)
            if md and "debit_total" not in out:
                out["debit_count"] = int(md.group(1))
                out["debit_total"] = float(md.group(2).replace(",", ""))
        if "credit_total" in out and "debit_total" in out:
            break
    return out


def first_service_self_check(frame: pd.DataFrame, expected: dict, tol: float = 0.01) -> list[str]:
    """Compare parsed credit/debit sums to the statement's printed totals; return a
    list of human-readable mismatch messages (empty when it reconciles). Credits are
    positive rows, debits negative."""
    msgs: list[str] = []
    credits, debits = frame[frame["amount"] > 0], frame[frame["amount"] < 0]
    got_credit, got_debit = round(credits["amount"].sum(), 2), round(-debits["amount"].sum(), 2)
    if expected.get("credit_total") is not None and abs(got_credit - expected["credit_total"]) > tol:
        msgs.append(f"credits parsed {got_credit:,.2f} ({len(credits)}) vs printed "
                    f"{expected['credit_total']:,.2f} ({expected.get('credit_count')})")
    if expected.get("debit_total") is not None and abs(got_debit - expected["debit_total"]) > tol:
        msgs.append(f"debits parsed {got_debit:,.2f} ({len(debits)}) vs printed "
                    f"{expected['debit_total']:,.2f} ({expected.get('debit_count')})")
    return msgs


def _read_first_service_pdf(path: str | Path) -> pd.DataFrame:
    """Open a First Service Bank PDF and feed its word boxes to the pure parser.
    Logs a warning if the parsed sums don't reconcile to the statement's printed
    control totals — a loud signal that a statement parsed incompletely."""
    try:
        import pdfplumber  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "First Service Bank PDF parsing needs pdfplumber — `pip install "
            "pdfplumber`.") from exc
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_words(x_tolerance=1, keep_blank_chars=False)
                 for page in pdf.pages]
    frame = parse_first_service_words(pages)
    mismatches = first_service_self_check(frame, first_service_summary_totals(pages))
    if mismatches:
        print(f"  ! Statement parse self-check failed for {Path(path).name} — "
              "did not reconcile to printed totals: " + "; ".join(mismatches))
    return frame


# Per-bank PDF layout parsers: path -> DataFrame[date, description, amount, check_no].
PDF_LAYOUTS = {
    "first_service_bank": _read_first_service_pdf,
}


# --------------------------------------------------------------------------- #
# First Service Bank — embedded cancelled-check images (Tier 4 T4-03/04/05)
# --------------------------------------------------------------------------- #
# The statement's image pages lay out cancelled checks in a 2-column grid, each
# cell captioned `[date] check_no $amount` (the date prefix is sometimes absent).
# Each check face is a vector+raster composite, not one clean embedded image, so we
# anchor on the caption and RENDER the grid cell above it to a JPEG — far more
# reliable than pulling the XObjects. The anchor logic is pure (operates on word
# boxes) and unit-tested; only the render step needs the PDF.
_FSB_IMG_CHECKNO_RE = re.compile(r"^\d{3,6}$")
_FSB_IMG_AMT_RE = re.compile(r"^\$[\d,]+\.\d{2}$")
_FSB_IMG_COL_SPLIT = 310            # check-no x0 below this → left column, else right
_FSB_IMG_COL_LEFT = (70, 333)       # (x0, x1) crop band for the left column
_FSB_IMG_COL_RIGHT = (333, 602)
_FSB_IMG_CELL_TOP = -102            # cell spans from caption_top-102 (the face) …
_FSB_IMG_CELL_BOT = 9              # … to caption_top+9 (just past the caption)
_FSB_IMG_RES = 200                  # render DPI


def first_service_check_anchors(words: list[dict]) -> list[tuple[str, float, float]]:
    """(check_no, x0, top) for each `check_no $amount` caption on one image page.

    Pure: `words` are pdfplumber word boxes for the page. The check number is the
    token immediately left of a `$amount` token; its x0 selects the grid column and
    its top anchors the cell. Captions with and without a leading date both match."""
    out: list[tuple[str, float, float]] = []
    for row in _fsb_cluster_rows(words):
        for b, c in zip(row, row[1:]):
            if _FSB_IMG_CHECKNO_RE.match(b["text"]) and _FSB_IMG_AMT_RE.match(c["text"]):
                out.append((b["text"], b["x0"], b["top"]))
    return out


def _render_first_service_check_images(path: str | Path) -> dict[str, bytes]:
    """Render each cancelled-check cell of a First Service Bank PDF to a JPEG,
    keyed by check number. pdfplumber + its renderer (pypdfium2) and Pillow are
    optional, imported lazily like the other Tier 4 adapters."""
    try:
        import io

        import pdfplumber  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "Check-image extraction needs pdfplumber — `pip install pdfplumber`.") from exc
    images: dict[str, bytes] = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1, keep_blank_chars=False)
            for check_no, x0, top in first_service_check_anchors(words):
                col_x0, col_x1 = (_FSB_IMG_COL_LEFT if x0 < _FSB_IMG_COL_SPLIT
                                  else _FSB_IMG_COL_RIGHT)
                bbox = (col_x0, max(0, top + _FSB_IMG_CELL_TOP),
                        min(page.width, col_x1), min(page.height, top + _FSB_IMG_CELL_BOT))
                pim = page.crop(bbox).to_image(resolution=_FSB_IMG_RES)
                buf = io.BytesIO()
                # pypdfium2 renders in palette mode; JPEG needs RGB.
                pim.original.convert("RGB").save(buf, format="JPEG", quality=85)
                images[check_no] = buf.getvalue()
    return images


# Per-bank renderers that pull cancelled-check images out of the statement PDF:
# path -> {check_no: jpeg_bytes}.
PDF_CHECK_IMAGE_LAYOUTS = {
    "first_service_bank": _render_first_service_check_images,
}


def extract_check_images(path: str | Path, *, layout: str) -> dict[str, bytes]:
    """Extract cancelled-check images embedded in a PDF statement, keyed by check
    number. `layout` selects the per-bank renderer (PDF_CHECK_IMAGE_LAYOUTS)."""
    try:
        renderer = PDF_CHECK_IMAGE_LAYOUTS[layout]
    except KeyError:
        raise ValueError(
            f"No embedded check-image renderer for layout {layout!r} — known: "
            f"{sorted(PDF_CHECK_IMAGE_LAYOUTS)}")
    return renderer(path)
