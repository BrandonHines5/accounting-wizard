"""Rule parameter configuration (config/rules.yaml)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from core.entities import Entity, REPO_ROOT

DEFAULT_RULES_PATH = REPO_ROOT / "config" / "rules.yaml"


class RulesConfig:
    def __init__(self, raw: dict):
        self._raw = raw
        self.defaults: dict = raw.get("defaults", {})
        self.intercompany_account_pattern: str = raw.get(
            "intercompany_account_pattern", r"(?i)due\s+(to|from)\s+(.+)"
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_RULES_PATH) -> "RulesConfig":
        return cls(yaml.safe_load(Path(path).read_text()))

    def param(self, key: str):
        return self.defaults[key]

    def patterns(self, key: str) -> list[re.Pattern]:
        """Compile a defaults list of case-insensitive regexes (empty when absent).
        Shared by the rule modules so pattern semantics — case-insensitivity, empty
        handling — can't silently diverge between Tier 1 and Tier 4."""
        return [re.compile(p, re.IGNORECASE) for p in (self.defaults.get(key) or [])]

    def approval_threshold(self, entity: Entity) -> float:
        """Per-entity AP approval threshold, falling back to the global default."""
        if entity.approval_threshold is not None:
            return float(entity.approval_threshold)
        return float(self.defaults["approval_threshold"])
