"""Rule parameter configuration (config/rules.yaml)."""
from __future__ import annotations

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

    def approval_threshold(self, entity: Entity) -> float:
        """Per-entity AP approval threshold, falling back to the global default."""
        if entity.approval_threshold is not None:
            return float(entity.approval_threshold)
        return float(self.defaults["approval_threshold"])
