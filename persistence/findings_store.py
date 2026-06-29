"""Findings history + the disposition-memory policy.

Run-over-run, every finding carries a stable `fingerprint` (see
`Finding.fingerprint`). The store remembers how each prior fingerprint was
dispositioned. On the next run:

- an *exact* re-occurrence of something a human cleared (legit / error_corrected)
  is suppressed — it never resurfaces in the active workbook;
- a *new* instance of the same pattern (same rule + entities + vendor) after a
  prior clear is escalated one severity level, flagged as a recurrence — the
  "cleared once, but it's happening again" signal;
- everything else passes through unchanged.

Suppressed findings are returned (not silently dropped) so the reporting layer
can still list them for an audit trail.
"""
from __future__ import annotations

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

# Detail keys used to identify "the same kind of issue" for recurrence-escalation.
_PATTERN_VENDOR_KEYS = ("vendor", "vendor_a", "debtor")


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
                rec["ai_assessment"] = assessment   # only the assessment, never disposition

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
            })


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [e.strip() for e in value.split(",") if e.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(e) for e in value]
    return []


def _pattern_key(rule_id: str, entity_ids, details: dict | None) -> str | None:
    """Coarse identity for recurrence detection: rule + entities + primary vendor.

    Returns None when no vendor key is present — without that discriminator the
    key would be just rule+entities, so one cleared finding would wrongly escalate
    every later finding of the same rule/entities. No vendor → no recurrence."""
    details = details or {}
    vendor = ""
    for key in _PATTERN_VENDOR_KEYS:
        val = details.get(key)
        if val:
            vendor = str(val)
            break
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


def apply_disposition_memory(
    findings: list[Finding],
    prior: pd.DataFrame | None,
    entities_by_id: dict[str, Entity] | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Partition findings against history.

    Returns (kept, suppressed): `kept` is the active list (recurrences escalated
    in place, re-sorted by severity); `suppressed` is the exact re-occurrences of
    previously-cleared findings, returned for the audit trail rather than dropped.
    With no history this is a no-op."""
    if prior is None or len(prior) == 0 or "fingerprint" not in prior.columns:
        return list(findings), []

    cleared = prior[prior["disposition"].astype(str).isin(_CLEARED)]
    cleared_fingerprints = set(cleared["fingerprint"])
    cleared_patterns = {
        key for _, row in cleared.iterrows()
        if (key := _pattern_key(row["rule_id"], row.get("entity_ids"),
                                row.get("details"))) is not None
    }

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
            _escalate(finding, entities_by_id)
        kept.append(finding)

    kept.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return kept, suppressed
