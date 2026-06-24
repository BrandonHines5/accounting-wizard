"""Finding model + severity handling.

Findings are phrased as verification questions, never accusations. Severity may
be floored upward by entity attributes (nonprofit misallocation = HIGH minimum)
but Tier 3 / code never silently drops a CRITICAL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from core.entities import Entity


class Severity(IntEnum):
    INFO = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    def __str__(self) -> str:
        return self.name


# Rule families where a nonprofit entity's involvement means possible
# misallocation of restricted/charitable funds → HIGH severity minimum.
MISALLOCATION_RULES = {"T1-20", "T1-21", "T1-22", "T1-23", "T1-24"}


@dataclass
class Finding:
    rule_id: str
    severity: Severity
    entity_ids: list[str]
    question: str                      # the verification question for the reviewer
    details: dict = field(default_factory=dict)
    transactions: list[str] = field(default_factory=list)  # source_ids involved
    disposition: str = "open"          # open | legit | error_corrected | escalated

    # Tier 3 (AI judgment layer) — populated by tier3.apply_tier3, empty until then.
    ai_assessment: str = ""            # plain-English review, 2–4 sentences
    false_positive_probability: float | None = None   # 0.0–1.0, None = not assessed
    recommended_action: str = ""       # clear | verify | escalate
    original_severity: Severity | None = None  # set only when Tier 3 changed severity

    def to_row(self) -> dict:
        """Flatten the finding (plus any Tier 3 fields) into a workbook row."""
        row = {
            "rule_id": self.rule_id,
            "severity": str(self.severity),
            "entities": ", ".join(self.entity_ids),
            "question": self.question,
            "ai_assessment": self.ai_assessment,
            "recommended_action": self.recommended_action,
            "false_positive": ("" if self.false_positive_probability is None
                               else f"{self.false_positive_probability:.0%}"),
            "transactions": ", ".join(self.transactions),
            "disposition": self.disposition,
        }
        if self.original_severity is not None:
            row["original_severity"] = str(self.original_severity)
        row.update(self.details)
        return row


def apply_entity_severity_floor(finding: Finding, entities_by_id: dict[str, Entity]) -> Finding:
    """Floor severity at HIGH when a misallocation-class finding touches a nonprofit."""
    if finding.rule_id not in MISALLOCATION_RULES:
        return finding
    involved = (entities_by_id.get(eid) for eid in finding.entity_ids)
    if any(e is not None and e.is_nonprofit for e in involved):
        if finding.severity < Severity.HIGH:
            finding.details["severity_note"] = (
                "Raised to HIGH: involves a 501(c)(3) entity (registry legal_type)."
            )
            finding.severity = Severity.HIGH
    return finding
