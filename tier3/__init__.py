"""Tier 3 — AI judgment layer.

Runs on every Tier 1/2/4 finding before human review. For each finding it
assembles a context packet (the transaction(s), vendor history, who entered,
related prior dispositions) and asks Claude for a plain-English assessment,
a severity confirmation/adjustment, a false-positive probability with the
specific innocent explanation if likely, and a recommended next step.

Standing rule (DETECTION_SPEC.md): Tier 3 may downgrade severity with stated
reasoning but never silently drops a CRITICAL finding. `apply_tier3` enforces
this — it only ever annotates or re-ranks findings, never removes them, and a
downgrade without a reason is ignored.
"""
from tier3.context import JudgmentPacket, build_packets
from tier3.incremental import select_for_review
from tier3.judge import (
    HeuristicJudge,
    Judge,
    Tier3Assessment,
    apply_tier3,
)

__all__ = [
    "JudgmentPacket",
    "build_packets",
    "Judge",
    "HeuristicJudge",
    "Tier3Assessment",
    "apply_tier3",
    "select_for_review",
]
