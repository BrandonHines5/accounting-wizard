"""Tier 1 rule engine.

Rules register themselves with @rule(...). The runner executes every
implemented rule over the canonical dataset for all active registry entities
and applies entity-based severity floors. Rules that need sources we don't
ingest yet are declared with implemented=False so the Methodology sheet shows
honest coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.config import RulesConfig
from core.entities import EntityRegistry
from core.findings import Finding, apply_entity_severity_floor


@dataclass
class RunContext:
    transactions: pd.DataFrame   # canonical, validated
    vendors: pd.DataFrame        # canonical, validated
    registry: EntityRegistry
    config: RulesConfig

    @property
    def active_entity_ids(self) -> list[str]:
        return [e.id for e in self.registry.active()]

    def entity_transactions(self, entity_id: str) -> pd.DataFrame:
        return self.transactions[self.transactions["entity_id"] == entity_id]


@dataclass
class RuleSpec:
    rule_id: str
    title: str
    implemented: bool
    requires: str                 # data sources needed
    func: callable = None
    notes: str = ""


_RULES: dict[str, RuleSpec] = {}


def rule(rule_id: str, title: str, requires: str, implemented: bool = True, notes: str = ""):
    def decorator(func):
        _RULES[rule_id] = RuleSpec(rule_id, title, implemented, requires, func, notes)
        return func
    return decorator


def pending_rule(rule_id: str, title: str, requires: str, notes: str = ""):
    """Declare a spec'd rule whose data source isn't ingested yet."""
    _RULES[rule_id] = RuleSpec(rule_id, title, False, requires, None, notes)


def all_rules() -> list[RuleSpec]:
    return sorted(_RULES.values(), key=lambda r: r.rule_id)


def run_all(ctx: RunContext) -> list[Finding]:
    entities_by_id = {e.id: e for e in ctx.registry}
    findings: list[Finding] = []
    for spec in all_rules():
        if not spec.implemented:
            continue
        for finding in spec.func(ctx):
            findings.append(apply_entity_severity_floor(finding, entities_by_id))
    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return findings
