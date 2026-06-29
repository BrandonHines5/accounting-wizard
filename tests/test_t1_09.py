"""T1-09 — Payment without a matching invoice.

Isolated fixture (its own transactions frame) so the bills these scenarios need
don't perturb the shared end-to-end fixture or the other rules' exact counts.
The rule is invoked directly to keep the assertions about T1-09 alone.
"""
import pandas as pd

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
