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
