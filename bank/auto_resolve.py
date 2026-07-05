"""Tier 4 → Tier 1 bank-verified auto-resolution of low-dollar duplicate findings.

A duplicate-payment finding (T1-01 exact, T1-02 fuzzy) fires from the BOOKS alone:
the same document/amount appears twice, and the deterministic rule cannot tell two
legitimate recurring bills (a utility that reuses its account number every month)
from one obligation paid twice. The bank statement is the independent source that
settles it — the question the Tier 3 review keeps recommending ("check the bank
statement; did both clear on different dates?").

This module answers that question automatically, but only from hard evidence. A
finding is auto-resolved ONLY when Tier 4 confirms that EVERY involved book payment
cleared the bank as its OWN distinct debit AND those clears are spaced like
recurring bills (>= `auto_resolve_min_spacing_days` apart), not a same-week
double-pay — and only when every payment is at or below `auto_resolve_max_amount`.

It is deliberately NOT a dollar-threshold suppressor. A CRITICAL is never silently
dropped (CLAUDE.md): an auto-resolved finding is dispositioned `legit` WITH the
bank evidence attached, returned for the "Auto-resolved (verified)" workbook sheet,
and persisted in the findings history — visible, auditable, and reversible. Anything
the evidence can't confirm (statement not ingested yet, only one clear found, clears
too close together, amount over the ceiling) stays on the human's list, unchanged.

Fraud-signal rules (bank-detail changes, payee mismatches, vendor/employee overlap,
…) are never eligible: a bank clear proves money left the account, which disproves a
*duplicate-payment* worry but says nothing about those other schemes.
"""
from __future__ import annotations

import pandas as pd

from core.config import RulesConfig
from core.findings import Disposition, Finding

# Only the duplicate-payment family is eligible. A cleared bank debit is evidence
# specifically about whether a payment really happened (and thus whether an
# apparent duplicate is two real payments) — it is not evidence about miscoding,
# approval bypass, or a changed vendor bank account. Those stay for human review.
AUTO_RESOLVE_RULES = frozenset({"T1-01", "T1-02"})

# Book payment types that clear against this bank account as a debit. A finding
# touching a `bill` (not yet paid) or a `card` charge (a different statement)
# can't be confirmed here, so it is never auto-resolved.
_CLEARING_TYPES = frozenset({"check", "ach", "wire", "bill_payment"})


def _norm_check(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _dispositioned_fingerprints(prior: pd.DataFrame | None) -> set:
    """Fingerprints in history that already carry a NON-open (human) disposition —
    a person's decision always wins over the automation, so these are never
    auto-resolved. Computed once per run so the candidate loop stays O(findings),
    not O(findings × history) as the history table grows."""
    if prior is None or len(prior) == 0 or "fingerprint" not in prior.columns \
            or "disposition" not in prior.columns:
        return set()
    mask = ~prior["disposition"].astype(str).str.strip().str.lower().isin(
        ("", "open", "none", "nan"))
    return set(prior.loc[mask, "fingerprint"])


def _distinct_clears(involved: pd.DataFrame, disb: pd.DataFrame,
                     amount_tol: float, date_tol: int, check_max_days: int):
    """Match each involved book payment to a DISTINCT bank debit line.

    Returns the list of matched bank index labels (one per involved payment, each
    bank line claimed at most once) when EVERY payment matched, else None — a
    partial match (e.g. only one of two clears is on an ingested statement) is not
    a confirmation."""
    used: list = []
    # Date-ordered so the greedy nearest-clear claim is deterministic and doesn't
    # depend on the book frame's row order (an earlier row can otherwise claim a
    # later row's better match and fail an otherwise-confirmable duplicate).
    for _, tx in involved.sort_values("date").iterrows():
        amt = abs(float(tx["amount"]))
        cands = disb[~disb.index.isin(used)
                     & ((disb["_amt"] - amt).abs() <= amount_tol)]
        check = _norm_check(tx.get("check_no"))
        if check:
            # A checked payment must match by check number within the clearing
            # window (numbers recycle over an account's life).
            gap = (cands["date"] - tx["date"]).dt.days
            cands = cands[(cands["_check"] == check) & gap.between(-date_tol, check_max_days)]
        else:
            # A non-check payment (ACH/e-payment) matches by amount + date proximity.
            cands = cands[((cands["date"] - tx["date"]).abs().dt.days <= date_tol)]
        if cands.empty:
            return None
        used.append((cands["date"] - tx["date"]).abs().idxmin())  # nearest clear
    return used


def _mark_resolved(finding: Finding, matched: pd.DataFrame,
                   ceiling: float, register: str | None) -> None:
    """Disposition the finding `legit` with the bank evidence attached (never a
    silent drop): the distinct cleared dates, the amount(s), the spacing, and the
    register. Fuzzy (T1-02) duplicates can clear at amounts differing within the
    match tolerance, so the note reports every distinct cleared amount rather than
    assuming one — the record is attached to a resolved CRITICAL."""
    dates = sorted(d.date().isoformat() for d in matched["date"])
    span = (matched["date"].max() - matched["date"].min()).days
    amounts = sorted(round(float(a), 2) for a in matched["_amt"].unique())
    amount_str = ("/".join(f"${a:,.2f}" for a in amounts) if len(amounts) > 1
                  else f"${amounts[0]:,.2f}")
    note = (
        f"Auto-resolved (bank-verified): Tier 4 confirms {amount_str} "
        f"cleared as {len(matched)} separate bank debits on {', '.join(dates)} "
        f"— {span} days apart, consistent with recurring billing, not one obligation "
        f"paid twice. Each payment is at or below the ${ceiling:,.0f} auto-resolve "
        "ceiling. Resolved automatically; reversible on review."
        + (f" [Register: {register}]" if register else ""))
    finding.disposition = Disposition.LEGIT
    finding.recommended_action = "clear"
    finding.details["auto_resolved"] = True
    finding.details["auto_resolution"] = note
    finding.details["cleared_dates"] = dates
    finding.details["dispositioned_by"] = "auto:bank-verified"


def auto_resolve_bank_verified(
    findings: list[Finding],
    transactions: pd.DataFrame,
    bank: pd.DataFrame | None,
    config: RulesConfig,
    *,
    account_labels: dict | None = None,
    prior: pd.DataFrame | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Partition `findings` into (kept, auto_resolved).

    `kept` is the active list minus anything bank-confirmed (re-sorted by severity);
    `auto_resolved` are the low-dollar duplicate findings Tier 4 independently
    confirmed as legitimate recurring payments, each already dispositioned `legit`
    with its evidence. A no-op (returns everything in `kept`) when the feature is
    disabled (`auto_resolve_max_amount` <= 0) or no bank data is present."""
    ceiling = float(config.defaults.get("auto_resolve_max_amount", 0) or 0)
    _by_severity = lambda f: (-int(f.severity), f.rule_id)  # noqa: E731
    if ceiling <= 0 or bank is None or len(bank) == 0:
        # Sort even on the no-op path so output ordering doesn't silently depend on
        # whether the feature ran (config/bank-data availability).
        return sorted(findings, key=_by_severity), []

    min_spacing = int(config.defaults.get("auto_resolve_min_spacing_days", 0) or 0)
    amount_tol = float(config.param("bank_amount_tolerance"))
    date_tol = int(config.param("bank_date_tolerance_days"))
    check_max_days = int(config.param("check_match_max_days"))
    source_col = transactions["source_id"].astype(str)
    dispositioned_fps = _dispositioned_fingerprints(prior)

    kept: list[Finding] = []
    auto_resolved: list[Finding] = []
    for finding in findings:
        if (finding.rule_id not in AUTO_RESOLVE_RULES
                or finding.disposition != Disposition.OPEN
                or not finding.entity_ids or len(finding.transactions) < 2
                or finding.fingerprint() in dispositioned_fps):
            kept.append(finding)
            continue

        entity_id = finding.entity_ids[0]
        ids = {str(t) for t in finding.transactions}
        involved = transactions[source_col.isin(ids)
                                & (transactions["entity_id"] == entity_id)]
        # Every involved payment must be found, be a bank-clearing type, and sit at
        # or below the ceiling — otherwise we can't (or shouldn't) confirm it here.
        if (set(involved["source_id"].astype(str)) != ids or len(involved) < 2
                or not involved["txn_type"].isin(_CLEARING_TYPES).all()
                or (involved["amount"].abs() > ceiling + amount_tol).any()):
            kept.append(finding)
            continue

        disb = bank[(bank["entity_id"] == entity_id) & (bank["amount"] < 0)].copy()
        if disb.empty:
            kept.append(finding)
            continue
        disb["_amt"] = disb["amount"].abs()
        disb["_check"] = disb["check_no"].map(_norm_check)

        matched_idx = _distinct_clears(involved, disb, amount_tol, date_tol, check_max_days)
        if matched_idx is None or len(matched_idx) != len(involved):
            kept.append(finding)
            continue
        matched = disb.loc[matched_idx]
        # The clears must be spaced like recurring bills, not a same-week double-pay.
        if (matched["date"].max() - matched["date"].min()).days < min_spacing:
            kept.append(finding)
            continue

        register = None
        if account_labels:
            for fp in matched["account_fingerprint"]:
                if account_labels.get(fp):
                    register = account_labels[fp]
                    break
        _mark_resolved(finding, matched, ceiling, register)
        auto_resolved.append(finding)

    kept.sort(key=_by_severity)
    return kept, auto_resolved
