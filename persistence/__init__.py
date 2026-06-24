"""Persistence + disposition memory (Phase 2 baseline).

Two jobs: keep a history of findings, and enforce the standing principle that a
cleared finding never resurfaces while a repeated pattern after a clear escalates
instead. `apply_disposition_memory` is the policy; `FindingsStore` is the I/O
boundary (an in-memory implementation for tests, a Supabase-backed one for
production). The Tier 3 layer consumes the same prior-findings history for
context, so this is what turns its `prior_findings` path from a stub into live
input.
"""
from persistence.findings_store import (
    FindingsStore,
    InMemoryFindingsStore,
    apply_disposition_memory,
)

__all__ = [
    "FindingsStore",
    "InMemoryFindingsStore",
    "apply_disposition_memory",
]
