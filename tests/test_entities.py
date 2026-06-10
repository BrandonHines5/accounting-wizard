import pytest

from core.entities import EntityRegistry


def test_registry_loads_all_entities(registry):
    assert len(registry) == 4


def test_active_excludes_wound_down_entities(registry):
    active_ids = {e.id for e in registry.active()}
    assert active_ids == {"alpha", "beta", "charity"}


def test_nonprofit_flag_comes_from_legal_type(registry):
    assert registry.get("charity").is_nonprofit
    assert not registry.get("alpha").is_nonprofit


def test_resolve_name_handles_aliases_and_case(registry):
    assert registry.resolve_name("Beta Construction").id == "beta"
    assert registry.resolve_name("ALPHA BUILDERS").id == "alpha"
    assert registry.resolve_name("CCH").id == "charity"
    assert registry.resolve_name("Some Unknown Co") is None


def test_real_registry_in_config_is_valid():
    # The committed config must always load — onboarding an entity is editing it.
    registry = EntityRegistry.load()
    assert len(registry.active()) >= 1
    assert all(e.id and e.name and e.legal_type for e in registry)


def test_duplicate_ids_rejected(registry):
    from core.entities import Entity
    e = registry.get("alpha")
    with pytest.raises(ValueError):
        EntityRegistry([e, e])
