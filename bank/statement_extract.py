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
_FSB_LINE_GAP = 4          # rows are ~10pt apart; a superscripted break-marker '*'
                           #   sits ~0.25pt off its row. Cluster words by vertical
                           #   gap (4 > intra-row jitter, < inter-row spacing) so a
                           #   '*' never splits its check onto a phantom line.

_FSB_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}$")
_FSB_AMT_RE = re.compile(r"^[\d,]*\.\d{2}$")     # allows sub-dollar '.25' (no lead digit)
_FSB_CHECKNO_RE = re.compile(r"^\d+\*?$")        # trailing '*' = break in check sequence
_FSB_STMT_DATE_RE = re.compile(r"Statement Date:\s*(\d{1,2})-(\d{1,2})-(\d{2})")


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
    """A checks-grid line carries up to three (date, check no, amount) triples,
    one per column group. Checks are disbursements → negative amounts."""
    groups: list[list[dict]] = [[], [], []]
    for w in row:
        g = 0 if w["x0"] < _FSB_GRID_SPLITS[0] else (1 if w["x0"] < _FSB_GRID_SPLITS[1] else 2)
        groups[g].append(w)
    out: list[dict] = []
    for grp in groups:
        if len(grp) < 3:
            continue
        date, checkno, *rest = grp
        if not _FSB_DATE_RE.match(date["text"]) or not _FSB_CHECKNO_RE.match(checkno["text"]):
            continue
        amt = _fsb_amount(rest)
        if not amt:
            continue
        number = checkno["text"].rstrip("*")
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


def _read_first_service_pdf(path: str | Path) -> pd.DataFrame:
    """Open a First Service Bank PDF and feed its word boxes to the pure parser."""
    try:
        import pdfplumber  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "First Service Bank PDF parsing needs pdfplumber — `pip install "
            "pdfplumber`.") from exc
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_words(x_tolerance=1, keep_blank_chars=False)
                 for page in pdf.pages]
    return parse_first_service_words(pages)


# Per-bank PDF layout parsers: path -> DataFrame[date, description, amount, check_no].
PDF_LAYOUTS = {
    "first_service_bank": _read_first_service_pdf,
}
