"""Entity registry.

The registry (config/entities.yaml) is the single source of truth for which
entities the detection battery covers. Rules must never hardcode entity names;
they iterate this registry and key behavior off entity attributes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = REPO_ROOT / "config" / "entities.yaml"

NONPROFIT_TYPES = {"nonprofit_501c3"}


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    legal_type: str
    active: bool = True
    aliases: tuple[str, ...] = field(default_factory=tuple)
    approval_threshold: float | None = None

    @property
    def is_nonprofit(self) -> bool:
        return self.legal_type in NONPROFIT_TYPES

    def matches_name(self, text: str) -> bool:
        """True if `text` refers to this entity (name or alias, case-insensitive,
        ignoring punctuation and legal suffixes — '13525WM, LLC' matches '13525WM')."""
        needle = _strip_legal_suffix(_norm(text))
        if not needle:
            return False
        candidates = [self.name, self.id, *self.aliases]
        return any(_strip_legal_suffix(_norm(c)) == needle for c in candidates)


class EntityRegistry:
    def __init__(self, entities: list[Entity]):
        self._entities = {e.id: e for e in entities}
        if len(self._entities) != len(entities):
            raise ValueError("Duplicate entity ids in registry")

    @classmethod
    def load(cls, path: Path | str = DEFAULT_REGISTRY_PATH) -> "EntityRegistry":
        raw = yaml.safe_load(Path(path).read_text())
        entities = [
            Entity(
                id=item["id"],
                name=item["name"],
                legal_type=item["legal_type"],
                active=item.get("active", True),
                aliases=tuple(item.get("aliases", [])),
                approval_threshold=item.get("approval_threshold"),
            )
            for item in raw["entities"]
        ]
        return cls(entities)

    def __iter__(self):
        return iter(self._entities.values())

    def __len__(self) -> int:
        return len(self._entities)

    def get(self, entity_id: str) -> Entity:
        return self._entities[entity_id]

    def active(self) -> list[Entity]:
        return [e for e in self if e.active]

    def resolve_name(self, text: str) -> Entity | None:
        """Resolve free text (e.g. counterparty in a 'Due to X' account) to an entity."""
        for entity in self:
            if entity.matches_name(text):
                return entity
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _strip_legal_suffix(normed: str) -> str:
    return re.sub(r"(llc|inc|corp|ltd)$", "", normed)
