"""T1-09 — Payment without a matching invoice.

Isolated fixture (its own transactions frame) so the bills these scenarios need
don't perturb the shared end-to-end fixture or the other rules' exact counts.
The rule is invoked directly to keep the assertions about T1-09 alone.
"""
import pandas as pd

from core.config import RulesConfig
from core.model import TRANSACTION_COLUMNS, VENDOR_COLUMNS, validate_transactions
from rules.billing import payment_without_matching_invoice
from rules.engine import RunContext

_BASE = {c: None for c in TRANSACTION_COLUMNS}


def _txn(source_id, vendor, txn_type, date, amount):
    return {**_BASE, "entity_id": "alpha", "source_system": "qb",
            "source_id": source_id, "vendor_name": vendor, "txn_type": txn_type,
            "date": date, "amount": amount, "check_no": source_id}


def _ctx(rows, registry, config) -> RunContext:
    known = {e.id for e in registry}
    txns = validate_transactions(pd.DataFrame(rows, columns=TRANSACTION_COLUMNS), known)
    vendors = pd.DataFrame(columns=VENDOR_COLUMNS)
    return RunContext(transactions=txns, vendors=vendors, registry=registry, config=config)


def _run(rows, registry, config):
    return list(payment_without_matching_invoice(_ctx(rows, registry, config)))


def test_single_invoice_match_is_clean(registry, config):
    rows = [
        _txn("B1", "Match Co", "bill", "2026-05-01", 1000.00),
        _txn("P1", "Match Co", "bill_payment", "2026-05-10", 1000.00),
    ]
    assert _run(rows, registry, config) == []


def test_payment_summing_multiple_invoices_is_clean(registry, config):
    # One check pays two invoices (300 + 700 = 1000) — the multi-invoice case.
    rows = [
        _txn("B1", "Batch Co", "bill", "2026-05-01", 300.00),
        _txn("B2", "Batch Co", "bill", "2026-05-02", 700.00),
        _txn("P1", "Batch Co", "bill_payment", "2026-05-10", 1000.00),
    ]
    assert _run(rows, registry, config) == []


def test_payment_with_no_matching_invoice_is_flagged(registry, config):
    # $500 bill is matched by the $500 payment; the later $900 payment ties to no
    # invoice and exceeds what's outstanding → flagged.
    rows = [
        _txn("B1", "NoInvoice Co", "bill", "2026-05-01", 500.00),
        _txn("P1", "NoInvoice Co", "bill_payment", "2026-05-05", 500.00),
        _txn("P2", "NoInvoice Co", "bill_payment", "2026-05-12", 900.00),
    ]
    hits = _run(rows, registry, config)
    assert [h.transactions for h in hits] == [["P2"]]
    assert hits[0].details["vendor"] == "NoInvoice Co"
    assert str(hits[0].severity) == "MEDIUM"


def test_double_payment_of_one_invoice_is_flagged(registry, config):
    # First $1,500 payment matches the bill; the second has no invoice left.
    rows = [
        _txn("B1", "DoublePay Co", "bill", "2026-05-01", 1500.00),
        _txn("P1", "DoublePay Co", "bill_payment", "2026-05-05", 1500.00),
        _txn("P2", "DoublePay Co", "bill_payment", "2026-05-09", 1500.00),
    ]
    hits = _run(rows, registry, config)
    assert [h.transactions for h in hits] == [["P2"]]


def test_partial_progress_payment_is_not_flagged(registry, config):
    # A payment below an outstanding invoice is a plausible progress payment.
    rows = [
        _txn("B1", "Partial Co", "bill", "2026-05-01", 10000.00),
        _txn("P1", "Partial Co", "bill_payment", "2026-05-10", 4000.00),
    ]
    assert _run(rows, registry, config) == []


def test_vendor_with_no_bills_is_skipped(registry, config):
    # No invoices on file for this vendor → not an AP/invoice vendor → not our rule.
    rows = [_txn("P1", "Cash Co", "check", "2026-05-10", 2000.00)]
    assert _run(rows, registry, config) == []


def test_payment_net_of_credit_memo_is_clean(registry, config):
    # A $1,000 bill with a $200 credit memo, paid with an $800 check: the payment
    # matches no bill exactly, but fits the outstanding balance net of credits.
    rows = [
        _txn("B1", "Credit Co", "bill", "2026-05-01", 1000.00),
        _txn("C1", "Credit Co", "credit_memo", "2026-05-03", -200.00),
        _txn("P1", "Credit Co", "bill_payment", "2026-05-10", 800.00),
    ]
    assert _run(rows, registry, config) == []


def test_on_account_payment_across_bills_is_clean(registry, config):
    # $6,000 payment against three open bills (2k+2k+3k = 7k outstanding): larger
    # than any single bill and matching no combo, but within the balance —
    # on-account, not an exception.
    rows = [
        _txn("B1", "OnAcct Co", "bill", "2026-05-01", 2000.00),
        _txn("B2", "OnAcct Co", "bill", "2026-05-02", 2000.00),
        _txn("B3", "OnAcct Co", "bill", "2026-05-03", 3000.00),
        _txn("P1", "OnAcct Co", "bill_payment", "2026-05-10", 6000.00),
    ]
    assert _run(rows, registry, config) == []


def test_on_account_consumption_is_partial_not_greedy(registry, config):
    # The $6,000 on-account payment must consume exactly $6,000 of the $7,000
    # open — leaving a $1,000 remainder a follow-up payment reconciles against.
    # Greedy full-bill consumption would wrongly flag P2.
    rows = [
        _txn("B1", "Rollfwd Co", "bill", "2026-05-01", 2000.00),
        _txn("B2", "Rollfwd Co", "bill", "2026-05-02", 2000.00),
        _txn("B3", "Rollfwd Co", "bill", "2026-05-03", 3000.00),
        _txn("P1", "Rollfwd Co", "bill_payment", "2026-05-10", 6000.00),
        _txn("P2", "Rollfwd Co", "bill_payment", "2026-05-17", 1000.00),
    ]
    assert _run(rows, registry, config) == []


def test_unmatched_payments_aggregate_to_one_finding_per_vendor(registry, config):
    # Two unsupported payments to one vendor → ONE finding carrying both, so the
    # reviewer answers one question, not one per check.
    rows = [
        _txn("B1", "Agg Co", "bill", "2026-05-01", 500.00),
        _txn("P1", "Agg Co", "bill_payment", "2026-05-05", 500.00),
        _txn("P2", "Agg Co", "bill_payment", "2026-05-12", 900.00),
        _txn("P3", "Agg Co", "bill_payment", "2026-05-19", 400.00),
    ]
    hits = _run(rows, registry, config)
    assert len(hits) == 1
    assert set(hits[0].transactions) == {"P2", "P3"}
    assert hits[0].details["payments"] == 2
    assert hits[0].details["total"] == 1300.0
    assert "1,300.00" in hits[0].question


def test_future_dated_bill_within_grace_supports_payment(registry, config):
    # QBO banking-feed pattern (the Adams Pest Control case): a check applied to
    # four bills, one of which is DATED AFTER the payment (the next service
    # period's bill, entered later). Within invoice_match_future_grace_days the
    # future-dated bill counts as support, so the 4-bill combination reconciles.
    rows = [
        _txn("B1", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B2", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B3", "Pest Co", "bill", "2026-06-18", 196.01),
        _txn("B4", "Pest Co", "bill", "2026-07-02", 206.96),   # 8 days after the payment
        _txn("P1", "Pest Co", "bill_payment", "2026-06-24", 838.79),
    ]
    assert _run(rows, registry, config) == []


def test_future_dated_bill_beyond_grace_still_flags(registry, config):
    # Beyond the grace the prepayment control still works: a bill dated 11 days
    # after the payment is not support, so the payment can't fully reconcile.
    rows = [
        _txn("B1", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B2", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B3", "Pest Co", "bill", "2026-06-18", 196.01),
        _txn("B4", "Pest Co", "bill", "2026-07-05", 206.96),   # 11 days after → outside grace
        _txn("P1", "Pest Co", "bill_payment", "2026-06-24", 838.79),
    ]
    hits = _run(rows, registry, config)
    assert len(hits) == 1 and hits[0].rule_id == "T1-09"
    assert hits[0].transactions == ["P1"]


def test_future_dated_bill_at_exact_grace_boundary_supports_payment(registry, config):
    # Inclusive boundary: a bill dated EXACTLY invoice_match_future_grace_days
    # (10) after the payment still counts as support.
    rows = [
        _txn("B1", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B2", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B3", "Pest Co", "bill", "2026-06-18", 196.01),
        _txn("B4", "Pest Co", "bill", "2026-07-04", 206.96),   # exactly 10 days after
        _txn("P1", "Pest Co", "bill_payment", "2026-06-24", 838.79),
    ]
    assert _run(rows, registry, config) == []


def test_future_grace_is_configurable(registry, config):
    # The grace is a rules.yaml knob, not a constant: tightened to 3 days, the
    # +8-day bill that the default (10) accepts is no longer support.
    tight = RulesConfig({"defaults": {**config.defaults,
                                      "invoice_match_future_grace_days": 3}})
    rows = [
        _txn("B1", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B2", "Pest Co", "bill", "2026-06-18", 217.91),
        _txn("B3", "Pest Co", "bill", "2026-06-18", 196.01),
        _txn("B4", "Pest Co", "bill", "2026-07-02", 206.96),   # +8 days: ok at 10, not at 3
        _txn("P1", "Pest Co", "bill_payment", "2026-06-24", 838.79),
    ]
    assert _run(rows, registry, config) == []
    hits = _run(rows, registry, tight)
    assert len(hits) == 1 and hits[0].rule_id == "T1-09"
