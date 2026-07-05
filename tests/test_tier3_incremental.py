"""Incremental Tier 3 selection — only review fresh findings, carry the rest."""
import pandas as pd

from core.findings import Finding, Severity
from tier3.incremental import select_for_review


def _f(rule_id, txn, sev=Severity.HIGH):
    return Finding(rule_id=rule_id, severity=sev, entity_ids=["alpha"],
                   question="verify?", transactions=[txn])


def _prior(pairs):  # [(fingerprint, ai_assessment), ...]
    return pd.DataFrame(pairs, columns=["fingerprint", "ai_assessment"])


def test_carries_forward_assessed_and_reviews_only_fresh():
    a, b = _f("T1-01", "TX-A"), _f("T1-02", "TX-B")
    prior = _prior([(a.fingerprint(), "stored assessment for A")])
    to_review, carried, deferred = select_for_review([a, b], prior, max_review=0)
    assert carried == [a] and a.ai_assessment == "stored assessment for A"  # reused in place
    assert to_review == [b] and deferred == 0                                # only the fresh one


def test_no_history_reviews_everything():
    a, b = _f("T1-01", "TX-A"), _f("T1-02", "TX-B")
    to_review, carried, deferred = select_for_review([a, b], None, max_review=0)
    assert to_review == [a, b] and carried == [] and deferred == 0


def test_prior_without_assessment_column_reviews_everything():
    a = _f("T1-01", "TX-A")
    prior = pd.DataFrame([{"fingerprint": a.fingerprint(), "disposition": "open"}])
    to_review, carried, _ = select_for_review([a], prior, max_review=0)
    assert to_review == [a] and carried == []


def test_blank_prior_assessment_is_not_carried():
    a = _f("T1-01", "TX-A")
    to_review, carried, _ = select_for_review([a], _prior([(a.fingerprint(), "   ")]), max_review=0)
    assert to_review == [a] and carried == []


def test_cap_keeps_highest_severity_and_defers_the_rest():
    crit = _f("T1-01", "TX-C", Severity.CRITICAL)
    med = _f("T1-30", "TX-M", Severity.MEDIUM)
    high = _f("T1-04", "TX-H", Severity.HIGH)
    to_review, carried, deferred = select_for_review([med, crit, high], None, max_review=2)
    assert to_review == [crit, high]      # severity-ordered; the MEDIUM is deferred
    assert deferred == 1 and carried == []


def test_cap_does_not_count_carried_findings():
    # Two already-assessed + two fresh, cap 2 → both fresh fit (carried don't count).
    assessed1, assessed2 = _f("T1-01", "TX-1"), _f("T1-02", "TX-2")
    fresh1, fresh2 = _f("T1-04", "TX-3"), _f("T1-05", "TX-4")
    prior = _prior([(assessed1.fingerprint(), "a1"), (assessed2.fingerprint(), "a2")])
    to_review, carried, deferred = select_for_review(
        [assessed1, fresh1, assessed2, fresh2], prior, max_review=2)
    assert carried == [assessed1, assessed2]
    assert to_review == [fresh1, fresh2] and deferred == 0


# --------------------------------------------------- provenance (stub lockout)

_STUB = "Deterministic T1-01 flag confirmed; no model review applied. Verify."
_FAILED = "Tier 3 review unavailable for this finding (TimeoutError); routed unchanged."


def test_heuristic_stub_is_rereviewed_by_a_model_judge():
    # The lockout bug: a heuristic stub stored as the assessment must NOT count
    # as reviewed once a real model judge is available.
    a = _f("T1-01", "TX-A")
    prior = _prior([(a.fingerprint(), _STUB)])          # legacy row, no ai_judge col
    to_review, carried, _ = select_for_review([a], prior, max_review=0, judge_kind="model")
    assert to_review == [a] and carried == []


def test_failed_assessment_is_rereviewed_by_a_model_judge():
    a = _f("T1-01", "TX-A")
    prior = _prior([(a.fingerprint(), _FAILED)])
    to_review, carried, _ = select_for_review([a], prior, max_review=0, judge_kind="model")
    assert to_review == [a] and carried == []


def test_heuristic_stub_is_carried_when_judge_is_heuristic():
    # No model available → the stub is the best we have; don't churn.
    a = _f("T1-01", "TX-A")
    prior = _prior([(a.fingerprint(), _STUB)])
    to_review, carried, _ = select_for_review([a], prior, max_review=0,
                                              judge_kind="heuristic")
    assert carried == [a] and to_review == []
    assert a.ai_judge == "heuristic"


def test_carried_null_recommended_action_does_not_become_string_nan():
    # Regression: a prior MODEL review with a NULL recommended_action (legacy rows
    # assessed before the column existed) round-trips through pandas as a float NaN.
    # NaN is truthy, so the old `value or ""` yielded the literal string "nan" — which
    # then failed the findings_recommended_action_check and aborted the whole save().
    a = _f("T1-01", "TX-A")
    prior = pd.DataFrame([
        {"fingerprint": a.fingerprint(), "ai_assessment": "real model review",
         "ai_judge": "model", "false_positive_probability": float("nan"),
         "recommended_action": float("nan")},
    ])
    to_review, carried, _ = select_for_review([a], prior, max_review=0, judge_kind="model")
    assert carried == [a] and to_review == []      # reused, not re-reviewed
    assert a.recommended_action == ""              # NOT "nan" — the poisoned value
    assert a.false_positive_probability is None


def test_ai_judge_column_wins_over_text_inference():
    a, b = _f("T1-01", "TX-A"), _f("T1-02", "TX-B")
    prior = pd.DataFrame([
        {"fingerprint": a.fingerprint(), "ai_assessment": "real model review",
         "ai_judge": "model", "false_positive_probability": 0.85,
         "recommended_action": "clear"},
        {"fingerprint": b.fingerprint(), "ai_assessment": "looks fine honestly",
         "ai_judge": "heuristic", "false_positive_probability": None,
         "recommended_action": None},
    ])
    to_review, carried, _ = select_for_review([a, b], prior, max_review=0,
                                              judge_kind="model")
    assert carried == [a] and to_review == [b]
    # carried findings get their stored triage back for the workbook/UI
    assert a.false_positive_probability == 0.85
    assert a.recommended_action == "clear"
    assert a.ai_judge == "model"
