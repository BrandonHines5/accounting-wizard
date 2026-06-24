"""Tier 3 judge interface, the deterministic fallback, and severity safeguards.

`apply_tier3(findings, packets, judge, ...)` enriches each finding in place with
the judge's assessment and re-ranks the list. It is the single chokepoint for the
standing rule that Tier 3 may downgrade with a reason but never silently drops a
CRITICAL: findings are only ever annotated or re-ordered here, never removed, and
a severity downgrade with no stated reason is refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.findings import (
    Finding,
    Severity,
    apply_entity_severity_floor,
)
from core.entities import Entity
from tier3.context import JudgmentPacket

_SEVERITY_BY_NAME = {s.name: s for s in Severity}
_VALID_ACTIONS = {"clear", "verify", "escalate"}


@dataclass
class Tier3Assessment:
    assessment: str                          # plain-English, 2–4 sentences
    severity: Severity                       # confirmed or adjusted
    severity_reason: str = ""                # required if severity differs from input
    false_positive_probability: float = 0.0  # 0.0–1.0
    innocent_explanation: str = ""           # specific benign explanation, if FP likely
    recommended_action: str = "verify"       # clear | verify | escalate
    recommended_action_detail: str = ""


class Judge:
    """Base class. Subclasses implement `assess(packet) -> Tier3Assessment`."""

    def assess(self, packet: JudgmentPacket) -> Tier3Assessment:  # pragma: no cover
        raise NotImplementedError

    def assess_all(self, packets: list[JudgmentPacket]) -> list[Tier3Assessment]:
        """Assess every packet. A failure on one finding must never abort the run
        or, worse, drop the finding — it falls back to a conservative no-change
        assessment that preserves severity and flags the failure for human review."""
        out = []
        for packet in packets:
            try:
                out.append(self.assess(packet))
            except Exception as exc:  # noqa: BLE001 — degrade, never lose a finding
                out.append(_failed_assessment(packet, exc))
        return out


def _failed_assessment(packet: JudgmentPacket, exc: Exception) -> Tier3Assessment:
    sev = packet.finding.severity
    return Tier3Assessment(
        assessment=("Tier 3 review unavailable for this finding "
                    f"({type(exc).__name__}); routed to human review unchanged."),
        severity=sev,                        # never change severity on failure
        severity_reason="",
        false_positive_probability=0.0,
        recommended_action="escalate" if sev >= Severity.HIGH else "verify",
        recommended_action_detail="Automated assessment failed — review manually.",
    )


class HeuristicJudge(Judge):
    """Deterministic, offline judge — no model call.

    Used in tests and as the `--tier3 heuristic` fallback when no API key is
    available. It confirms severity (never adjusts), derives a coarse
    false-positive prior from context (recurring/known-vendor patterns read as
    more likely benign), and maps severity to a recommended action. It is
    intentionally conservative: it adds triage structure without inventing
    judgment the deterministic rules don't support."""

    def assess(self, packet: JudgmentPacket) -> Tier3Assessment:
        finding = packet.finding
        established = any(h.get("txn_count_in_entity", 0) >= 5
                          for h in packet.vendor_history)
        fp = 0.4 if established and finding.severity <= Severity.MEDIUM else 0.15
        cleared_before = any(p.get("disposition") in {"legit", "error_corrected"}
                             for p in packet.prior_findings)
        if cleared_before:
            fp = min(0.9, fp + 0.4)
        action = ("escalate" if finding.severity >= Severity.HIGH
                  else "verify" if finding.severity == Severity.MEDIUM
                  else "clear")
        explanation = ("A similar finding was previously dispositioned as benign — "
                       "confirm this is not a recurrence." if cleared_before else "")
        return Tier3Assessment(
            assessment=(f"Deterministic {finding.rule_id} flag confirmed; no model "
                        "review applied. Verify against source documents."),
            severity=finding.severity,
            severity_reason="",
            false_positive_probability=fp,
            innocent_explanation=explanation,
            recommended_action=action,
            recommended_action_detail="",
        )


def _clamp(p: float) -> float:
    try:
        return max(0.0, min(1.0, float(p)))
    except (TypeError, ValueError):
        return 0.0


def coerce_severity(value, fallback: Severity) -> Severity:
    if isinstance(value, Severity):
        return value
    return _SEVERITY_BY_NAME.get(str(value).strip().upper(), fallback)


def apply_assessment(
    finding: Finding,
    assessment: Tier3Assessment,
    entities_by_id: dict[str, Entity] | None = None,
) -> Finding:
    """Fold one assessment into its finding, enforcing the severity safeguards."""
    finding.ai_assessment = assessment.assessment.strip()
    finding.false_positive_probability = _clamp(assessment.false_positive_probability)
    action = assessment.recommended_action.strip().lower()
    finding.recommended_action = action if action in _VALID_ACTIONS else "verify"
    if assessment.innocent_explanation.strip():
        finding.details["innocent_explanation"] = assessment.innocent_explanation.strip()
    if assessment.recommended_action_detail.strip():
        finding.details["recommended_next_step"] = assessment.recommended_action_detail.strip()

    new_sev = coerce_severity(assessment.severity, finding.severity)
    reason = assessment.severity_reason.strip()
    if new_sev > finding.severity:
        finding.original_severity = finding.severity
        finding.severity = new_sev
        finding.details["severity_adjustment"] = (
            f"Tier 3 raised {finding.original_severity}→{new_sev}: "
            f"{reason or 'no reason given'}")
    elif new_sev < finding.severity:
        # Downgrade requires a stated reason. Without one, refuse it — this is the
        # guard that keeps a CRITICAL from being silently demoted.
        if reason:
            finding.original_severity = finding.severity
            finding.severity = new_sev
            finding.details["severity_adjustment"] = (
                f"Tier 3 lowered {finding.original_severity}→{new_sev}: {reason}")
        else:
            finding.details["severity_adjustment"] = (
                f"Tier 3 proposed lowering to {new_sev} but gave no reason — kept "
                f"at {finding.severity}.")

    # A Tier 3 downgrade must never breach the entity-based floor (nonprofit
    # misallocation stays HIGH minimum). Re-apply it after any adjustment.
    if entities_by_id is not None:
        apply_entity_severity_floor(finding, entities_by_id)
    return finding


def apply_tier3(
    findings: list[Finding],
    packets: list[JudgmentPacket],
    judge: Judge,
    entities_by_id: dict[str, Entity] | None = None,
) -> list[Finding]:
    """Assess every finding and re-rank by (possibly adjusted) severity.

    `packets` must align positionally with `findings` (see `build_packets`).
    Returns the same list, mutated in place and re-sorted."""
    if len(findings) != len(packets):
        raise ValueError("findings and packets must be the same length and order")
    assessments = judge.assess_all(packets)
    for finding, assessment in zip(findings, assessments):
        apply_assessment(finding, assessment, entities_by_id)
    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings
