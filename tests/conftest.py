import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config import RulesConfig            # noqa: E402
from core.entities import EntityRegistry       # noqa: E402
from core.model import validate_transactions, validate_vendors  # noqa: E402
import rules                                   # noqa: E402, F401
from rules.engine import RunContext            # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def registry() -> EntityRegistry:
    return EntityRegistry.load(FIXTURES / "entities.yaml")


@pytest.fixture(scope="session")
def config() -> RulesConfig:
    return RulesConfig.load()


@pytest.fixture(scope="session")
def ctx(registry, config) -> RunContext:
    known = {e.id for e in registry}
    transactions = validate_transactions(pd.read_csv(FIXTURES / "transactions.csv"), known)
    vendors = validate_vendors(pd.read_csv(FIXTURES / "vendors.csv"), known)
    return RunContext(transactions=transactions, vendors=vendors,
                      registry=registry, config=config)
