"""Tier 4 three-way match over a planted bank ↔ books scenario set."""
import pandas as pd
import pytest

from bank.model import BANK_COLUMNS, validate_bank_transactions
from bank.reconcile import reconcile, reconcile_all, reconcile_deposits


def _books() -> pd.DataFrame:
    rows = [
        # source_id, txn_type, date, vendor, amount, check_no
        ("TX-0", "check", "2026-04-03", "Early Vendor", 250.00, "1000"),  # clean match (early)
        ("TX-1", "check", "2026-05-05", "Acme Lumber", 500.00, "1001"),   # clean match
        ("TX-2", "check", "2026-05-08", "Smith Electric", 1200.00, "1002"),  # altered amount
        ("TX-3", "check", "2026-04-10", "Roof Pros", 900.00, "1003"),     # book-only (stale-outstanding)
        ("TX-4", "check", "2026-04-01", "QuickPour", 400.00, "1004"),     # long clearing gap
        ("TX-5", "ach", "2026-05-16", "CloudCo", 1500.00, ""),            # non-check, matched
    ]
    df = pd.DataFrame(rows, columns=["source_id", "txn_type", "date",
                                     "vendor_name", "amount", "check_no"])
    df["entity_id"] = "alpha"
    df["date"] = pd.to_datetime(df["date"])
    return df


def _bank(registry) -> pd.DataFrame:
    rows = [
        # amount (signed), date, description, check_no
        (-250.00, "2026-04-05", "CHECK 1000", "1000"),     # clean match — extends the window
        (-500.00, "2026-05-07", "CHECK 1001", "1001"),     # clean match (gap 2)
        (-1300.00, "2026-05-10", "CHECK 1002", "1002"),    # T4-04 altered
        (-700.00, "2026-05-12", "CHECK 1009", "1009"),     # T4-02 unrecorded
        (-400.00, "2026-05-20", "CHECK 1004", "1004"),     # T4-06 gap (49d)
        (-2500.00, "2026-05-18", "WIRE TRANSFER OUT", ""),  # T4-09 unmatched non-check
        (-1500.00, "2026-05-17", "ACH PMT CLOUDCO", ""),   # matched non-check
        (5000.00, "2026-05-19", "DEPOSIT", ""),            # inflow — ignored this slice
    ]
    df = pd.DataFrame(rows, columns=["amount", "date", "description", "check_no"])
    df["entity_id"] = "alpha"
    df["account_fingerprint"] = "acct-hash-1"
    return validate_bank_transactions(df, {e.id for e in registry})


@pytest.fixture
def findings(registry, config):
    return reconcile(_books(), _bank(registry), registry, config)


def by_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


def test_total_and_severity_mix(findings):
    assert len(findings) == 5
    sev = [str(f.severity) for f in findings]
    assert sev.count("CRITICAL") == 3      # T4-04, T4-02 unrecorded, T4-09
    assert sev.count("HIGH") == 1          # T4-02 book-only
    assert sev.count("MEDIUM") == 1        # T4-06 clearing gap


def test_amount_alteration_flagged(findings):
    alt = by_rule(findings, "T4-04")
    assert len(alt) == 1
    assert alt[0].details["cleared"] == 1300.0 and alt[0].details["recorded"] == 1200.0


def test_unrecorded_and_outstanding(findings):
    t402 = by_rule(findings, "T4-02")
    by_check = {f.details["check_no"]: str(f.severity) for f in t402}
    assert by_check["1009"] == "CRITICAL"   # cleared, no book entry
    assert by_check["1003"] == "HIGH"       # recorded, never cleared


def test_recorded_check_outside_bank_window_not_flagged(registry, config):
    # A check recorded months before the earliest statement can't be confirmed
    # cleared from the data we hold, so it must NOT be flagged "never cleared".
    extra = pd.DataFrame([{"source_id": "TX-OLD", "txn_type": "check",
                           "date": pd.Timestamp("2026-01-02"), "vendor_name": "Old Vendor",
                           "amount": 750.0, "check_no": "0500", "entity_id": "alpha"}])
    books = pd.concat([_books(), extra], ignore_index=True)
    findings = reconcile(books, _bank(registry), registry, config)
    flagged = {f.details.get("check_no") for f in by_rule(findings, "T4-02")}
    assert "0500" not in flagged            # out of bank window → not flagged
    assert "1003" in flagged                # in-window outstanding → still flagged


def test_bank_side_findings_carry_cleared_date(findings):
    crit = next(f for f in by_rule(findings, "T4-02") if str(f.severity) == "CRITICAL")
    assert crit.details["cleared_date"] == "2026-05-12"   # bank clear date of check 1009
    assert by_rule(findings, "T4-09")[0].details["cleared_date"] == "2026-05-18"


def test_non_check_sweep_and_gap(findings):
    sweep = by_rule(findings, "T4-09")
    assert len(sweep) == 1 and sweep[0].details["amount"] == 2500.0
    gap = by_rule(findings, "T4-06")
    assert len(gap) == 1 and gap[0].details["gap_days"] == 49


def test_clean_matches_produce_nothing(findings):
    # check 1001 and the CloudCo ACH both reconcile cleanly; the deposit is ignored
    assert all(f.details.get("check_no") != "1001" for f in findings)
    assert not by_rule(findings, "T4-07")   # no deposit-side findings this slice


def test_check_number_collision_across_accounts_pairs_by_amount(registry, config):
    # Check #7946 exists on two of the entity's accounts: $9,536.70 (Ozk) and
    # $1,500 (operating). The books carry both. Amount-aware matching pairs each
    # cleared line to the right entry, so no false T4-04 'altered check' fires —
    # the real-world Any Y Jurado case.
    books = pd.DataFrame(
        [("OZK", "check", "2026-05-29", "Any Y Jurado", 9536.70, "7946"),
         ("OPS", "check", "2026-05-27", "Other Payee", 1500.00, "7946")],
        columns=["source_id", "txn_type", "date", "vendor_name", "amount", "check_no"])
    books["entity_id"] = "alpha"
    books["date"] = pd.to_datetime(books["date"])
    rows = [(-9536.70, "2026-05-29", "Withdrawal", "7946"),     # cleared Ozk
            (-1500.00, "2026-05-28", "CHECK 7946", "7946")]     # cleared operating
    bank = pd.DataFrame(rows, columns=["amount", "date", "description", "check_no"])
    bank["entity_id"] = "alpha"
    bank["account_fingerprint"] = "acct"
    bank = validate_bank_transactions(bank, {e.id for e in registry})
    assert reconcile(books, bank, registry, config) == []


def _deposit_books() -> pd.DataFrame:
    rows = [
        # source_id, entity_id, txn_type, date, amount
        ("DEP-1", "alpha",   "deposit", "2026-05-05", 2000.00),  # clean match
        ("DEP-2", "alpha",   "deposit", "2026-05-10", 3500.00),  # T4-07 missing
        ("DON-1", "charity", "deposit", "2026-05-06", 1000.00),  # clean match
        ("DON-2", "charity", "deposit", "2026-05-12", 5000.00),  # T4-08 missing donation
    ]
    df = pd.DataFrame(rows, columns=["source_id", "entity_id", "txn_type", "date", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _deposit_bank(registry) -> pd.DataFrame:
    rows = [
        # entity_id, amount (signed +), date, description
        ("alpha",   2000.00, "2026-05-07", "MOBILE DEPOSIT"),  # matches DEP-1 (gap 2)
        ("alpha",    750.00, "2026-05-15", "DEPOSIT"),         # T4-07 unrecorded (MEDIUM)
        ("charity", 1000.00, "2026-05-08", "DONATION ACH"),    # matches DON-1 (gap 2)
        ("charity",  900.00, "2026-05-14", "DEPOSIT"),         # T4-08 unrecorded (HIGH)
    ]
    df = pd.DataFrame(rows, columns=["entity_id", "amount", "date", "description"])
    df["account_fingerprint"] = "acct-hash-2"
    return validate_bank_transactions(df, {e.id for e in registry})


@pytest.fixture
def deposit_findings(registry, config):
    return reconcile_deposits(_deposit_books(), _deposit_bank(registry), registry, config)


def test_deposit_total_and_registry_routing(deposit_findings):
    assert len(deposit_findings) == 4
    # alpha (llc) → T4-07, charity (501c3) → T4-08 — keyed off legal_type, not name.
    assert sorted(f.rule_id for f in deposit_findings) == ["T4-07", "T4-07", "T4-08", "T4-08"]
    for f in deposit_findings:
        assert f.rule_id == ("T4-08" if "charity" in f.entity_ids else "T4-07")


def test_missing_deposits_are_critical(deposit_findings):
    missing = {f.transactions[0]: f for f in deposit_findings if f.transactions}
    assert str(missing["DEP-2"].severity) == "CRITICAL" and missing["DEP-2"].rule_id == "T4-07"
    assert str(missing["DON-2"].severity) == "CRITICAL" and missing["DON-2"].rule_id == "T4-08"


def test_unrecorded_deposit_floors_higher_for_nonprofit(deposit_findings):
    unrecorded = {f.entity_ids[0]: str(f.severity)
                  for f in deposit_findings if not f.transactions}
    assert unrecorded["alpha"] == "MEDIUM"    # could be a transfer/loan/contribution
    assert unrecorded["charity"] == "HIGH"    # possible unrecorded donation


def test_clean_deposits_produce_nothing(deposit_findings):
    flagged = {f.details.get("amount") for f in deposit_findings}
    assert 2000.0 not in flagged and 1000.0 not in flagged


def test_unrecorded_deposits_have_distinct_fingerprints(deposit_findings):
    # the bank_ref natural key keeps transaction-less findings from colliding
    unrecorded = [f for f in deposit_findings if not f.transactions]
    assert len({f.fingerprint() for f in unrecorded}) == len(unrecorded)


def test_reconcile_all_merges_both_sides(registry, config):
    disb = reconcile(_books(), _bank(registry), registry, config)
    combined = reconcile_all(_books(), _bank(registry), registry, config)
    # _books() has no deposit rows, so the deposit side is SKIPPED (nothing to
    # match against) and the +5000 alpha inflow rolls up into exactly one INFO
    # ingest-gap note instead of a per-line unexplained-inflow finding.
    assert len(combined) == len(disb) + 1
    note = [f for f in combined if f.rule_id == "T4-07"]
    assert len(note) == 1 and str(note[0].severity) == "INFO"
    assert note[0].details["stat_key"] == "no_receipts_ingested"
    assert note[0].details["bank_deposits"] == 1
    sev = [int(f.severity) for f in combined]
    assert sev == sorted(sev, reverse=True)   # severity-sorted


def _dep_books(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["source_id", "entity_id", "txn_type", "date", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _dep_bank(registry, rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["entity_id", "amount", "date", "description"])
    df["account_fingerprint"] = "acct-batch"
    return validate_bank_transactions(df, {e.id for e in registry})


def test_batched_deposit_is_absorbed(registry, config):
    books = _dep_books([
        ("R1", "alpha", "deposit", "2026-05-05", 100.00),
        ("R2", "alpha", "deposit", "2026-05-05", 200.00),
        ("R3", "alpha", "deposit", "2026-05-06", 300.00),
    ])
    bank = _dep_bank(registry, [("alpha", 600.00, "2026-05-07", "BATCH DEPOSIT")])
    # 100+200+300 = 600 → recognized as one batched deposit, no false 'missing'.
    assert reconcile_deposits(books, bank, registry, config) == []


def test_short_batch_flags_only_the_shortfall(registry, config):
    books = _dep_books([
        ("R1", "alpha", "deposit", "2026-05-05", 100.00),
        ("R2", "alpha", "deposit", "2026-05-05", 200.00),
        ("R3", "alpha", "deposit", "2026-05-06", 300.00),
    ])
    bank = _dep_bank(registry, [("alpha", 500.00, "2026-05-07", "DEPOSIT")])
    findings = reconcile_deposits(books, bank, registry, config)
    # {200,300}=500 clears; the $100 receipt is the only short/missing piece.
    assert len(findings) == 1
    assert findings[0].rule_id == "T4-07" and str(findings[0].severity) == "CRITICAL"
    assert findings[0].transactions == ["R1"] and findings[0].details["amount"] == 100.0


def test_batch_leaves_real_missing_and_unrecorded(registry, config):
    books = _dep_books([
        ("R1", "alpha", "deposit", "2026-05-05", 110.00),
        ("R2", "alpha", "deposit", "2026-05-05", 205.00),
        ("R3", "alpha", "deposit", "2026-05-06", 320.00),   # 110+205+320 = 635
        ("R4", "alpha", "deposit", "2026-05-10", 400.00),   # genuinely never deposited
    ])
    bank = _dep_bank(registry, [
        ("alpha", 635.00, "2026-05-07", "BATCH"),           # = R1+R2+R3
        ("alpha", 55.00, "2026-05-12", "MYSTERY"),          # unrecorded inflow
    ])
    findings = reconcile_deposits(books, bank, registry, config)
    assert len(findings) == 2
    missing = [f for f in findings if f.transactions == ["R4"]]
    assert missing and str(missing[0].severity) == "CRITICAL"
    unrecorded = [f for f in findings if not f.transactions]
    assert unrecorded and str(unrecorded[0].severity) == "MEDIUM"
    assert unrecorded[0].details["amount"] == 55.0


def test_bank_lines_before_books_coverage_roll_up_to_one_info(registry, config):
    # A statement backfill reaching years before the earliest book transaction
    # must NOT flood the queue with CRITICALs — those lines roll up into ONE
    # INFO coverage note (T4-01) per entity/side.
    old = pd.DataFrame(
        [(-900.00, "2023-03-10", "CHECK 0107", "0107"),
         (-450.00, "2023-04-02", "ACH OLD VENDOR", ""),
         (-120.00, "2023-05-15", "DEBIT CARD PURCHASE", "")],
        columns=["amount", "date", "description", "check_no"])
    old["entity_id"] = "alpha"
    old["account_fingerprint"] = "acct-hash-1"
    bank = pd.concat([_bank(registry), validate_bank_transactions(
        old, {e.id for e in registry})], ignore_index=True)
    findings = reconcile(_books(), bank, registry, config)
    assert not [f for f in findings if f.rule_id in ("T4-02", "T4-09")
                and f.details.get("cleared_date", "9999").startswith("2023")]
    notes = [f for f in findings if f.rule_id == "T4-01"]
    assert len(notes) == 1 and str(notes[0].severity) == "INFO"
    assert notes[0].details["lines"] == 3
    assert notes[0].details["side"] == "disbursement"


def test_recycled_check_number_is_not_an_alteration(registry, config):
    # Check #1001 cleared in 2023 for a different amount than the 2026 book
    # entry with the same (recycled) number. An undated match would call that a
    # CRITICAL "alteration"; the date-constrained match must not pair them.
    recycled = pd.DataFrame([(-77.25, "2023-06-01", "CHECK 1001", "1001")],
                            columns=["amount", "date", "description", "check_no"])
    recycled["entity_id"] = "alpha"
    recycled["account_fingerprint"] = "acct-hash-1"
    bank = pd.concat([_bank(registry), validate_bank_transactions(
        recycled, {e.id for e in registry})], ignore_index=True)
    findings = reconcile(_books(), bank, registry, config)
    assert len(by_rule(findings, "T4-04")) == 1          # only the planted 1002 case
    # the 2023 line is out of books coverage → part of the INFO note, not a T4-02
    assert [f.details["lines"] for f in by_rule(findings, "T4-01")] == [1]
    # and the 2026 book entry still matched its own 2026 clearing cleanly
    assert all(f.details.get("check_no") != "1001" for f in by_rule(findings, "T4-02"))


def test_small_unmatched_lines_are_info_not_critical(registry, config):
    # In-coverage but below bank_min_critical_amount → INFO, still listed.
    small = pd.DataFrame([(-4.00, "2026-05-14", "SERVICE FEE", ""),
                          (-6.50, "2026-05-13", "CHECK 1044", "1044")],
                         columns=["amount", "date", "description", "check_no"])
    small["entity_id"] = "alpha"
    small["account_fingerprint"] = "acct-hash-1"
    bank = pd.concat([_bank(registry), validate_bank_transactions(
        small, {e.id for e in registry})], ignore_index=True)
    findings = reconcile(_books(), bank, registry, config)
    fee = [f for f in by_rule(findings, "T4-09") if f.details["amount"] == 4.0]
    chk = [f for f in by_rule(findings, "T4-02") if f.details.get("check_no") == "1044"]
    assert fee and str(fee[0].severity) == "INFO"
    assert chk and str(chk[0].severity) == "INFO"


def test_recent_uncleared_check_is_ordinary_float_not_flagged(registry, config):
    # A check recorded days before the last statement line hasn't had time to
    # clear — flagging it as "never cleared" is noise, not a finding.
    recent = pd.DataFrame([{"source_id": "TX-NEW", "txn_type": "check",
                            "date": pd.Timestamp("2026-05-15"), "vendor_name": "Fresh Vendor",
                            "amount": 640.0, "check_no": "1050", "entity_id": "alpha"}])
    books = pd.concat([_books(), recent], ignore_index=True)
    findings = reconcile(books, _bank(registry), registry, config)
    flagged = {f.details.get("check_no") for f in by_rule(findings, "T4-02")}
    assert "1050" not in flagged
    assert "1003" in flagged    # the stale one (30+ days) is still flagged


def test_journal_entry_matches_non_check_debit(registry, config):
    # QB books often record transfers/loan payments/fees as journal entries; a
    # matching journal line must absorb the bank debit instead of raising T4-09.
    books = pd.concat([_books(), pd.DataFrame([
        {"source_id": "JRN-1", "txn_type": "journal", "date": pd.Timestamp("2026-05-18"),
         "vendor_name": None, "amount": -2500.00, "check_no": "", "entity_id": "alpha"}])],
        ignore_index=True)
    findings = reconcile(books, _bank(registry), registry, config)
    assert by_rule(findings, "T4-09") == []   # the wire is explained by the journal


def _sweep_bank(registry) -> pd.DataFrame:
    # Cash Manager sweep: nightly transfer out of the operating account into the
    # linked sweep account (…4520) and the sweep-back credit. Both clear with no
    # book entry — they must be recognized as internal transfers, not flagged.
    rows = [
        (-107273.94, "2026-04-29", "Trnsfr to Account Ending in 4520", ""),
        (-249320.89, "2026-05-21", "Trnsfr to Account Ending in 4520", ""),
        (160276.82, "2026-05-19", "Trnsfr from Account Ending in 4520", ""),
    ]
    df = pd.DataFrame(rows, columns=["amount", "date", "description", "check_no"])
    df["entity_id"] = "alpha"
    df["account_fingerprint"] = "acct-hash-1"
    return validate_bank_transactions(df, {e.id for e in registry})


def test_sweep_transfers_not_flagged_as_unrecorded_disbursement(registry, config):
    # The sweep-out debits carry no book entry by design; recognized as internal
    # cash-management sweeps, they must NOT each raise a CRITICAL T4-09.
    bank = pd.concat([_bank(registry), _sweep_bank(registry)], ignore_index=True)
    findings = reconcile(_books(), bank, registry, config)
    crit_sweeps = [f for f in by_rule(findings, "T4-09")
                   if str(f.severity) == "CRITICAL"
                   and "4520" in (f.details.get("description") or "")]
    assert crit_sweeps == []                       # no per-line CRITICALs for the sweep
    note = [f for f in by_rule(findings, "T4-09") if f.details.get("stat_key")]
    assert len(note) == 1 and str(note[0].severity) == "INFO"
    assert note[0].details["stat_key"] == "internal_sweep|disbursement"
    assert note[0].details["lines"] == 2           # two sweep-out debits summarized
    # the genuine unmatched non-check debit (the planted wire) still fires
    assert any(f.details.get("amount") == 2500.0 for f in by_rule(findings, "T4-09"))


def test_sweep_note_absent_when_no_sweep_lines(findings):
    # Nothing in the base scenario matches a sweep pattern, so no INFO sweep note.
    assert not [f for f in findings if f.details.get("stat_key", "").startswith("internal_sweep")]


def test_sweep_back_credit_not_flagged_as_unrecorded_inflow(registry, config):
    # An entity WITH book receipts: the sweep-back credit must be recognized as an
    # internal transfer, not raised as a T4-07 unexplained inflow.
    books = _dep_books([("R1", "alpha", "deposit", "2026-05-05", 2000.00)])
    bank = _dep_bank(registry, [
        ("alpha", 2000.00, "2026-05-07", "MOBILE DEPOSIT"),               # matches R1
        ("alpha", 160276.82, "2026-05-19", "Trnsfr from Account Ending in 4520"),
    ])
    findings = reconcile_deposits(books, bank, registry, config)
    assert not [f for f in findings if f.details.get("amount") == 160276.82]
    note = [f for f in findings if f.details.get("stat_key") == "internal_sweep|deposit"]
    assert len(note) == 1 and str(note[0].severity) == "INFO" and note[0].rule_id == "T4-07"


def test_validate_rejects_unknown_entity(registry):
    bad = pd.DataFrame([{"entity_id": "ghost", "account_fingerprint": "x",
                         "date": "2026-05-01", "amount": -10.0}])
    with pytest.raises(ValueError):
        validate_bank_transactions(bad, {e.id for e in registry})


def test_validate_requires_core_columns(registry):
    with pytest.raises(ValueError):
        validate_bank_transactions(pd.DataFrame([{"entity_id": "alpha"}]),
                                   {e.id for e in registry})
