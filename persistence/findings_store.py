"""Findings history + the disposition-memory policy.

Run-over-run, every finding carries a stable `fingerprint` (see
`Finding.fingerprint`). The store remembers how each prior fingerprint was
dispositioned. On the next run:

- an *exact* re-occurrence of something a human cleared (legit / error_corrected)
  is suppressed — it never resurfaces in the active workbook;
- a *new* instance of the same pattern (same rule + entities + vendor/description)
  after a prior clear follows the RULE's recurrence policy:
  * fraud-pattern rules (duplicate payments, vendor bank changes, check
    alterations) ESCALATE one severity level — "cleared once, but it's
    happening again" is exactly the signal those rules exist for;
  * cadence/operational rules (off-cycle payments, clearing gaps, recurring
    ACH sweeps, payment-timing stats) SUPPRESS the recurrence, carrying the
    human's original reason forward — clearing a monthly bank fee once must
    teach the system, not make next month's fee MORE prominent. CRITICAL
    findings are never auto-suppressed (hard rule), they just carry the prior
    reason as context;
  * every other rule passes through unchanged, annotated with the prior reason.

Suppressed findings are returned (not silently dropped) so the reporting layer
can still list them for an audit trail.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

import pandas as pd

from core.entities import Entity
from core.findings import (
    Disposition,
    Finding,
    Severity,
    apply_entity_severity_floor,
)

# Dispositions that mean a human resolved the finding (vs. open / escalated).
_CLEARED = {str(Disposition.LEGIT), str(Disposition.ERROR_CORRECTED)}

# Detail keys used to identify "the same kind of issue" for recurrence handling.
_PATTERN_VENDOR_KEYS = ("vendor", "vendor_a", "debtor")

# Recurrence-after-clear policy, per rule family. A pattern recurring after a
# human cleared it means opposite things for different rules:
# - ESCALATE: repeat duplicate payments / bank-detail changes / alterations to
#   the same vendor after a clear are the classic fraud ramp — surface louder.
# - SUPPRESS: recurring operational patterns (a vendor that's always paid
#   off-cycle, a monthly ACH fee, a slow-clearing payee) are benign by the
#   human's own prior judgment — re-raising them every run is what buries the
#   real findings. CRITICALs are exempt from suppression (never auto-dropped).
# - anything unlisted: pass through, annotated with the prior reason so Tier 3
#   and the reviewer see it.
RECURRENCE_ESCALATE_RULES = {"T1-01", "T1-02", "T1-04", "T1-12", "T1-14",
                             "T4-03", "T4-04", "T4-05"}
RECURRENCE_SUPPRESS_RULES = {"T1-07", "T1-08", "T1-20", "T1-22", "T2-10",
                             "T4-02", "T4-06", "T4-09"}


class FindingsStore(ABC):
    """I/O boundary for findings history. Implementations must not let `save`
    overwrite a human disposition — only insert genuinely new fingerprints."""

    @abstractmethod
    def load_prior(self) -> pd.DataFrame:
        """Prior findings with at least: fingerprint, rule_id, entity_ids,
        disposition, details. Empty frame when there is no history."""

    @abstractmethod
    def save(self, findings: list[Finding]) -> None:
        """Insert new fingerprints as `open`; leave existing rows untouched."""

    def persist_assessments(self, findings: list[Finding]) -> None:
        """Store the Tier 3 `ai_assessment` for findings already in history,
        updating ONLY that column — never the disposition or any human field. This
        lets an incremental run converge: a finding reviewed once is recognized as
        already-assessed next run and skipped, instead of being re-reviewed forever
        (save() leaves existing rows untouched, so it can't persist the assessment).
        Default no-op for stores without an update path."""


class InMemoryFindingsStore(FindingsStore):
    """Non-persistent store for tests and dry runs. Seed `prior` with
    already-dispositioned records to exercise disposition memory."""

    def __init__(self, prior: list[dict] | None = None):
        self._records: list[dict] = [dict(r) for r in (prior or [])]

    def load_prior(self) -> pd.DataFrame:
        return pd.DataFrame(self._records)

    def persist_assessments(self, findings: list[Finding]) -> None:
        by_fp = {r["fingerprint"]: r for r in self._records}
        for f in findings:
            assessment = (f.ai_assessment or "").strip()
            rec = by_fp.get(f.fingerprint())
            if assessment and rec is not None:
                # only the Tier 3 columns, never the disposition
                rec["ai_assessment"] = assessment
                rec["false_positive_probability"] = f.false_positive_probability
                rec["recommended_action"] = f.recommended_action or None
                rec["ai_judge"] = f.ai_judge or None

    def save(self, findings: list[Finding]) -> None:
        existing = {r["fingerprint"] for r in self._records}
        for f in findings:
            fp = f.fingerprint()
            if fp in existing:
                continue
            existing.add(fp)
            self._records.append({
                "fingerprint": fp,
                "rule_id": f.rule_id,
                "entity_ids": list(f.entity_ids),
                "disposition": str(Disposition.OPEN),
                "details": dict(f.details),
                "question": f.question,
                "ai_assessment": f.ai_assessment or None,
                "false_positive_probability": f.false_positive_probability,
                "recommended_action": f.recommended_action or None,
                "ai_judge": f.ai_judge or None,
            })


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [e.strip() for e in value.split(",") if e.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(e) for e in value]
    return []


def _normalized_description(details: dict) -> str:
    """Bank-side findings (T4-09 sweeps, unexplained inflows) carry a statement
    description instead of a vendor. Digits vary line to line (dates, trace
    numbers), so collapse them — 'ACH FEE 0632' and 'ACH FEE 0715' are the same
    recurring pattern."""
    desc = details.get("description")
    if not desc:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", str(desc).lower())).strip()


def _pattern_key(rule_id: str, entity_ids, details: dict | None) -> str | None:
    """Coarse identity for recurrence detection: rule + entities + primary vendor
    (falling back to the normalized bank description for vendor-less bank lines).

    Returns None when neither is present — without a discriminator the key would
    be just rule+entities, so one cleared finding would wrongly re-classify every
    later finding of the same rule/entities. No vendor → no recurrence."""
    details = details or {}
    vendor = ""
    for key in _PATTERN_VENDOR_KEYS:
        val = details.get(key)
        if val:
            vendor = str(val)
            break
    vendor = vendor or _normalized_description(details)
    if not vendor:
        return None
    return f"{rule_id}|{','.join(sorted(_as_list(entity_ids)))}|{vendor}"


def _escalate(finding: Finding, entities_by_id: dict[str, Entity] | None) -> None:
    original = finding.severity
    if finding.severity < Severity.CRITICAL:
        finding.severity = Severity(int(finding.severity) + 1)
    finding.details["recurrence"] = (
        f"Recurs after a prior clear of a similar {finding.rule_id} finding — "
        f"escalated {original}→{finding.severity}.")
    if entities_by_id is not None:
        apply_entity_severity_floor(finding, entities_by_id)


def _prior_reason(note: str) -> str:
    return f' (your reason: "{note}")' if note else ""


def apply_disposition_memory(
    findings: list[Finding],
    prior: pd.DataFrame | None,
    entities_by_id: dict[str, Entity] | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Partition findings against history.

    Returns (kept, suppressed): `kept` is the active list (recurrences escalated
    or annotated in place per the rule's policy, re-sorted by severity);
    `suppressed` is the exact re-occurrences of previously-cleared findings plus
    the pattern-recurrences of suppress-policy rules, returned for the audit
    trail rather than dropped. With no history this is a no-op."""
    if prior is None or len(prior) == 0 or "fingerprint" not in prior.columns:
        return list(findings), []

    cleared = prior[prior["disposition"].astype(str).isin(_CLEARED)]
    cleared_fingerprints = set(cleared["fingerprint"])
    # pattern key -> the human's stated reason (a non-empty note wins).
    cleared_patterns: dict[str, str] = {}
    for _, row in cleared.iterrows():
        key = _pattern_key(row["rule_id"], row.get("entity_ids"), row.get("details"))
        if key is None:
            continue
        note = str(row.get("disposition_note") or "").strip()
        if note or key not in cleared_patterns:
            cleared_patterns[key] = note

    kept: list[Finding] = []
    suppressed: list[Finding] = []
    for finding in findings:
        if finding.fingerprint() in cleared_fingerprints:
            finding.details["disposition_memory"] = (
                "Suppressed: previously reviewed and dispositioned as resolved.")
            suppressed.append(finding)
            continue
        pattern = _pattern_key(finding.rule_id, finding.entity_ids, finding.details)
        if pattern is not None and pattern in cleared_patterns:
            note = cleared_patterns[pattern]
            if finding.rule_id in RECURRENCE_ESCALATE_RULES:
                _escalate(finding, entities_by_id)
            elif (finding.rule_id in RECURRENCE_SUPPRESS_RULES
                    and finding.severity < Severity.CRITICAL):
                finding.details["disposition_memory"] = (
                    "Suppressed: recurrence of a pattern you previously cleared"
                    f"{_prior_reason(note)}.")
                suppressed.append(finding)
                continue
            else:
                finding.details["prior_clear"] = (
                    "A similar finding was previously cleared"
                    f"{_prior_reason(note)} — confirm this instance matches.")
        kept.append(finding)

    kept.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return kept, suppressed
