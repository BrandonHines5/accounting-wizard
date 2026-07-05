"""Incremental Tier 3 selection.

Re-reviewing every open finding on every weekly run is what blew past the job's
time limit (~1,100 findings × a reasoning call). Almost all of those were already
reviewed in a prior run and their assessment is stored. This module splits the
findings into:

- **carried** — already assessed in a prior run (same fingerprint, non-empty
  stored ai_assessment) by a judge at least as good as the current one. The
  stored assessment is reused in place (so the workbook stays faithful) and the
  model is NOT called again.
- **to_review** — genuinely new / not-yet-assessed findings, plus findings whose
  stored assessment is PROVISIONAL (heuristic stub or a failed call) when the
  current judge is a model: an offline triage pass must never permanently lock a
  finding out of its real review. Only these go to the judge, capped per run so
  a large first-time backlog still finishes within the job limit; the remainder
  is picked up on later runs once these persist (see
  FindingsStore.persist_assessments).

The cap walks findings highest-severity-first, so a truncated run always reviews
the most serious unassessed findings first.
"""
from __future__ import annotations

import pandas as pd

from core.findings import Finding

# Text signatures of provisional assessments written BEFORE provenance was
# stored (ai_judge column empty on legacy rows): the HeuristicJudge stub and the
# judge-failure fallback. Rows matching these are classified "heuristic" so a
# model run supersedes them.
_PROVISIONAL_MARKERS = ("no model review applied", "Tier 3 review unavailable")


def _judge_kind(assessment: str, ai_judge) -> str:
    """Provenance of a stored assessment: the ai_judge column when present,
    else inferred from the legacy assessment text."""
    if isinstance(ai_judge, str) and ai_judge.strip():
        return ai_judge.strip()
    if any(marker in assessment for marker in _PROVISIONAL_MARKERS):
        return "heuristic"
    return "model"


def _clamp01(value):
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, p)) if p == p else None    # NaN-safe


def _clean_action(value) -> str:
    """A stored recommended_action as a clean string, or '' when unset.

    A NULL recommended_action round-trips through pandas as a float NaN, and NaN is
    truthy — so `value or ''` yields the NaN (str()'d to the literal 'nan', which
    fails the findings_recommended_action_check on the next save). Guard on type
    instead of truthiness."""
    return value.strip() if isinstance(value, str) else ""


def _assessed(prior: pd.DataFrame | None) -> dict[str, dict]:
    """fingerprint -> stored triage (assessment, provenance kind, fp probability,
    recommended action), for prior findings carrying a non-empty assessment.
    Empty when there is no history or the column isn't present (e.g. the
    in-memory store used in tests)."""
    if (prior is None or len(prior) == 0
            or "fingerprint" not in prior.columns or "ai_assessment" not in prior.columns):
        return {}
    out: dict[str, dict] = {}
    for _, row in prior.iterrows():
        assessment = row["ai_assessment"]
        if not (isinstance(assessment, str) and assessment.strip()):
            continue
        out[str(row["fingerprint"])] = {
            "assessment": assessment,
            "kind": _judge_kind(assessment, row.get("ai_judge")),
            "false_positive_probability": _clamp01(row.get("false_positive_probability")),
            "recommended_action": _clean_action(row.get("recommended_action")),
        }
    return out


def select_for_review(
    findings: list[Finding],
    prior: pd.DataFrame | None,
    max_review: int | None = None,
    judge_kind: str = "model",
) -> tuple[list[Finding], list[Finding], int]:
    """Partition `findings` for incremental Tier 3.

    Returns (to_review, carried, deferred_count):
    - carried findings get their prior assessment/triage reused in place;
    - to_review is the fresh set the judge should assess — everything without a
      stored assessment, plus (when `judge_kind` is "model") everything whose
      stored assessment is only provisional/heuristic — capped to `max_review`
      (None/<=0 = no cap), highest-severity first;
    - deferred_count is how many fresh findings the cap pushed to a later run.
    """
    assessed = _assessed(prior)
    to_review: list[Finding] = []
    carried: list[Finding] = []
    for f in findings:
        stored = assessed.get(f.fingerprint())
        reusable = stored is not None and (
            stored["kind"] == "model" or judge_kind != "model")
        if reusable:
            f.ai_assessment = stored["assessment"]      # reuse — no model call
            f.ai_judge = stored["kind"]
            if stored["false_positive_probability"] is not None:
                f.false_positive_probability = stored["false_positive_probability"]
            if stored["recommended_action"]:
                f.recommended_action = stored["recommended_action"]
            carried.append(f)
        else:
            to_review.append(f)

    deferred = 0
    if max_review and max_review > 0 and len(to_review) > max_review:
        to_review.sort(key=lambda f: (-int(f.severity), f.rule_id))
        deferred = len(to_review) - max_review
        to_review = to_review[:max_review]
    return to_review, carried, deferred
