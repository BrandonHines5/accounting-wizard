"""Bank-verified auto-resolution of low-dollar duplicate findings (bank/auto_resolve.py).

The evidence bar is deliberately high: a CRITICAL duplicate is only auto-resolved
when Tier 4 shows every payment cleared as its OWN distinct debit, spaced like
recurring bills, under the ceiling — and never for a fraud-signal rule or a finding
a human already touched. Everything the evidence can't confirm stays on the list.
"""
import pandas as pd
import pytest

from bank.auto_resolve import auto_resolve_bank_verified
from bank.model import validate_bank_transactions
from core.config import RulesConfig
from core.findings import Disposition, Finding, Severity
from persistence.findings_store import InMemoryFindingsStore


def _books(rows, entity="alpha") -> pd.DataFrame:
    """rows: (source_id, txn_type, date, amount, check_no). Amounts are signed
    (QB exports disbursements negative), matching the real canonical model."""
    df = pd.DataFrame(rows, columns=["source_id", "txn_type", "date", "amount", "check_no"])
    df["entity_id"] = entity
    df["date"] = pd.to_datetime(df["date"])
    df["vendor_name"] = "Utility Billing Services"
    df["invoice_no"] = "20276531"
    return df


def _bank(rows, registry, entity="alpha", fp="acct-hash-1") -> pd.DataFrame:
    """rows: (amount, date, description, check_no)."""
    df = pd.DataFrame(rows, columns=["amount", "date", "description", "check_no"])
    df["entity_id"] = entity
    df["account_fingerprint"] = fp
    return validate_bank_transactions(df, {e.id for e in registry})


def _dup(ids, rule="T1-01", entity="alpha", amount=66.92) -> Finding:
    return Finding(
        rule_id=rule, severity=Severity.CRITICAL, entity_ids=[entity],
        question=f"Utility Billing Services document 20276531 appears {len(ids)} times.",
        details={"vendor": "Utility Billing Services", "amount": amount,
                 "invoice_no": "20276531"},
        transactions=[str(i) for i in ids])


def _cfg(config, **overrides) -> RulesConfig:
    """Real defaults (ceiling 100 / spacing 20 / tolerances) with test overrides."""
    return RulesConfig({"defaults": {**config.defaults, **overrides}})


# The paradigmatic case: a $66.92 utility billed monthly, cleared twice a month
# apart as two distinct ACH debits → two real payments, auto-resolved.
def test_two_distinct_monthly_clears_are_resolved(registry, config):
    books = _books([("VT-17134", "bill_payment", "2026-04-08", -66.92, ""),
                    ("VT-17163", "bill_payment", "2026-05-08", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "UTILITY BILLING SVCS ACH", ""),
                  (-66.92, "2026-05-08", "UTILITY BILLING SVCS ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-17134", "VT-17163"])], books, bank, config)

    assert kept == []
    assert len(resolved) == 1
    f = resolved[0]
    assert f.disposition == Disposition.LEGIT
    assert f.details["auto_resolved"] is True
    assert f.details["cleared_dates"] == ["2026-04-08", "2026-05-08"]
    assert f.details["dispositioned_by"] == "auto:bank-verified"
    assert "separate bank debits" in f.details["auto_resolution"]


# The real-world state of this very finding: the April statement is in, but the
# May statement (covering the second clear) hasn't been uploaded — only one clear
# is found, so it is NOT resolved and stays for the human.
def test_missing_second_statement_keeps_finding(registry, config):
    books = _books([("VT-17134", "bill_payment", "2026-04-08", -66.92, ""),
                    ("VT-17163", "bill_payment", "2026-05-08", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "UTILITY BILLING SVCS ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-17134", "VT-17163"])], books, bank, config)

    assert resolved == []
    assert len(kept) == 1 and kept[0].disposition == Disposition.OPEN


# Two clears three days apart is the shape of a genuine same-week double-pay — the
# thing we must NOT auto-clear — so it stays for human review.
def test_same_week_double_pay_is_not_resolved(registry, config):
    books = _books([("VT-1", "bill_payment", "2026-04-08", -66.92, ""),
                    ("VT-2", "bill_payment", "2026-04-10", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "UTILITY BILLING SVCS ACH", ""),
                  (-66.92, "2026-04-10", "UTILITY BILLING SVCS ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"])], books, bank, config)

    assert resolved == []
    assert len(kept) == 1


# Above the low-dollar ceiling → always reaches the human, even if bank-confirmed.
def test_over_ceiling_is_not_resolved(registry, config):
    books = _books([("VT-1", "bill_payment", "2026-04-08", -150.00, ""),
                    ("VT-2", "bill_payment", "2026-05-08", -150.00, "")])
    bank = _bank([(-150.00, "2026-04-08", "VENDOR ACH", ""),
                  (-150.00, "2026-05-08", "VENDOR ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"], amount=150.00)], books, bank, config)

    assert resolved == []
    assert len(kept) == 1


# The checked-payment path: two distinct checks clearing a month apart resolve too.
def test_check_number_path_resolves(registry, config):
    books = _books([("VT-1", "check", "2026-04-08", -66.92, "501"),
                    ("VT-2", "check", "2026-05-08", -66.92, "502")])
    bank = _bank([(-66.92, "2026-04-09", "CHECK 501", "501"),
                  (-66.92, "2026-05-09", "CHECK 502", "502")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"])], books, bank, config)

    assert kept == [] and len(resolved) == 1


# A fraud-signal rule is NEVER auto-resolved, even with two clean distinct clears —
# a bank clear says nothing about a changed vendor bank account.
def test_fraud_signal_rule_never_resolved(registry, config):
    books = _books([("VT-1", "ach", "2026-04-08", -66.92, ""),
                    ("VT-2", "ach", "2026-05-08", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "VENDOR ACH", ""),
                  (-66.92, "2026-05-08", "VENDOR ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"], rule="T1-14")], books, bank, config)

    assert resolved == []
    assert len(kept) == 1


# A duplicate a human already dispositioned is left exactly as they left it.
def test_human_disposition_wins(registry, config):
    books = _books([("VT-1", "bill_payment", "2026-04-08", -66.92, ""),
                    ("VT-2", "bill_payment", "2026-05-08", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "VENDOR ACH", ""),
                  (-66.92, "2026-05-08", "VENDOR ACH", "")], registry)
    finding = _dup(["VT-1", "VT-2"])
    prior = pd.DataFrame([{"fingerprint": finding.fingerprint(),
                           "disposition": "escalated", "rule_id": "T1-01"}])
    kept, resolved = auto_resolve_bank_verified(
        [finding], books, bank, config, prior=prior)

    assert resolved == []
    assert len(kept) == 1


# A bill (not yet paid) can't clear the bank, so a duplicate touching one is never
# confirmed here.
def test_bill_type_not_confirmable(registry, config):
    books = _books([("VT-1", "bill", "2026-04-08", 66.92, ""),
                    ("VT-2", "bill", "2026-05-08", 66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "VENDOR ACH", ""),
                  (-66.92, "2026-05-08", "VENDOR ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"])], books, bank, config)

    assert resolved == []
    assert len(kept) == 1


def test_disabled_when_ceiling_zero(registry, config):
    books = _books([("VT-1", "bill_payment", "2026-04-08", -66.92, ""),
                    ("VT-2", "bill_payment", "2026-05-08", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "VENDOR ACH", ""),
                  (-66.92, "2026-05-08", "VENDOR ACH", "")], registry)
    kept, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"])], books, bank, _cfg(config, auto_resolve_max_amount=0))

    assert resolved == []
    assert len(kept) == 1


def test_no_bank_data_is_noop(registry, config):
    books = _books([("VT-1", "bill_payment", "2026-04-08", -66.92, "")])
    kept, resolved = auto_resolve_bank_verified([_dup(["VT-1", "VT-2"])], books, None, config)
    assert resolved == [] and len(kept) == 1


# The register label (masked last-4) is threaded into the evidence note.
def test_register_label_in_note(registry, config):
    books = _books([("VT-1", "bill_payment", "2026-04-08", -66.92, ""),
                    ("VT-2", "bill_payment", "2026-05-08", -66.92, "")])
    bank = _bank([(-66.92, "2026-04-08", "VENDOR ACH", ""),
                  (-66.92, "2026-05-08", "VENDOR ACH", "")], registry)
    _, resolved = auto_resolve_bank_verified(
        [_dup(["VT-1", "VT-2"])], books, bank, config,
        account_labels={"acct-hash-1": "…0452"})
    assert "[Register: …0452]" in resolved[0].details["auto_resolution"]


# --- persistence of the auto-disposition -------------------------------------

def test_persist_auto_disposition_sets_legit():
    store = InMemoryFindingsStore()
    f = _dup(["VT-1", "VT-2"])
    f.disposition = Disposition.LEGIT
    f.details["auto_resolution"] = "Auto-resolved (bank-verified): …"
    f.details["dispositioned_by"] = "auto:bank-verified"

    store.save([f])
    store.persist_auto_dispositions([f])

    prior = store.load_prior()
    row = prior[prior["fingerprint"] == f.fingerprint()].iloc[0]
    assert row["disposition"] == "legit"
    assert row["dispositioned_by"] == "auto:bank-verified"


def test_persist_auto_disposition_never_overwrites_human():
    f = _dup(["VT-1", "VT-2"])
    store = InMemoryFindingsStore(
        prior=[{"fingerprint": f.fingerprint(), "rule_id": "T1-01",
                "disposition": "escalated", "details": {}}])
    f.disposition = Disposition.LEGIT
    f.details["auto_resolution"] = "Auto-resolved (bank-verified): …"

    store.save([f])                       # ignore_duplicates — no-op on the existing row
    store.persist_auto_dispositions([f])  # guarded to open rows only

    prior = store.load_prior()
    row = prior[prior["fingerprint"] == f.fingerprint()].iloc[0]
    assert row["disposition"] == "escalated"


class _FakeTable:
    """Captures the Supabase write shape (payload + .eq filters) without a client."""
    def __init__(self):
        self.payload = None
        self.filters = []

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def execute(self):
        return None


def test_supabase_persist_auto_disposition_shape_and_open_guard():
    # The production adapter must UPDATE disposition=legit stamped with the auto
    # provenance + a note + dispositioned_at, and guard the write to still-open
    # rows via .eq("disposition", "open") so a human call is never overwritten.
    from persistence.supabase_store import SupabaseFindingsStore
    store = SupabaseFindingsStore.__new__(SupabaseFindingsStore)
    store._table = _FakeTable()
    f = _dup(["VT-1", "VT-2"])
    f.disposition = Disposition.LEGIT
    f.details["auto_resolution"] = "Auto-resolved (bank-verified): cleared 2026-04-08, 2026-05-08"
    f.details["dispositioned_by"] = "auto:bank-verified"

    store.persist_auto_dispositions([f])

    payload = store._table.payload
    assert payload["disposition"] == "legit"
    assert payload["dispositioned_by"] == "auto:bank-verified"
    assert payload["disposition_note"]        # non-empty evidence note
    assert payload["dispositioned_at"]        # stamped
    assert ("fingerprint", f.fingerprint()) in store._table.filters
    assert ("disposition", "open") in store._table.filters   # the human-wins guard
