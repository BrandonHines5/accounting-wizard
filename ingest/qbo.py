"""Pull QB reports straight from QuickBooks Online via the Intuit Accounting API,
so the normal ingest runs over freshly-rendered export files — no hand-uploading
and no QB Desktop export step. This is the QBO analog of `ingest/sharepoint.py`.

Every entity EXCEPT Hines Homes and Titan House is on QuickBooks Online (those two
still export from QB Desktop and land via SharePoint until they migrate). Which
entities pull from QBO is config-driven (`config/qbo.yaml`, copied from
`qbo.example.yaml`); an entity is "on QBO" iff it appears there. Nothing about the
detection code changes — the connector writes the same
`data/<entity_id>/qb__*.csv` files the SharePoint/export path produces, so the
existing mapping-driven normalization (`ingest/normalize.py` +
`config/source_mappings.yaml`) consumes them unchanged. That mapping was already
built to serve both QB Desktop and QuickBooks Online column labels; the flattener
below emits the QBO-export labels it already lists, so no mapping change is needed.

Design
------
- `QboAuth` exchanges a per-company refresh token for a short-lived access token
  (OAuth 2.0 client-credentials-over-refresh-token). QBO rotates the refresh token
  roughly every 24h; whenever the token endpoint returns a new one, it is handed to
  the injected `token_store` so a stateless weekly run persists it (see
  `persistence/qbo_token_store.py`) and doesn't break within a day.
- `QboClient` calls the Reports API (`/v3/company/{realm}/reports/{name}`) and the
  query API (`SELECT … FROM Vendor`) and returns the raw JSON.
- `flatten_report` turns a report's grouped JSON tree into a flat detail frame,
  forward-filling each section's group value (e.g. the GL account) into a named
  column and dropping subtotal/summary rows. Column headers are resolved from the
  stable Intuit `ColKey` (not the localized `ColTitle`) to the export labels the
  mapping expects; unknown keys are kept under their report title and logged, the
  same best-effort-and-confirm-by-running posture as `source_mappings.yaml`.
- `pull_all` walks the config and writes one CSV per report per entity. One
  entity's failure (bad realm, expired token, API error) is logged and skipped,
  never aborting the batch — matching the SharePoint pull.

`session` (a `requests`-style object) and `client` are injectable for testing, so
the whole path is exercised with no network.

Auth env (one Intuit app, one connection per company):
    QBO_CLIENT_ID / QBO_CLIENT_SECRET   the app's OAuth client credentials
    <refresh_token_env>                 per-company refresh token (named per entity
                                        in config/qbo.yaml)
The connected app needs only read access (com.intuit.quickbooks.accounting scope);
no write scopes are used.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import pandas as pd
import yaml

from core.entities import REPO_ROOT

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "qbo.yaml"

# OAuth token endpoint is environment-independent (sandbox and production share it);
# only the API base URL differs.
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
PROD_API_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_API_BASE = "https://sandbox-quickbooks.api.intuit.com"
# Minor version pins the Accounting API response shape; bump deliberately.
MINOR_VERSION = "70"

# Global app-credential env vars (per-entity refresh tokens are named in the config).
QBO_ENV = ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET")

# Stable Intuit report ColKey -> the QBO-export column label the mapping expects
# (config/source_mappings.yaml). Keyed on ColKey because it is stable across
# companies and locales, unlike the human ColTitle. Several keys collapse to the
# same label (only one appears in any given report, so no duplicate columns arise).
COLKEY_LABELS = {
    # transaction date
    "tx_date": "Transaction date",
    "txn_date": "Transaction date",
    "date": "Transaction date",
    # transaction type
    "txn_type": "Transaction type",
    # document / reference number
    "doc_num": "Num",
    # payee / vendor / customer name
    "name": "Name",
    "vend_name": "Name",
    "cust_name": "Name",
    "emp_name": "Name",
    # memo / description
    "memo": "Description",
    # the counter / split account and the posting account
    "split_acc": "Split",
    "account_name": "Account full name",
    "account_type": "Account type",
    # signed amount
    "subt_nat_amount": "Amount",
    "nat_amount": "Amount",
    "amount": "Amount",
    "subt_nat_home_amount": "Amount",
    # class / job
    "klass_name": "Class",
    # who entered/last-touched it
    "create_by": "Created By",
    "last_mod_by": "Last modified by",
}

# Which QBO report (or query) feeds each mapping stem. Two stems share the flat
# TransactionList: the per-mapping txn_type filters in source_mappings.yaml keep
# disjoint types (vendor_transaction_detail keeps bills/payments/expenses;
# credit_memos keeps credit-memo/vendor-credit), so the same source rows never
# double-count — exactly how the QB Desktop world used two separate reports. The
# fetch is cached per (report, params) so TransactionList is pulled once per entity.
#
#   kind: report   -> Reports API; group_as forward-fills the section header
#   kind: vendors  -> Vendor query API (QBO has no vendor-contact-list report)
QBO_REPORTS: dict[str, dict] = {
    # Vendor money movement (bills, bill payments, direct expenses). Flat — the
    # vendor rides in the Name column, which the mapping already accepts.
    "qb__vendor_transaction_detail": {"kind": "report", "report": "TransactionList"},
    # General Ledger — grouped by account; the account is the section header, so
    # forward-fill it into "Distribution account" (a GL mapping candidate). The
    # GL mapping keeps only Journal Entry rows, so this contributes journals only.
    "qb__general_ledger": {"kind": "report", "report": "GeneralLedger",
                           "group_as": "Distribution account"},
    # Credit memos / vendor credits — same TransactionList, credit types only.
    "qb__credit_memos": {"kind": "report", "report": "TransactionList"},
    # Vendor master (contact list) via the query API.
    "qb__vendor_list": {"kind": "vendors"},
}


def load_qbo_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict | None:
    """Parsed config/qbo.yaml, or None if it doesn't exist (QBO pull disabled)."""
    path = Path(path)
    return yaml.safe_load(path.read_text()) if path.exists() else None


def api_base(config: dict | None) -> str:
    """Production vs sandbox API base URL from the config's `environment`."""
    env = str((config or {}).get("environment", "production")).lower()
    return SANDBOX_API_BASE if env == "sandbox" else PROD_API_BASE


# --------------------------------------------------------------------------- auth

class RefreshTokenStore(Protocol):
    """Reads (and, where it can, persists) a per-entity QBO refresh token."""

    def get(self, entity_id: str) -> str: ...

    def put(self, entity_id: str, realm_id: str, refresh_token: str) -> None: ...


class EnvRefreshTokenStore:
    """Refresh tokens from environment variables, one env var per entity (named in
    config/qbo.yaml via `refresh_token_env`).

    Env vars can't be written back, so `put` can't persist a rotation — it warns
    (never printing the token) that automation needs a persistent store
    (`--store supabase`, persistence/qbo_token_store.py) to survive QBO's ~daily
    refresh-token rotation. Fine for local/interactive runs where the secret is
    refreshed by hand."""

    def __init__(self, env_by_entity: dict[str, str]) -> None:
        self._env_by_entity = env_by_entity

    def get(self, entity_id: str) -> str:
        var = self._env_by_entity.get(entity_id)
        if not var:
            raise KeyError(
                f"No refresh_token_env configured for '{entity_id}' in config/qbo.yaml")
        token = os.environ.get(var)
        if not token:
            raise KeyError(
                f"QBO refresh token for '{entity_id}' is not set — export {var} "
                "(obtain it once via Intuit's authorization-code flow; never committed).")
        return token

    def put(self, entity_id: str, realm_id: str, refresh_token: str) -> None:
        var = self._env_by_entity.get(entity_id, "<refresh_token_env>")
        print(f"  ! QBO: refresh token for '{entity_id}' rotated. An env-only store "
              f"can't persist it; update the {var} secret, or run with --store supabase "
              "so the rotation is saved automatically (else the pull breaks within ~24h).")


class QboAuth:
    """Exchanges a per-entity refresh token for an access token, persisting any
    rotated refresh token via the injected `token_store`.

    Access tokens are cached per entity for the life of the object (a single run
    makes several report calls per entity), so the refresh endpoint is hit once
    per entity, not once per report."""

    def __init__(self, client_id: str, client_secret: str, token_store: RefreshTokenStore,
                 *, session=None, token_endpoint: str = TOKEN_ENDPOINT) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_store = token_store
        self.token_endpoint = token_endpoint
        self._session = session
        self._access_cache: dict[str, str] = {}

    @property
    def session(self):
        if self._session is None:
            import requests  # lazy: optional dependency
            self._session = requests.Session()
        return self._session

    def access_token(self, entity_id: str, realm_id: str, *, force: bool = False) -> str:
        if not force and entity_id in self._access_cache:
            return self._access_cache[entity_id]
        refresh = self.token_store.get(entity_id)
        resp = self.session.post(
            self.token_endpoint,
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            auth=(self.client_id, self.client_secret),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        rotated = payload.get("refresh_token")
        if rotated and rotated != refresh:
            self.token_store.put(entity_id, realm_id, rotated)
        token = payload["access_token"]
        self._access_cache[entity_id] = token
        return token


# ------------------------------------------------------------------------- client

class QboClient:
    """Reads QBO reports and vendor records for one Intuit app across companies.

    `auth` supplies the bearer token per (entity, realm); `session` is injectable
    for testing. `report`/`query` return the raw parsed JSON."""

    def __init__(self, auth: QboAuth, *, base_url: str = PROD_API_BASE, session=None,
                 minor_version: str = MINOR_VERSION) -> None:
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.minor_version = minor_version
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests  # lazy: optional dependency
            self._session = requests.Session()
        return self._session

    def _get(self, entity_id: str, realm_id: str, url: str, params: dict) -> dict:
        token = self.auth.access_token(entity_id, realm_id)
        resp = self.session.get(
            url, params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=60)
        resp.raise_for_status()
        return resp.json()

    def report(self, entity_id: str, realm_id: str, name: str, *, start: str | None = None,
               end: str | None = None, params: dict | None = None) -> dict:
        p: dict = {"minorversion": self.minor_version}
        if start:
            p["start_date"] = start
        if end:
            p["end_date"] = end
        p.update(params or {})
        url = f"{self.base_url}/v3/company/{realm_id}/reports/{name}"
        return self._get(entity_id, realm_id, url, p)

    def query(self, entity_id: str, realm_id: str, sql: str) -> dict:
        url = f"{self.base_url}/v3/company/{realm_id}/query"
        return self._get(entity_id, realm_id, url,
                         {"query": sql, "minorversion": self.minor_version})

    def vendors(self, entity_id: str, realm_id: str, *, page: int = 1000) -> list[dict]:
        """Every Vendor record, paging through the query API's STARTPOSITION cursor."""
        out: list[dict] = []
        start = 1
        while True:
            sql = f"SELECT * FROM Vendor STARTPOSITION {start} MAXRESULTS {page}"
            batch = (self.query(entity_id, realm_id, sql).get("QueryResponse") or {}).get("Vendor") or []
            out += batch
            if len(batch) < page:
                return out
            start += page


# ---------------------------------------------------------------------- flattening

def _col_key(col: dict) -> str | None:
    for meta in col.get("MetaData") or []:
        if meta.get("Name") == "ColKey":
            return meta.get("Value")
    return None


def _resolve_headers(columns: list[dict], colkey_labels: dict, label: str) -> list[str]:
    """Header name per report column, from ColKey (falling back to ColTitle), made
    unique. Unmapped ColKeys are surfaced so a real run reveals exactly which keys
    still need adding — the same posture the ingest takes for unmapped export
    columns."""
    headers: list[str] = []
    used: dict[str, int] = {}
    unknown: list[str] = []
    for i, col in enumerate(columns):
        key = _col_key(col)
        title = (col.get("ColTitle") or "").strip()
        header = colkey_labels.get(key)
        if header is None:
            header = title or key or f"col{i}"
            if key and key not in colkey_labels:
                unknown.append(f"{key!r} ({title!r})")
        if header in used:
            used[header] += 1
            header = f"{header}.{used[header]}"
        else:
            used[header] = 0
        headers.append(header)
    if unknown:
        print(f"  ~ {label}: unmapped QBO column key(s) {unknown} — kept under the "
              "report's own title; add to ingest.qbo.COLKEY_LABELS if a rule needs one.")
    return headers


def flatten_report(report: dict, *, group_as: str | None = None,
                   colkey_labels: dict | None = None, label: str = "report") -> pd.DataFrame:
    """Flatten a QBO report's grouped JSON tree into a flat detail DataFrame.

    A report is `{Columns:{Column:[…]}, Rows:{Row:[…]}}`. Each Row is either a
    Section (has nested `Rows`, with the group value in `Header.ColData[0]`) or a
    detail row (`ColData`). Sections' `Summary` subtotals are ignored. When
    `group_as` is set, each detail row gets the value of its nearest enclosing
    section header written into that named column (e.g. the GL account)."""
    labels = {**COLKEY_LABELS, **(colkey_labels or {})}
    columns = (report.get("Columns") or {}).get("Column") or []
    headers = _resolve_headers(columns, labels, label)

    records: list[dict] = []

    def walk(rows: list, group_val):
        for row in rows:
            nested = (row.get("Rows") or {}).get("Row")
            if nested is not None:                      # a Section: descend, tracking its group value
                header_cd = (row.get("Header") or {}).get("ColData") or []
                gv = header_cd[0].get("value") if header_cd else group_val
                walk(nested, gv if group_as else group_val)
                continue
            col_data = row.get("ColData")
            if not col_data:                            # a bare summary/blank row — skip
                continue
            values = [c.get("value") if isinstance(c, dict) else c for c in col_data]
            record = dict(zip(headers, values))
            if group_as and group_val is not None:
                record[group_as] = group_val
            records.append(record)

    walk((report.get("Rows") or {}).get("Row") or [], None)

    df = pd.DataFrame(records, columns=_frame_columns(headers, group_as))
    return df


def _frame_columns(headers: list[str], group_as: str | None) -> list[str]:
    """Column order for the flattened frame: the report's columns, plus the
    group_as column if it isn't already one of them (so a grouped report always
    emits the group column, even when a section happened to be empty)."""
    cols = list(headers)
    if group_as and group_as not in cols:
        cols.append(group_as)
    return cols


def flatten_vendors(vendors: list[dict]) -> pd.DataFrame:
    """Vendor query records -> a frame shaped for the qb__vendor_list mapping.

    Emits the QBO-export column labels the mapping already lists (Display Name,
    Billing address, Phone numbers, Tax ID, Created), so the vendor normalizer
    consumes it unchanged."""
    rows = [_vendor_row(v) for v in vendors]
    cols = ["Display Name", "Billing address", "Phone numbers", "Tax ID", "Created"]
    return pd.DataFrame(rows, columns=cols)


def _vendor_row(vendor: dict) -> dict:
    addr = vendor.get("BillAddr") or {}
    parts = [str(addr[k]) for k in
             ("Line1", "Line2", "City", "CountrySubDivisionCode", "PostalCode")
             if addr.get(k)]
    meta = vendor.get("MetaData") or {}
    return {
        "Display Name": vendor.get("DisplayName") or vendor.get("CompanyName"),
        "Billing address": ", ".join(parts) or None,
        "Phone numbers": (vendor.get("PrimaryPhone") or {}).get("FreeFormNumber"),
        "Tax ID": vendor.get("TaxIdentifier"),
        "Created": meta.get("CreateTime"),
    }


# --------------------------------------------------------------------------- pull

def _select_reports(names: list[str] | None) -> dict[str, dict]:
    """The report specs to pull for an entity: all of QBO_REPORTS, or the subset
    named in the entity's `reports:` override (unknown names are ignored with a
    notice)."""
    if not names:
        return QBO_REPORTS
    selected = {}
    for name in names:
        if name in QBO_REPORTS:
            selected[name] = QBO_REPORTS[name]
        else:
            print(f"  ! QBO: unknown report '{name}' in config — skipped "
                  f"(known: {sorted(QBO_REPORTS)})")
    return selected


def pull_entity(client: QboClient, entity_id: str, realm_id: str, dest_dir: Path | str, *,
                start: str | None = None, end: str | None = None,
                reports: list[str] | None = None, on_file=None) -> list[str]:
    """Pull each configured report for one entity and write it as
    <dest_dir>/<stem>.csv (the same files the export/SharePoint path drops).

    Reports fetched from the same underlying QBO report+params are pulled once and
    reused (TransactionList feeds two stems). Empty reports are skipped, not
    written. Returns the written filenames."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    fetch_cache: dict[tuple, dict] = {}
    for stem, spec in _select_reports(reports).items():
        label = f"{entity_id}/{stem}"
        if spec["kind"] == "vendors":
            df = flatten_vendors(client.vendors(entity_id, realm_id))
        else:
            params = spec.get("params") or {}
            key = (spec["report"], start, end, tuple(sorted(params.items())))
            if key not in fetch_cache:
                fetch_cache[key] = client.report(entity_id, realm_id, spec["report"],
                                                 start=start, end=end, params=params)
            df = flatten_report(fetch_cache[key], group_as=spec.get("group_as"),
                                colkey_labels=spec.get("colkey_labels"), label=label)
        if df.empty:
            print(f"  ~ QBO {label}: no rows returned for the window — not written.")
            continue
        path = dest / f"{stem}.csv"
        df.to_csv(path, index=False)
        written.append(path.name)
        if on_file is not None:
            on_file(path.name, path.stat().st_size)
    return written


def _realm_id(ent: dict) -> str | None:
    """An entity's QBO realm (company) id — inline `realm_id`, or from the env var
    named by `realm_id_env`. Not a secret, so inline is fine."""
    realm = (ent or {}).get("realm_id")
    if realm:
        return str(realm)
    var = (ent or {}).get("realm_id_env")
    return os.environ.get(var) if var else None


def pull_all(config: dict, data_dir: Path | str, registry, *, client: QboClient,
             start: str | None = None, end: str | None = None,
             entities: set[str] | None = None, on_file=None) -> dict:
    """Pull every configured, registered, active entity's reports into
    <data-dir>/<entity_id>/.

    `entities` optionally scopes the pull to a subset of ids (mirrors --entity).
    One entity's failure (missing realm/token, API error) is logged and skipped so
    the others still pull and the run completes. Returns {entity_id: [written
    files]} for every entity attempted."""
    known = {e.id for e in registry}
    active = {e.id for e in registry.active()}
    pulled: dict[str, list[str]] = {}
    for entity_id, ent in (config.get("entities") or {}).items():
        if entities is not None and entity_id not in entities:
            continue
        if entity_id not in known:
            print(f"  ! QBO: '{entity_id}' is not in config/entities.yaml — skipped")
            continue
        if entity_id not in active:
            print(f"  - QBO: '{entity_id}' is inactive in the registry — skipped")
            continue
        realm_id = _realm_id(ent)
        if not realm_id:
            print(f"  ! QBO: no realm_id for '{entity_id}' in config/qbo.yaml — skipped")
            pulled[entity_id] = []
            continue
        try:
            pulled[entity_id] = pull_entity(
                client, entity_id, realm_id, Path(data_dir) / entity_id,
                start=start, end=end, reports=(ent or {}).get("reports"), on_file=on_file)
        except Exception as exc:  # noqa: BLE001 — one entity's connection must not abort the batch
            # A bad realm, an expired/rotated refresh token, or a transient API error
            # for one company is logged and skipped so the other entities still pull.
            print(f"  ! QBO: could not pull '{entity_id}' "
                  f"({type(exc).__name__}: {exc}) — skipped; other entities continue")
            pulled[entity_id] = []
    return pulled
