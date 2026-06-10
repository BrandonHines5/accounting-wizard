from core.findings import Finding, Severity, apply_entity_severity_floor


def entities_map(registry):
    return {e.id: e for e in registry}


def make(rule_id, severity, entity_ids):
    return Finding(rule_id=rule_id, severity=severity, entity_ids=entity_ids,
                   question="?")


def test_nonprofit_misallocation_floored_to_high(registry):
    f = make("T1-23", Severity.MEDIUM, ["charity"])
    out = apply_entity_severity_floor(f, entities_map(registry))
    assert out.severity == Severity.HIGH
    assert "501(c)(3)" in out.details["severity_note"]


def test_floor_only_applies_to_misallocation_rules(registry):
    f = make("T1-01", Severity.MEDIUM, ["charity"])
    assert apply_entity_severity_floor(f, entities_map(registry)).severity == Severity.MEDIUM


def test_floor_never_lowers_critical(registry):
    f = make("T1-24", Severity.CRITICAL, ["charity", "alpha"])
    assert apply_entity_severity_floor(f, entities_map(registry)).severity == Severity.CRITICAL


def test_for_profit_entities_unaffected(registry):
    f = make("T1-23", Severity.MEDIUM, ["alpha", "beta"])
    assert apply_entity_severity_floor(f, entities_map(registry)).severity == Severity.MEDIUM
