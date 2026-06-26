"""Mapping-driven normalization: raw export → canonical transactions/vendors.

QB Desktop report exports are messy: title rows above the data, an empty
"Export Tips" sheet, indent columns, grouped layouts where the vendor/account
name appears once as a section row, subtotal/total rows, and headers that vary
by memorized-report configuration. This module:

  1. auto-detects the worksheet and header row by scoring rows against the
     mapping's expected column names (config/source_mappings.yaml)
  2. forward-fills grouped-report section headers (e.g. the vendor name row in
     Vendor Transaction Detail) into a synthetic column rows can map from
  3. maps source columns → canonical columns; missing *required* columns
     (date/amount or vendor identity) fail loudly with the file name, missing
     optional ones warn and fill None
  4. derives txn_type from the report's Type column where configured, dropping
     types another report owns (prevents the same check being counted from
     Check Detail, Vendor Detail, and the GL at once)
  5. drops subtotal/blank rows (no parseable date or amount), synthesizes a
     source_id where the report has no Trans # column, and de-duplicates real
     Trans # collisions across reports by per-report priority

Layout convention for the watched folder:

    data/<entity_id>/<source>__<report>.xlsx   e.g. data/hines-homes/qb__check_detail.xlsx

The entity_id path segment must match config/entities.yaml — this is how every
row gets stamped with its entity.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from core.entities import REPO_ROOT, EntityRegistry
from core.model import COST_LINE_COLUMNS, TRANSACTION_COLUMNS, VENDOR_COLUMNS

DEFAULT_MAPPINGS_PATH = REPO_ROOT / "config" / "source_mappings.yaml"

REQUIRED_TXN_FIELDS = {"date", "amount"}
REQUIRED_VENDOR_FIELDS = {"vendor_name"}
HEADER_SCAN_ROWS = 25


def load_mappings(path: Path | str = DEFAULT_MAPPINGS_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _candidates(spec) -> list[str]:
    """A column spec is a single header name or a list of candidate names (first
    one present in the export wins). Lets one mapping serve both QB Desktop and
    QuickBooks Online, whose reports label the same field differently
    (e.g. "Date" vs "Transaction date", "Memo" vs "Description")."""
    return [spec] if isinstance(spec, str) else list(spec)


def read_report(path: Path, mapping: dict) -> pd.DataFrame:
    """Read a QB-style export, locating the real header row on any sheet."""
    # Real header candidates from the column map (skip "__"-prefixed synthetic
    # group-header names), plus the txn_type_from column(s), so QBO headers are
    # recognized when scoring the header row.
    expected = {h for spec in mapping.get("columns", {}).values()
                for h in _candidates(spec) if not str(h).startswith("__")}
    type_spec = mapping.get("txn_type_from")
    if type_spec:
        expected |= set(_candidates(type_spec["column"]))
    if path.suffix.lower() == ".csv":
        sheets = {"csv": pd.read_csv(path, header=None, dtype=object,
                                     skip_blank_lines=False)}
    else:
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)

    best = None  # (mapping hits, non-empty cells, sheet name, row index, values)
    for sheet_name, raw in sheets.items():
        for i in range(min(HEADER_SCAN_ROWS, len(raw))):
            values = [str(v).strip() if pd.notna(v) else "" for v in raw.iloc[i]]
            hits = len(expected & set(values))
            non_empty = sum(1 for v in values if v)
            if best is None or (hits, non_empty) > (best[0], best[1]):
                best = (hits, non_empty, sheet_name, i, values)

    if best is None or best[0] == 0:
        layout = {name: f"{len(df)} rows x {df.shape[1]} cols" for name, df in sheets.items()}
        raise ValueError(
            f"{path.name}: no row in the first {HEADER_SCAN_ROWS} rows of any sheet "
            f"matches the expected headers {sorted(expected)} (sheets: {layout}). "
            "Update config/source_mappings.yaml to this export's column names."
        )

    _, _, sheet_name, header_idx, values = best
    raw = sheets[sheet_name]
    df = raw.iloc[header_idx + 1:].copy()
    # Name blank/duplicate headers uniquely (QB indent columns export as blanks)
    seen: dict[str, int] = {}
    columns = []
    for j, v in enumerate(values):
        name = v or f"_col{j}"
        if name in seen:
            seen[name] += 1
            name = f"{name}.{seen[name]}"
        else:
            seen[name] = 0
        columns.append(name)
    df.columns = columns
    return df.dropna(how="all")


def apply_group_headers(raw: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Forward-fill grouped-report section values into synthetic columns.

    Grouped QB reports (Vendor Transaction Detail, General Ledger) put the
    group key — vendor or account — on its own row. Each group_headers entry
    {column: "_col1", as: "__group_vendor"} carries that value down so detail
    rows can map it like a normal column. "Total …" closing rows are inert:
    they're followed immediately by the next section header.
    """
    for spec in mapping.get("group_headers", []):
        source = spec["column"]
        if source not in raw.columns:
            continue
        values = raw[source].where(raw[source].astype(str).str.strip().ne(""))
        raw[spec["as"]] = values.ffill()
    return raw


def normalize_frame(
    raw: pd.DataFrame,
    mapping: dict,
    entity_id: str,
    source_system: str,
    target_columns: list[str],
    label: str = "export",
) -> pd.DataFrame:
    """Apply a column mapping + constants to a raw export frame."""
    columns: dict = mapping.get("columns", {})
    constants: dict = mapping.get("constants", {})
    required = (REQUIRED_VENDOR_FIELDS if mapping.get("kind") == "vendors"
                else REQUIRED_TXN_FIELDS)
    out = pd.DataFrame(index=raw.index)
    for canonical in target_columns:
        if canonical in columns:
            cands = _candidates(columns[canonical])
            source_col = next((c for c in cands if c in raw.columns), None)
            if source_col is not None:
                out[canonical] = raw[source_col]
            elif canonical in required:
                raise ValueError(
                    f"{label}: mapping expects one of {cands} for required field "
                    f"'{canonical}' but the export has: {list(raw.columns)}. "
                    "Update config/source_mappings.yaml."
                )
            else:
                print(f"  ~ {label}: none of {cands} ({canonical}) found — "
                      "left blank. Adjust config/source_mappings.yaml if it exists "
                      "under another name.")
                out[canonical] = None
        elif canonical in constants:
            out[canonical] = constants[canonical]
        else:
            out[canonical] = None
    out["entity_id"] = entity_id
    out["source_system"] = source_system

    type_spec = mapping.get("txn_type_from")
    if type_spec and "txn_type" in target_columns:
        cands = _candidates(type_spec["column"])
        source_col = next((c for c in cands if c in raw.columns), None)
        if source_col is None:
            # No Type column present. If the mapping also sets a constant txn_type
            # (a QB Desktop report that needs no per-row type), keep that constant.
            if "txn_type" in constants:
                return out
            raise ValueError(f"{label}: txn_type_from column(s) {cands} not in export "
                             f"({list(raw.columns)})")
        mapped = raw[source_col].astype(str).str.strip().map(type_spec["values"])
        out["txn_type"] = mapped
        if type_spec.get("drop_unmapped", True):
            unmapped = (mapped.isna() & raw[source_col].notna()
                        & raw[source_col].astype(str).str.strip().ne(""))
            dropped = int(unmapped.sum())
            if dropped:
                # Surface the actual unmapped labels so a run reveals exactly which
                # QBO type strings still need adding to `values` (vs. silently lost).
                seen = sorted(raw.loc[unmapped, source_col].astype(str).str.strip().unique())
                print(f"  ~ {label}: dropped {dropped} rows with types owned by "
                      f"another report or out of scope (unmapped types: {seen[:12]})")
            out = out[mapped.notna()]
    return out


def clean_transactions(df: pd.DataFrame, label: str = "export") -> pd.DataFrame:
    """Drop QB report noise: subtotal/total/blank rows without a real date+amount."""
    if df.empty:
        return df
    dates = pd.to_datetime(df["date"], errors="coerce")
    amounts = pd.to_numeric(df["amount"], errors="coerce")
    keep = dates.notna() & amounts.notna()
    dropped = int((~keep).sum())
    if dropped:
        print(f"  ~ {label}: dropped {dropped} non-transaction rows "
              "(section headers/subtotals/blanks)")
    df = df[keep].copy()
    df["date"] = dates[keep]
    df["amount"] = amounts[keep]
    return df


def synthesize_source_ids(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Reports without a Trans # column get row-position ids (<report>:<row>).

    Stable within one export; once Trans # is added to the memorized report
    (Customize Report → Display) real ids take over automatically.
    """
    missing = df["source_id"].isna()
    if missing.any():
        df = df.copy()
        df.loc[missing, "source_id"] = [f"{key}:{i}" for i in df.index[missing]]
    return df


def dedupe_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """Keep each QB transaction once when several reports contain it.

    QB's Trans # is stable across reports, so for every (entity_id, source_id)
    we keep all rows from the highest-priority report that contains it
    (priorities in source_mappings.yaml). Synthesized ids never collide, so
    cross-report overlap for those files is handled by txn_type_from filters.
    """
    if df.empty or "_priority" not in df.columns:
        return df.drop(columns=["_priority"], errors="ignore")
    has_id = df["source_id"].notna()
    keyed = df[has_id]
    best = keyed.groupby(["entity_id", "source_id"])["_priority"].transform("max")
    out = pd.concat([keyed[keyed["_priority"] == best], df[~has_id]])
    return out.drop(columns=["_priority"]).sort_index()


def ensure_unique_source_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee one row per (entity_id, source_system, source_id).

    A real QB Trans # is shared by every split line of a single transaction (e.g.
    a credit memo posted across several GL accounts), so a report that maps
    source_id to Trans # (currently only credit_memos) emits duplicate keys. The
    transactions table's `unique (entity_id, source_system, source_id)` constraint
    — and the chunked upsert that loads it — reject duplicate keys *within one
    command* ("ON CONFLICT DO UPDATE command cannot affect row a second time").
    Suffix the 2nd+ occurrence of each key (#2, #3, …) so every split line
    persists as its own row, matching the line-level granularity the
    synthesized-id reports (Vendor Transaction Detail, GL) already produce.
    Synthesized ids are unique by construction, so they are left untouched.
    """
    if df.empty:
        return df
    rank = df.groupby(["entity_id", "source_system", "source_id"]).cumcount()
    if not (rank > 0).any():
        return df
    df = df.copy()
    df["source_id"] = df["source_id"].astype(str)
    # Seed with every key already in the frame so a suffix we mint never lands on
    # an id that exists elsewhere — e.g. a real id that already looks like
    # "<base>#2" would make a naive rank+1 scheme re-collide. Allocate the next
    # unused "#n" per (entity, source, base) instead.
    taken = set(zip(df["entity_id"], df["source_system"], df["source_id"]))
    for idx in rank[rank > 0].index:
        entity = df.at[idx, "entity_id"]
        system = df.at[idx, "source_system"]
        base = df.at[idx, "source_id"]
        n = 2
        while (entity, system, f"{base}#{n}") in taken:
            n += 1
        df.at[idx, "source_id"] = f"{base}#{n}"
        taken.add((entity, system, f"{base}#{n}"))
    return df


def ingest_data_dir(
    data_dir: Path,
    registry: EntityRegistry,
    mappings: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk data/<entity_id>/ and normalize every recognized export.

    Returns (transactions, vendors, cost_lines) canonical frames (unvalidated).
    Files whose stem doesn't match a mapping key are skipped with a notice.
    """
    txn_frames, vendor_frames, cost_line_frames = [], [], []
    skipped: list[str] = []
    known_ids = {e.id for e in registry}
    for entity_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if entity_dir.name not in known_ids:
            print(f"  ! Skipping {entity_dir} — '{entity_dir.name}' is not in config/entities.yaml")
            continue
        for file in sorted(entity_dir.iterdir()):
            if file.suffix.lower() not in {".xlsx", ".xls", ".csv"}:
                continue
            key = file.stem  # e.g. qb__check_detail
            mapping = mappings.get(key)
            if mapping is None:
                print(f"  ! No mapping for {file.name} (key '{key}') — skipped")
                continue
            if not mapping.get("enabled", True):
                print(f"  - {file.name}: mapping disabled "
                      f"({mapping.get('notes', 'see source_mappings.yaml')})")
                continue
            label = f"{entity_dir.name}/{file.name}"
            try:
                raw = apply_group_headers(read_report(file, mapping), mapping)
                source_system = key.split("__", 1)[0]
                kind = mapping.get("kind")
                if kind == "vendors":
                    frame = normalize_frame(raw, mapping, entity_dir.name, source_system,
                                            VENDOR_COLUMNS, label=label)
                    frame = frame[frame["vendor_name"].notna()]
                    vendor_frames.append(frame)
                elif kind == "cost_lines":
                    frame = normalize_frame(raw, mapping, entity_dir.name, source_system,
                                            COST_LINE_COLUMNS, label=label)
                    frame = clean_transactions(frame, label=label)
                    frame = synthesize_source_ids(frame, key)
                    cost_line_frames.append(frame)
                else:
                    frame = normalize_frame(raw, mapping, entity_dir.name, source_system,
                                            TRANSACTION_COLUMNS, label=label)
                    frame = clean_transactions(frame, label=label)
                    frame = synthesize_source_ids(frame, key)
                    frame["_priority"] = mapping.get("priority", 0)
                    txn_frames.append(frame)
            except Exception as exc:  # noqa: BLE001 — one unreadable export must not abort the batch
                # e.g. a QuickBooks Online export whose columns differ from the
                # mapping. Log loudly and skip so the other entities still run; the
                # skipped sources are summarized below (never silently dropped).
                print(f"  ✗ {label}: SKIPPED — {exc}")
                skipped.append(label)
    if skipped:
        print(f"  ⚠ Skipped {len(skipped)} export(s) that could not be read "
              "(format mismatch — e.g. QBO vs QB Desktop columns); their entities "
              "are missing from this run until the mapping/export is fixed:")
        for label in skipped:
            print(f"      - {label}")
    transactions = (dedupe_transactions(pd.concat(txn_frames, ignore_index=True))
                    if txn_frames else pd.DataFrame(columns=TRANSACTION_COLUMNS))
    transactions = ensure_unique_source_ids(transactions)
    vendors = (pd.concat(vendor_frames, ignore_index=True).drop_duplicates(
                   subset=["entity_id", "vendor_name"])
               if vendor_frames else pd.DataFrame(columns=VENDOR_COLUMNS))
    cost_lines = (pd.concat(cost_line_frames, ignore_index=True)
                  if cost_line_frames else pd.DataFrame(columns=COST_LINE_COLUMNS))
    return transactions, vendors, cost_lines
