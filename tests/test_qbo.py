"""QuickBooks Online connector: report-JSON flattening, OAuth refresh + rotation,
the REST client, and the per-entity pull — all exercised with fake HTTP sessions
and fake clients (no network). The final test proves a pulled CSV normalizes
through the real source_mappings.yaml, so the API path and the export path meet."""
from types import SimpleNamespace

import pandas as pd
import pytest

from ingest.qbo import (QBO_REPORTS, SANDBOX_API_BASE, TOKEN_ENDPOINT,
                        EnvRefreshTokenStore, QboAuth, QboClient, _request_with_retry,
                        api_base, discovery_document, flatten_report, flatten_vendors,
                        load_qbo_config, pull_all, pull_entity, resolve_token_endpoint)


# --------------------------------------------------------------- report fixtures

def _col(title, key):
    return {"ColTitle": title, "MetaData": [{"Name": "ColKey", "Value": key}]}


def _data(*values):
    return {"ColData": [{"value": v} for v in values], "type": "Data"}


# General Ledger: grouped by account (the account is the section header, not a
# column), with a subtotal Summary row that must be dropped.
GL_REPORT = {
    "Header": {"ReportName": "GeneralLedger"},
    "Columns": {"Column": [
        _col("Date", "tx_date"), _col("Transaction Type", "txn_type"),
        _col("Num", "doc_num"), _col("Name", "name"),
        _col("Memo/Description", "memo"), _col("Split", "split_acc"),
        _col("Amount", "subt_nat_amount"),
    ]},
    "Rows": {"Row": [{
        "Header": {"ColData": [{"value": "Checking", "id": "35"}]},
        "Rows": {"Row": [
            _data("2026-06-01", "Journal Entry", "JE1", "Acme", "reclass",
                  "Accounts Payable", "-100.00"),
            _data("2026-06-02", "Journal Entry", "JE2", "", "fix",
                  "Owner Draw", "50.00"),
        ]},
        "Summary": {"ColData": [{"value": "Total for Checking"}, {"value": ""},
                                {"value": ""}, {"value": ""}, {"value": ""},
                                {"value": ""}, {"value": "-50.00"}]},
        "type": "Section",
    }]},
}

# Transaction List: flat (no sections) — the vendor rides in the Name column.
# Covers the four type buckets: bill + bill payment (kept by vendor detail),
# vendor credit (kept by credit memos), invoice (dropped by both).
TXN_REPORT = {
    "Columns": {"Column": [
        _col("Date", "tx_date"), _col("Transaction Type", "txn_type"),
        _col("Num", "doc_num"), _col("Name", "name"),
        _col("Memo/Description", "memo"), _col("Account", "account_name"),
        _col("Split", "split_acc"), _col("Amount", "subt_nat_amount"),
    ]},
    "Rows": {"Row": [
        _data("2026-06-03", "Bill", "INV-1", "Acme Lumber", "framing",
              "Accounts Payable", "06-100 Framing", "1000.00"),
        _data("2026-06-05", "Bill Payment", "2001", "Acme Lumber", "",
              "Checking", "Accounts Payable", "1000.00"),
        _data("2026-06-07", "Vendor Credit", "VC-9", "Acme Lumber", "return",
              "Accounts Payable", "06-100 Framing", "-200.00"),
        _data("2026-06-09", "Invoice", "1500", "Homebuyer", "draw",
              "Accounts Receivable", "Sales", "5000.00"),
    ]},
}


# ----------------------------------------------------------------- flatten_report

def test_flatten_report_grouped_gl_fills_account_and_drops_summary():
    df = flatten_report(GL_REPORT, group_as="Distribution account",
                        label="alpha/qb__general_ledger")
    assert len(df) == 2                                   # summary row excluded
    assert list(df["Distribution account"]) == ["Checking", "Checking"]
    assert list(df["Transaction type"]) == ["Journal Entry", "Journal Entry"]
    assert list(df["Amount"]) == ["-100.00", "50.00"]
    # ColKey -> the export label the mapping expects
    assert set(["Transaction date", "Num", "Name", "Description", "Split"]) <= set(df.columns)
    assert "Total for Checking" not in df["Transaction date"].tolist()


def test_flatten_report_flat_transaction_list():
    df = flatten_report(TXN_REPORT)
    assert len(df) == 4
    assert list(df["Transaction type"]) == ["Bill", "Bill Payment", "Vendor Credit", "Invoice"]
    assert "Account full name" in df.columns          # account_name colkey
    assert "Split" in df.columns
    assert list(df["Name"])[:2] == ["Acme Lumber", "Acme Lumber"]


def test_flatten_report_logs_unmapped_colkey(capsys):
    report = {"Columns": {"Column": [
        _col("Weird Custom", "mystery_key"), _col("Amount", "subt_nat_amount")]},
        "Rows": {"Row": [_data("x", "1.00")]}}
    df = flatten_report(report, label="alpha/qb__x")
    assert "Weird Custom" in df.columns              # kept under its report title
    assert "Amount" in df.columns
    out = capsys.readouterr().out
    assert "unmapped QBO column key" in out and "mystery_key" in out


def test_flatten_report_emits_group_column_even_without_sections():
    report = {"Columns": {"Column": [_col("Amount", "subt_nat_amount")]},
              "Rows": {"Row": [_data("1.00")]}}
    df = flatten_report(report, group_as="Distribution account")
    assert "Distribution account" in df.columns
    assert df["Distribution account"].isna().all()


def test_flatten_report_empty():
    assert flatten_report({"Columns": {"Column": []}, "Rows": {"Row": []}}).empty


# ---------------------------------------------------------------- flatten_vendors

def test_flatten_vendors_maps_to_export_labels():
    vendors = [
        {"DisplayName": "Acme Lumber",
         "PrimaryPhone": {"FreeFormNumber": "555-1000"},
         "TaxIdentifier": "12-3456789",
         "MetaData": {"CreateTime": "2026-01-02T10:00:00-06:00"},
         "BillAddr": {"Line1": "1 Main St", "City": "Austin",
                      "CountrySubDivisionCode": "TX", "PostalCode": "78701"}},
        {"CompanyName": "NoName Co"},                 # sparse: falls back to CompanyName
    ]
    df = flatten_vendors(vendors)
    assert list(df.columns) == ["Display Name", "Billing address", "Phone numbers",
                                "Tax ID", "Created"]
    assert df.iloc[0]["Display Name"] == "Acme Lumber"
    assert df.iloc[0]["Tax ID"] == "12-3456789"
    assert df.iloc[0]["Phone numbers"] == "555-1000"
    assert "Austin" in df.iloc[0]["Billing address"]
    assert df.iloc[1]["Display Name"] == "NoName Co"


def test_flatten_vendors_empty():
    assert flatten_vendors([]).empty


# ---------------------------------------------------------------------- OAuth

class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAuthSession:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "auth": auth, "headers": headers})
        return _Resp(payload=self._payloads.pop(0))


class _RecordingStore:
    def __init__(self, token):
        self.token = token
        self.puts = []

    def get(self, entity_id):
        return self.token

    def put(self, entity_id, realm_id, refresh_token):
        self.puts.append((entity_id, realm_id, refresh_token))
        self.token = refresh_token


def test_access_token_refreshes_caches_and_persists_rotation():
    sess = _FakeAuthSession([{"access_token": "AT1", "refresh_token": "RT2"}])
    store = _RecordingStore("RT1")
    auth = QboAuth("cid", "secret", store, session=sess)
    assert auth.access_token("alpha", "R1") == "AT1"
    assert store.puts == [("alpha", "R1", "RT2")]         # rotation persisted
    # cached: a second call for the same entity does not hit the token endpoint again
    assert auth.access_token("alpha", "R1") == "AT1"
    assert len(sess.calls) == 1
    call = sess.calls[0]
    assert call["data"] == {"grant_type": "refresh_token", "refresh_token": "RT1"}
    assert call["auth"] == ("cid", "secret")


def test_access_token_no_persist_when_refresh_unchanged():
    sess = _FakeAuthSession([{"access_token": "AT", "refresh_token": "RT1"}])
    store = _RecordingStore("RT1")
    QboAuth("cid", "secret", store, session=sess).access_token("alpha", "R1")
    assert store.puts == []


def test_access_token_no_persist_when_no_refresh_returned():
    sess = _FakeAuthSession([{"access_token": "AT"}])          # no refresh_token key
    store = _RecordingStore("RT1")
    QboAuth("cid", "secret", store, session=sess).access_token("alpha", "R1")
    assert store.puts == []


# ---------------------------------------------------------- EnvRefreshTokenStore

def test_env_token_store_reads_configured_var(monkeypatch):
    monkeypatch.setenv("QBO_RT_ALPHA", "RT-alpha")
    assert EnvRefreshTokenStore({"alpha": "QBO_RT_ALPHA"}).get("alpha") == "RT-alpha"


def test_env_token_store_missing_var_raises():
    with pytest.raises(KeyError):
        EnvRefreshTokenStore({"alpha": "DEFINITELY_UNSET_VAR"}).get("alpha")


def test_env_token_store_unconfigured_entity_raises():
    with pytest.raises(KeyError):
        EnvRefreshTokenStore({}).get("alpha")


def test_env_token_store_put_warns_without_leaking_token(capsys):
    store = EnvRefreshTokenStore({"alpha": "QBO_RT_ALPHA"})
    store.put("alpha", "R1", "SECRET-NEW-TOKEN")            # must not raise
    out = capsys.readouterr().out
    assert "rotated" in out and "QBO_RT_ALPHA" in out
    assert "SECRET-NEW-TOKEN" not in out                   # never print the token


# -------------------------------------------------------------------- QboClient

class _StaticAuth:
    def __init__(self, token="TKN"):
        self.token = token

    def access_token(self, entity_id, realm_id, force=False):
        return self.token


class _FakeApiSession:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return _Resp(payload=self._payloads.pop(0))


def test_report_builds_url_params_and_bearer():
    sess = _FakeApiSession([{"Rows": {}}])
    client = QboClient(_StaticAuth("TKN"), base_url=SANDBOX_API_BASE, session=sess,
                       minor_version="70")
    client.report("alpha", "R9", "TransactionList", start="2026-01-01", end="2026-06-30")
    call = sess.calls[0]
    assert call["url"].endswith("/v3/company/R9/reports/TransactionList")
    assert call["params"]["start_date"] == "2026-01-01"
    assert call["params"]["end_date"] == "2026-06-30"
    assert call["params"]["minorversion"] == "70"
    assert call["headers"] == {"Authorization": "Bearer TKN", "Accept": "application/json"}


def test_vendors_paginates_until_short_page():
    pages = [
        {"QueryResponse": {"Vendor": [{"DisplayName": "A"}, {"DisplayName": "B"}]}},
        {"QueryResponse": {"Vendor": [{"DisplayName": "C"}]}},   # short page → stop
    ]
    sess = _FakeApiSession(pages)
    client = QboClient(_StaticAuth(), base_url=SANDBOX_API_BASE, session=sess)
    vendors = client.vendors("alpha", "R1", page=2)
    assert [v["DisplayName"] for v in vendors] == ["A", "B", "C"]
    assert len(sess.calls) == 2
    assert "STARTPOSITION 1 " in sess.calls[0]["params"]["query"]
    assert "STARTPOSITION 3 " in sess.calls[1]["params"]["query"]


def test_api_base_selects_environment():
    assert api_base({"environment": "sandbox"}) == SANDBOX_API_BASE
    assert api_base({}).startswith("https://quickbooks.api.intuit.com")


# ------------------------------------------------------------------- pull_entity

class _FakeClient:
    """Returns the same report JSON for every report name and a fixed vendor list;
    records how many times each report/query was fetched."""

    def __init__(self, report_json, vendors):
        self._report = report_json
        self._vendors = vendors
        self.report_calls = []
        self.vendor_calls = 0

    def report(self, entity_id, realm_id, name, start=None, end=None, params=None):
        self.report_calls.append(name)
        return self._report

    def vendors(self, entity_id, realm_id, page=1000):
        self.vendor_calls += 1
        return self._vendors


def test_pull_entity_writes_all_stems_and_caches_transaction_list(tmp_path):
    client = _FakeClient(TXN_REPORT, [{"DisplayName": "Acme Lumber", "TaxIdentifier": "12-3"}])
    written = pull_entity(client, "alpha", "R1", tmp_path / "alpha",
                          start="2026-01-01", end="2026-06-30")
    assert sorted(written) == ["qb__credit_memos.csv", "qb__general_ledger.csv",
                               "qb__vendor_list.csv", "qb__vendor_transaction_detail.csv"]
    # TransactionList feeds two stems but is fetched once (cache); GL fetched once.
    assert client.report_calls.count("TransactionList") == 1
    assert client.report_calls.count("GeneralLedger") == 1
    assert client.vendor_calls == 1
    vl = pd.read_csv(tmp_path / "alpha" / "qb__vendor_list.csv")
    assert "Display Name" in vl.columns and vl.iloc[0]["Display Name"] == "Acme Lumber"


def test_pull_entity_skips_empty_reports(tmp_path):
    client = _FakeClient({"Columns": {"Column": []}, "Rows": {"Row": []}}, [])
    assert pull_entity(client, "alpha", "R1", tmp_path / "alpha") == []


def test_pull_entity_honors_reports_subset(tmp_path):
    client = _FakeClient(TXN_REPORT, [])
    written = pull_entity(client, "alpha", "R1", tmp_path / "alpha",
                          reports=["qb__vendor_transaction_detail"])
    assert written == ["qb__vendor_transaction_detail.csv"]
    assert client.report_calls == ["TransactionList"]


# --------------------------------------------------------------------- pull_all

def test_pull_all_skips_unknown_inactive_and_isolates_errors(tmp_path, registry):
    class _PerEntity:
        def __init__(self):
            self.seen = []

        def report(self, entity_id, realm_id, name, start=None, end=None, params=None):
            self.seen.append(entity_id)
            if entity_id == "beta":
                raise RuntimeError("token expired")
            return TXN_REPORT

        def vendors(self, entity_id, realm_id, page=1000):
            return []

    client = _PerEntity()
    cfg = {"entities": {
        "alpha": {"realm_id": "R1"},        # active + registered → pulled
        "beta": {"realm_id": "R2"},         # raises → isolated, empty list
        "delta": {"realm_id": "R4"},        # inactive in the registry → skipped
        "ghost": {"realm_id": "R9"},        # not registered → skipped
    }}
    pulled = pull_all(cfg, tmp_path, registry, client=client,
                      start="2026-01-01", end="2026-06-30")
    assert pulled["alpha"]                  # wrote files
    assert pulled["beta"] == []             # error isolated, batch continued
    assert "delta" not in pulled            # inactive
    assert "ghost" not in pulled            # unknown entity
    assert "delta" not in client.seen and "ghost" not in client.seen


def test_pull_all_scopes_to_requested_entities(tmp_path, registry):
    client = _FakeClient(TXN_REPORT, [])
    cfg = {"entities": {"alpha": {"realm_id": "R1"}, "beta": {"realm_id": "R2"}}}
    pulled = pull_all(cfg, tmp_path, registry, client=client, entities={"alpha"})
    assert set(pulled) == {"alpha"}


def test_pull_all_skips_entity_without_realm(tmp_path, registry):
    client = _FakeClient(TXN_REPORT, [])
    pulled = pull_all({"entities": {"alpha": {}}}, tmp_path, registry, client=client)
    assert pulled == {"alpha": []}


# ------------------------------------------------------- Supabase token store

class _FakeSupaTable:
    def __init__(self, rows):
        self._rows = rows
        self.upserts = []

    def select(self, cols):
        return self

    def eq(self, key, value):
        return self

    def limit(self, n):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)

    def upsert(self, row, on_conflict=None):
        self.upserts.append((row, on_conflict))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[row]))


class _FakeSupaClient:
    def __init__(self, table):
        self._table = table

    def schema(self, schema):
        return self

    def table(self, name):
        return self._table


class _SeedStore:
    def __init__(self, token="ENV-RT"):
        self.token = token
        self.gets = []

    def get(self, entity_id):
        self.gets.append(entity_id)
        return self.token


def test_supabase_token_store_prefers_db_then_seeds_and_persists():
    from persistence.qbo_token_store import SupabaseRefreshTokenStore

    # DB has a stored token → used directly.
    table = _FakeSupaTable(rows=[{"refresh_token": "DB-RT"}])
    store = SupabaseRefreshTokenStore(_FakeSupaClient(table), _SeedStore())
    assert store.get("alpha") == "DB-RT"

    # DB empty → bootstrap from the env seed.
    seed = _SeedStore("ENV-RT")
    empty = _FakeSupaTable(rows=[])
    store2 = SupabaseRefreshTokenStore(_FakeSupaClient(empty), seed)
    assert store2.get("alpha") == "ENV-RT"
    assert seed.gets == ["alpha"]

    # put upserts the rotated token on the entity_id conflict key.
    store2.put("alpha", "R1", "NEW-RT")
    assert empty.upserts and empty.upserts[0][0]["refresh_token"] == "NEW-RT"
    assert empty.upserts[0][0]["realm_id"] == "R1"
    assert empty.upserts[0][1] == "entity_id"


def test_supabase_token_store_list_connections():
    from persistence.qbo_token_store import SupabaseRefreshTokenStore

    table = _FakeSupaTable(rows=[{"entity_id": "hope-filled", "realm_id": "R1"},
                                 {"entity_id": "mojuva", "realm_id": "R2"},
                                 {"entity_id": "incomplete", "realm_id": None}])
    store = SupabaseRefreshTokenStore(_FakeSupaClient(table), _SeedStore())
    assert store.list_connections() == {"hope-filled": "R1", "mojuva": "R2"}


# --------------------------------------------- realm_overrides (UI connections)

def test_pull_all_uses_realm_override_when_config_realm_absent(tmp_path, registry):
    client = _FakeClient(TXN_REPORT, [])
    pulled = pull_all({"entities": {"alpha": {}}}, tmp_path, registry, client=client,
                      realm_overrides={"alpha": "R-DB"})
    assert pulled["alpha"]                                  # pulled using the DB realm


def test_pull_all_config_realm_beats_override(tmp_path, registry):
    seen = {}

    class _C:
        def report(self, entity_id, realm_id, name, start=None, end=None, params=None):
            seen["realm"] = realm_id
            return TXN_REPORT

        def vendors(self, entity_id, realm_id, page=1000):
            return []

    pull_all({"entities": {"alpha": {"realm_id": "R-CFG"}}}, tmp_path, registry,
             client=_C(), realm_overrides={"alpha": "R-DB"})
    assert seen["realm"] == "R-CFG"                         # explicit config realm wins


def test_pull_all_skips_when_no_realm_config_or_override(tmp_path, registry):
    client = _FakeClient(TXN_REPORT, [])
    pulled = pull_all({"entities": {"alpha": {}}}, tmp_path, registry, client=client)
    assert pulled == {"alpha": []}                          # no realm anywhere → skipped


# ------------------------------------------------- config + end-to-end normalize

def test_load_qbo_config_absent_returns_none(tmp_path):
    assert load_qbo_config(tmp_path / "nope.yaml") is None


def test_qbo_reports_registry_covers_expected_stems():
    assert set(QBO_REPORTS) == {"qb__vendor_transaction_detail", "qb__general_ledger",
                                "qb__credit_memos", "qb__vendor_list"}


def test_pulled_csv_normalizes_through_source_mappings(tmp_path, registry):
    """The payoff: a report flattened to CSV is consumed by the real
    source_mappings.yaml exactly like a QBO export — vendor detail keeps
    bill/bill_payment, credit memos keep the vendor credit, income is dropped."""
    from ingest.normalize import ingest_data_dir, load_mappings

    data_dir = tmp_path / "data"
    entity_dir = data_dir / "alpha"
    entity_dir.mkdir(parents=True)
    frame = flatten_report(TXN_REPORT)
    frame.to_csv(entity_dir / "qb__vendor_transaction_detail.csv", index=False)
    frame.to_csv(entity_dir / "qb__credit_memos.csv", index=False)

    transactions, _vendors, _cost = ingest_data_dir(data_dir, registry, load_mappings())
    alpha = transactions[transactions["entity_id"] == "alpha"]
    assert sorted(alpha["txn_type"].dropna().tolist()) == ["bill", "bill_payment", "credit_memo"]
    assert set(alpha["vendor_name"].dropna()) == {"Acme Lumber"}
    assert alpha["source_system"].unique().tolist() == ["qb"]


# ------------------------------------------------- retry with backoff (Q3)

class _SeqSession:
    """A fake session whose get/post return queued items in order; an item that is
    an Exception is raised instead of returned (a network error)."""

    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    def _next(self):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, *a, **kw):
        return self._next()

    def get(self, *a, **kw):
        return self._next()


def test_request_with_retry_retries_transient_then_succeeds():
    seq = [_Resp(503), _Resp(503), _Resp(200, payload={"ok": True})]
    calls = {"n": 0}
    slept = []

    def do():
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    resp = _request_with_retry(do, label="x", retries=3, backoff=1.0, sleep=slept.append)
    assert resp.status_code == 200 and calls["n"] == 3
    assert slept == [1.0, 2.0]                       # exponential backoff before retries 2 and 3


def test_request_with_retry_does_not_retry_client_error():
    calls = {"n": 0}
    slept = []

    def do():
        calls["n"] += 1
        return _Resp(400)                            # invalid_grant etc. — not retryable

    with pytest.raises(RuntimeError):
        _request_with_retry(do, label="x", retries=3, sleep=slept.append)
    assert calls["n"] == 1 and slept == []


def test_request_with_retry_exhausts_then_raises_on_persistent_network_error():
    calls = {"n": 0}
    slept = []

    def do():
        calls["n"] += 1
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        _request_with_retry(do, label="x", retries=2, backoff=1.0, sleep=slept.append)
    assert calls["n"] == 3 and slept == [1.0, 2.0]   # 1 initial + 2 retries


def test_request_with_retry_final_transient_status_raises():
    with pytest.raises(RuntimeError):
        _request_with_retry(lambda: _Resp(503), label="x", retries=1, sleep=lambda s: None)


def test_request_with_retry_logs_intuit_tid_on_error(capsys):
    resp = _Resp(400, headers={"intuit_tid": "tid-abc123"})
    with pytest.raises(RuntimeError):
        _request_with_retry(lambda: resp, label="token refresh", retries=0, sleep=lambda s: None)
    assert "intuit_tid=tid-abc123" in capsys.readouterr().out


def test_request_with_retry_includes_intuit_tid_in_retry_reason(capsys):
    seq = [_Resp(503, headers={"intuit_tid": "tid-1"}), _Resp(200, payload={"ok": 1})]
    calls = {"n": 0}

    def do():
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    _request_with_retry(do, label="API request", retries=2, backoff=1.0, sleep=lambda s: None)
    assert "intuit_tid=tid-1" in capsys.readouterr().out


def test_request_with_retry_success_without_tid_is_quiet(capsys):
    resp = _request_with_retry(lambda: _Resp(200, payload={"ok": 1}), label="x",
                               retries=1, sleep=lambda s: None)
    assert resp.status_code == 200
    assert "intuit_tid" not in capsys.readouterr().out    # nothing logged on a clean success


def test_access_token_retries_transient_then_persists():
    sess = _SeqSession([_Resp(503),
                        _Resp(200, payload={"access_token": "AT", "refresh_token": "RT2"})])
    store = _RecordingStore("RT1")
    slept = []
    auth = QboAuth("cid", "secret", store, session=sess, backoff=1.0, sleep=slept.append)
    assert auth.access_token("alpha", "R1") == "AT"
    assert sess.calls == 2 and slept == [1.0]
    assert store.puts == [("alpha", "R1", "RT2")]    # rotation still persisted after the retry


def test_access_token_does_not_retry_invalid_grant():
    sess = _SeqSession([_Resp(400, payload={"error": "invalid_grant"})])
    store = _RecordingStore("RT1")
    slept = []
    auth = QboAuth("cid", "secret", store, session=sess, sleep=slept.append)
    with pytest.raises(RuntimeError):
        auth.access_token("alpha", "R1")
    assert sess.calls == 1 and slept == [] and store.puts == []


def test_client_get_retries_transient():
    sess = _SeqSession([_Resp(500), _Resp(200, payload={"ok": 1})])
    slept = []
    client = QboClient(_StaticAuth("TKN"), base_url=SANDBOX_API_BASE, session=sess,
                       backoff=1.0, sleep=slept.append)
    assert client.report("alpha", "R1", "TransactionList") == {"ok": 1}
    assert sess.calls == 2 and slept == [1.0]


# ------------------------------------------ discovery-document endpoints (Q5)

def test_discovery_document_returns_endpoints():
    doc = {"issuer": "https://oauth.platform.intuit.com/op/v1",
           "token_endpoint": "https://disco.example/token",
           "authorization_endpoint": "https://disco.example/authorize"}
    sess = _SeqSession([_Resp(200, payload=doc)])
    assert discovery_document("production", session=sess)["token_endpoint"] == "https://disco.example/token"


def test_resolve_token_endpoint_uses_discovery():
    doc = {"token_endpoint": "https://disco.example/token"}
    sess = _SeqSession([_Resp(200, payload=doc)])
    assert resolve_token_endpoint("production", session=sess) == "https://disco.example/token"


def test_resolve_token_endpoint_falls_back_when_discovery_unreachable():
    sess = _SeqSession([RuntimeError("no network")])
    # retries=0 → one attempt, which raises → caught → documented fallback returned.
    assert resolve_token_endpoint("sandbox", session=sess, retries=0) == TOKEN_ENDPOINT


def test_resolve_token_endpoint_falls_back_when_field_missing():
    sess = _SeqSession([_Resp(200, payload={"issuer": "x"})])   # no token_endpoint
    assert resolve_token_endpoint("production", session=sess) == TOKEN_ENDPOINT
