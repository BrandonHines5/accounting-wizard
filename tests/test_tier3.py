"""Tier 3 AI judgment layer: packet assembly, severity safeguards, judges."""
import json

import pandas as pd
import pytest

from core.findings import Finding, Severity
from rules.engine import run_all
from tier3 import HeuristicJudge, apply_tier3, build_packets
from tier3.anthropic_judge import AnthropicJudge
from tier3.judge import Tier3Assessment, apply_assessment


@pytest.fixture
def findings(ctx):
    # Function-scoped: apply_tier3 mutates Finding objects in place, so each test
    # gets its own fresh battery output rather than sharing (and leaking) state.
    return run_all(ctx)


def entities_map(registry):
    return {e.id: e for e in registry}


def find(findings, rule_id):
    return next(f for f in findings if f.rule_id == rule_id)


# ---------------------------------------------------------------- packet assembly

def test_packet_pulls_involved_transactions_and_vendor_history(ctx, findings):
    dup = find(findings, "T1-01")          # Acme Lumber TX-001/TX-002 on alpha
    packet = build_packets([dup], ctx)[0]

    assert packet.entity["id"] == "alpha"
    assert packet.entity["is_nonprofit"] is False
    assert {t["source_id"] for t in packet.transactions} == {"TX-001", "TX-002"}
    acme = next(h for h in packet.vendor_history if h["vendor_name"] == "Acme Lumber")
    assert acme["txn_count_in_entity"] >= 2
    # the packet is JSON-serializable (Timestamps coerced to ISO strings)
    json.dumps(packet.to_prompt_dict())


def test_packet_surfaces_prior_dispositions(ctx, findings):
    dup = find(findings, "T1-01")
    prior = pd.DataFrame([
        {"rule_id": "T1-01", "entity_ids": ["alpha"], "disposition": "legit",
         "question": "Earlier Acme duplicate?", "dispositioned_at": "2026-04-01"},
        {"rule_id": "T1-30", "entity_ids": ["beta"], "disposition": "open",
         "question": "unrelated", "dispositioned_at": None},
    ])
    packet = build_packets([dup], ctx, prior_findings=prior)[0]
    assert len(packet.prior_findings) == 1
    assert packet.prior_findings[0]["disposition"] == "legit"


def test_packet_handles_finding_without_transactions(ctx, findings):
    interco = find(findings, "T1-24")      # no source_ids attached
    packet = build_packets([interco], ctx)[0]
    assert packet.transactions == []
    json.dumps(packet.to_prompt_dict())


# ---------------------------------------------------------------- severity guards

def test_downgrade_requires_reason(registry):
    f = Finding("T1-07", Severity.MEDIUM, ["alpha"], "?")
    apply_assessment(f, Tier3Assessment(assessment="benign", severity=Severity.INFO,
                                        severity_reason=""), entities_map(registry))
    assert f.severity == Severity.MEDIUM            # refused — no reason
    assert "no reason" in f.details["severity_adjustment"]
    assert f.original_severity is None


def test_downgrade_with_reason_applies(registry):
    f = Finding("T1-07", Severity.MEDIUM, ["alpha"], "?")
    apply_assessment(f, Tier3Assessment(assessment="bank fee", severity=Severity.INFO,
                                        severity_reason="Recurring bank fee, not a payment."),
                     entities_map(registry))
    assert f.severity == Severity.INFO
    assert f.original_severity == Severity.MEDIUM


def test_critical_never_silently_downgraded(registry):
    f = Finding("T1-01", Severity.CRITICAL, ["alpha"], "?")
    apply_assessment(f, Tier3Assessment(assessment="looks fine", severity=Severity.INFO,
                                        severity_reason=""), entities_map(registry))
    assert f.severity == Severity.CRITICAL          # the core safeguard


def test_severity_can_be_raised(registry):
    f = Finding("T1-30", Severity.MEDIUM, ["alpha"], "?")
    apply_assessment(f, Tier3Assessment(assessment="suspicious", severity=Severity.HIGH,
                                        severity_reason="Round write-off to a new payee."),
                     entities_map(registry))
    assert f.severity == Severity.HIGH
    assert f.original_severity == Severity.MEDIUM


def test_nonprofit_floor_survives_tier3_downgrade(registry):
    # Misallocation touching a 501(c)(3) must stay HIGH minimum even if Tier 3
    # downgrades it with a reason.
    f = Finding("T1-23", Severity.HIGH, ["charity", "alpha"], "?")
    apply_assessment(f, Tier3Assessment(assessment="probably fine", severity=Severity.INFO,
                                        severity_reason="Looks like a normal posting."),
                     entities_map(registry))
    assert f.severity == Severity.HIGH


# ---------------------------------------------------------------- judges

def test_heuristic_judge_enriches_without_changing_severity(ctx, registry, findings):
    subset = [find(findings, "T1-01"), find(findings, "T1-30")]
    before = {f.rule_id: f.severity for f in subset}
    packets = build_packets(subset, ctx)
    apply_tier3(subset, packets, HeuristicJudge(), entities_map(registry))
    for f in subset:
        assert f.ai_assessment
        assert f.recommended_action in {"clear", "verify", "escalate"}
        assert 0.0 <= f.false_positive_probability <= 1.0
        assert f.severity == before[f.rule_id]      # heuristic confirms, never adjusts


def test_apply_tier3_is_resorted_and_length_checked(ctx, registry, findings):
    subset = list(findings)
    packets = build_packets(subset, ctx)
    out = apply_tier3(subset, packets, HeuristicJudge(), entities_map(registry))
    severities = [int(f.severity) for f in out]
    assert severities == sorted(severities, reverse=True)
    with pytest.raises(ValueError):
        apply_tier3(subset, packets[:-1], HeuristicJudge())


def test_judge_failure_degrades_without_dropping(ctx, findings):
    class BrokenJudge(HeuristicJudge):
        def assess(self, packet):
            raise RuntimeError("model down")

    crit = find(findings, "T1-01")
    f = Finding(crit.rule_id, crit.severity, crit.entity_ids, crit.question,
                transactions=crit.transactions)
    packets = build_packets([f], ctx)
    apply_tier3([f], packets, BrokenJudge())
    assert f.severity == Severity.CRITICAL          # preserved on failure
    assert "unavailable" in f.ai_assessment
    assert f.recommended_action == "escalate"


# ---------------------------------------------------------------- Claude judge (stubbed)

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self._payload)


class _FakeClient:
    def __init__(self, payload):
        self.messages = _FakeMessages(payload)


def test_anthropic_judge_builds_request_and_parses_response(ctx, registry, findings):
    payload = json.dumps({
        "assessment": "Two checks to Acme for the same invoice — likely a reissue.",
        "severity": "HIGH",
        "severity_reason": "Second check voided per memo; reduce from CRITICAL.",
        "false_positive_probability": 0.7,
        "innocent_explanation": "First check was voided and reissued.",
        "recommended_action": "verify",
        "recommended_action_detail": "Confirm the first check cleared or was voided.",
    })
    client = _FakeClient(payload)
    judge = AnthropicJudge(client=client, model="claude-opus-4-8")

    dup = find(findings, "T1-01")
    f = Finding(dup.rule_id, dup.severity, dup.entity_ids, dup.question,
                transactions=dup.transactions)
    packets = build_packets([f], ctx)
    apply_tier3([f], packets, judge, entities_map(registry))

    # request shape: model, structured-output schema, cached system prompt
    kwargs = client.messages.calls[0]
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    # response folded in, downgrade honored because a reason was given
    assert f.severity == Severity.HIGH
    assert f.original_severity == Severity.CRITICAL
    assert f.false_positive_probability == 0.7
    assert f.details["innocent_explanation"].startswith("First check")


def test_anthropic_judge_assess_all_concurrent_preserves_order_and_isolates_errors(
        ctx, findings, monkeypatch):
    # The weekly batch fans Tier 3 out across threads. Results must stay aligned
    # with the input packets, and one finding erroring must degrade only itself.
    monkeypatch.setenv("TIER3_CONCURRENCY", "4")

    class _StubJudge(AnthropicJudge):
        def assess(self, packet):
            if packet.finding.rule_id == "T1-24":
                raise RuntimeError("model down")
            return Tier3Assessment(assessment=f"ok {packet.finding.rule_id}",
                                   severity=packet.finding.severity)

    subset = list(findings)
    assert len(subset) > 1                       # exercise the threaded path
    judge = _StubJudge(client=object())          # injected; stub assess never uses it
    packets = build_packets(subset, ctx)
    assert any(p.finding.rule_id == "T1-24" for p in packets)  # error path is exercised
    out = judge.assess_all(packets)

    assert len(out) == len(packets)
    for packet, assessment in zip(packets, out, strict=True):
        if packet.finding.rule_id == "T1-24":
            assert "unavailable" in assessment.assessment      # degraded, not dropped
        else:
            assert assessment.assessment == f"ok {packet.finding.rule_id}"


def test_anthropic_judge_empty_response_degrades_not_crashes(ctx, findings):
    # A refusal / thinking-only reply has no text block — must surface as a clear
    # failed assessment that preserves severity, not a JSONDecodeError.
    client = _FakeClient("")
    judge = AnthropicJudge(client=client)
    crit = find(findings, "T1-01")
    f = Finding(crit.rule_id, crit.severity, crit.entity_ids, crit.question,
                transactions=crit.transactions)
    packets = build_packets([f], ctx)
    apply_tier3([f], packets, judge)
    assert f.severity == Severity.CRITICAL
    assert "unavailable" in f.ai_assessment
