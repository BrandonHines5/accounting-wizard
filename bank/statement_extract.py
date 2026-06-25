"""Tier 4 statement extraction (T4-01): bank statement → canonical bank_transactions.

Two layers:

1. `normalize_register` — the deterministic, tested core. Takes already-extracted
   register rows (a DataFrame from a CSV/Excel online-banking export, or the PDF
   adapter below) and maps them to the `bank_transactions` model: signed amounts
   (negative = money out), a hashed account fingerprint (never the raw number),
   parsed dates, and normalized check numbers. It validates before returning.
   `extract_export` is the thin file wrapper around it (CSV/Excel — the common
   path, since most banks export the register directly).

2. `extract_pdf` — a best-effort, format-specific adapter that pulls table rows
   out of a PDF statement with pdfplumber (an optional dependency, imported lazily
   like the Anthropic/Supabase adapters). Real statement layouts vary, so the read
   is advisory and feeds layer 1 — the determinism lives in layer 1.

Hard rules (CLAUDE.md): the raw account number is hashed on the way in and never
stored; check/statement images stay in SharePoint — only `image_ref` paths here.
Amounts handle accounting formats: '$1,234.56', '(1,234.56)' (parenthesis-
negative), and debit/credit column pairs.
"""
from __future__ import annotations

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
) -> pd.DataFrame:
    """Best-effort extraction of a PDF statement's register into canonical
    bank_transactions. Statement layouts vary, so the raw table read is advisory —
    map the bank's column headers via `columns` and review results before trusting
    them. The deterministic normalization is `normalize_register`."""
    raw = _read_pdf_tables(path, pages=pages)
    return normalize_register(
        raw, entity_id=entity_id, account_number=account_number,
        known_entity_ids=known_entity_ids, columns=columns, salt=salt,
        image_ref=image_ref or str(path))
