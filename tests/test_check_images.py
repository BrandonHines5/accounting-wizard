"""Tier 4 check-image vision reads (T4-03/04/05) over a planted scenario set,
driven by a fake reader so the read→books comparison is fully deterministic."""
import pandas as pd
import pytest

from bank.check_images import CheckRead, CheckReader, verify_check_images
from bank.model import validate_bank_transactions


class FakeReader(CheckReader):
    """Returns a canned CheckRead keyed by the (decoded) front-image bytes."""

    def __init__(self, by_token):
        self._by_token = by_token
        self.calls = 0

    def read_check(self, *, front, back=None, media_type="image/jpeg"):
        self.calls += 1
        return self._by_token[front.decode()]


def _books() -> pd.DataFrame:
    rows = [
        ("TX-1", "check", "Acme Lumber", 500.00, "2001"),
        ("TX-2", "check", "Smith Electric", 1200.00, "2002"),
        ("TX-3", "check", "Roof Pros", 900.00, "2003"),
        ("TX-4", "check", "QuickPour", 400.00, "2004"),
        ("TX-5", "check", "CloudCo", 1500.00, "2005"),
    ]
    df = pd.DataFrame(rows, columns=["source_id", "txn_type", "vendor_name", "amount", "check_no"])
    df["entity_id"] = "alpha"
    df["date"] = pd.to_datetime("2026-05-05")
    return df


def _bank(registry) -> pd.DataFrame:
    rows = [
        ("2001", "img-2001", -500.00),
        ("2002", "img-2002", -1200.00),
        ("2003", "img-2003", -900.00),
        ("2004", "img-2004", -400.00),
        ("2005", "img-2005", -1500.00),
    ]
    df = pd.DataFrame(rows, columns=["check_no", "image_ref", "amount"])
    df["entity_id"] = "alpha"
    df["account_fingerprint"] = "acct-hash-9"
    df["date"] = pd.to_datetime("2026-05-08")
    df["description"] = "CHECK"
    return validate_bank_transactions(df, {e.id for e in registry})


def _reads() -> dict:
    return {
        "img-2001": CheckRead(payee="Acme Lumber", amount=500.00, confidence=99),     # clean
        "img-2002": CheckRead(payee="QuickCash Holdings", amount=1200.00, confidence=98),  # payee
        "img-2003": CheckRead(payee="Roof Pros", amount=1900.00, confidence=97),      # altered amt
        "img-2004": CheckRead(payee="QuickPour", amount=400.00, confidence=72),       # unreadable
        "img-2005": CheckRead(payee="CloudCo", amount=1500.00, confidence=99,
                              endorsement="John Doe", endorsement_flags=("double_endorsement",)),
    }


@pytest.fixture
def result(registry, config):
    reader = FakeReader(_reads())
    return verify_check_images(_bank(registry), _books(), reader, registry, config,
                               fetch_front=lambda ref: ref.encode())


def by_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def test_total_findings(result):
    _, findings = result
    # 2002 payee, 2003 amount, 2004 low-confidence, 2005 endorsement; 2001 is clean
    assert len(findings) == 4


def test_payee_mismatch_is_critical(result):
    _, findings = result
    crit = [f for f in by_rule(findings, "T4-03") if str(f.severity) == "CRITICAL"]
    assert len(crit) == 1
    d = crit[0].details
    assert d["check_no"] == "2002"
    assert d["read_payee"] == "QuickCash Holdings" and d["recorded_vendor"] == "Smith Electric"


def test_amount_alteration_is_critical(result):
    _, findings = result
    t404 = by_rule(findings, "T4-04")
    assert len(t404) == 1
    assert t404[0].details["read_amount"] == 1900.0 and t404[0].details["recorded"] == 900.0


def test_low_confidence_routes_to_review_queue(result):
    _, findings = result
    review = [f for f in by_rule(findings, "T4-03")
              if f.details.get("image_review") == "low_confidence"]
    assert len(review) == 1
    assert review[0].details["check_no"] == "2004" and str(review[0].severity) == "MEDIUM"


def test_endorsement_anomaly_is_high(result):
    _, findings = result
    t405 = by_rule(findings, "T4-05")
    assert len(t405) == 1 and str(t405[0].severity) == "HIGH"
    assert "double_endorsement" in t405[0].details["endorsement_flags"]


def test_reads_enrich_the_bank_frame(result):
    bank, _ = result
    by_check = bank.set_index("check_no")
    assert by_check.loc["2002", "payee_read"] == "QuickCash Holdings"
    assert by_check.loc["2003", "amount_read"] == 1900.0
    assert by_check.loc["2004", "read_confidence"] == 72


def test_clean_check_produces_nothing(result):
    _, findings = result
    assert all(f.details.get("check_no") != "2001" for f in findings)


def test_non_check_rows_are_skipped(registry, config):
    df = pd.DataFrame([{"entity_id": "alpha", "account_fingerprint": "h", "date": "2026-05-08",
                        "description": "DEPOSIT", "amount": 2000.0, "check_no": ""}])
    bank = validate_bank_transactions(df, {e.id for e in registry})
    reader = FakeReader({})
    _, findings = verify_check_images(bank, _books(), reader, registry, config,
                                      fetch_front=lambda ref: ref.encode())
    assert findings == [] and reader.calls == 0


def test_bill_sharing_check_number_never_shadows_the_real_check(registry, config):
    """Regression (check #8108): a vendor bill can carry the same document number in
    QB's 'Num' column as an unrelated payment. The cancelled check must be compared
    against the *payment* that actually cleared, never the like-numbered bill — else
    the image (correctly reading the payment amount) trips a false T4-04 alteration
    against the bill's amount. The bill is listed FIRST so a naive first-match would
    grab it."""
    books = pd.DataFrame(
        [
            # Bill from Affordable Gutters, invoice no. 8108 — leaks into check_no.
            ("BILL-8108", "bill", "Affordable Gutters", 2808.00, "8108"),
            # The real check: bill payment to Yesenia Rivera, check no. 8108.
            ("PMT-8108", "bill_payment", "Yesenia Rivera", 12197.00, "8108"),
        ],
        columns=["source_id", "txn_type", "vendor_name", "amount", "check_no"],
    )
    books["entity_id"] = "alpha"
    books["date"] = pd.to_datetime("2026-04-22")

    bank = pd.DataFrame([("8108", "img-8108", -12197.00)],
                        columns=["check_no", "image_ref", "amount"])
    bank["entity_id"] = "alpha"
    bank["account_fingerprint"] = "acct-hash-9"
    bank["date"] = pd.to_datetime("2026-04-22")
    bank["description"] = "CHECK"
    bank = validate_bank_transactions(bank, {e.id for e in registry})

    reader = FakeReader({"img-8108": CheckRead(payee="Yesenia Rivera",
                                               amount=12197.00, confidence=99)})
    _, findings = verify_check_images(bank, books, reader, registry, config,
                                      fetch_front=lambda ref: ref.encode())

    # Image matches the payment on both payee and amount → no finding at all, and
    # certainly no T4-04 alteration comparing $12,197 read against the bill's $2,808.
    assert findings == []
