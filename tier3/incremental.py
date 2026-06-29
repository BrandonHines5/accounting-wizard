"""Incremental Tier 3 selection.

Re-reviewing every open finding on every weekly run is what blew past the job's
time limit (~1,100 findings × a reasoning call). Almost all of those were already
reviewed in a prior run and their assessment is stored. This module splits the
findings into:

- **carried** — already assessed in a prior run (same fingerprint, non-empty
  stored ai_assessment). The stored assessment is reused in place (so the workbook
  stays faithful) and the model is NOT called again.
- **to_review** — genuinely new / not-yet-assessed findings. Only these go to the
  judge, capped per run so a large first-time backlog still finishes within the
  job limit; the remainder is picked up on later runs once these persist (see
  FindingsStore.persist_assessments).

The cap walks findings highest-severity-first, so a truncated run always reviews
the most serious unassessed findings first.
"""
from __future__ import annotations

import pandas as pd

from core.findings import Finding


def _assessed(prior: pd.DataFrame | None) -> dict[str, str]:
    """fingerprint -> stored ai_assessment, for prior findings that carry a
    non-empty one. Empty when there is no history or the column isn't present
    (e.g. the in-memory store used in tests)."""
    if (prior is None or len(prior) == 0
            or "fingerprint" not in prior.columns or "ai_assessment" not in prior.columns):
        return {}
    out: dict[str, str] = {}
    for fp, assessment in zip(prior["fingerprint"], prior["ai_assessment"]):
        if isinstance(assessment, str) and assessment.strip():
            out[str(fp)] = assessment
    return out


def select_for_review(
    findings: list[Finding],
    prior: pd.DataFrame | None,
    max_review: int | None = None,
) -> tuple[list[Finding], list[Finding], int]:
    """Partition `findings` for incremental Tier 3.

    Returns (to_review, carried, deferred_count):
    - carried findings get their prior ai_assessment reused in place;
    - to_review is the fresh set the judge should assess, capped to `max_review`
      (None/<=0 = no cap), highest-severity first;
    - deferred_count is how many fresh findings the cap pushed to a later run.
    """
    assessed = _assessed(prior)
    to_review: list[Finding] = []
    carried: list[Finding] = []
    for f in findings:
        prior_assessment = assessed.get(f.fingerprint())
        if prior_assessment:
            f.ai_assessment = prior_assessment       # reuse — no model call
            carried.append(f)
        else:
            to_review.append(f)

    deferred = 0
    if max_review and max_review > 0 and len(to_review) > max_review:
        to_review.sort(key=lambda f: (-int(f.severity), f.rule_id))
        deferred = len(to_review) - max_review
        to_review = to_review[:max_review]
    return to_review, carried, deferred
