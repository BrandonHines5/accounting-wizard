"""Weekly Tier 1 run: ingest export drops → rule battery → exceptions workbook.

Usage:
    python -m skill.run [--data-dir data] [--output output/exceptions.xlsx]
                        [--entity <id> ...]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from core.config import RulesConfig
from core.entities import REPO_ROOT, EntityRegistry
from core.model import validate_transactions, validate_vendors
from ingest.normalize import ingest_data_dir, load_mappings
from reporting.workbook import write_workbook
import rules  # noqa: F401  — registers all rule modules
from rules.engine import RunContext, run_all


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Tier 1 forensics battery.")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--entity", action="append", default=None,
                        help="Limit to specific entity id(s); default = all active")
    parser.add_argument("--since", default=None,
                        help="Only analyze transactions on/after this date (YYYY-MM-DD). "
                             "Weekly runs should scope to the recent window.")
    parser.add_argument("--until", default=None,
                        help="Only analyze transactions on/before this date (YYYY-MM-DD)")
    args = parser.parse_args()

    registry = EntityRegistry.load()
    config = RulesConfig.load()
    known_ids = {e.id for e in registry}

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        raise SystemExit(f"Data dir {data_dir} not found — drop weekly exports there first "
                         "(see skill/SKILL.md).")

    print(f"Ingesting exports from {data_dir} …")
    transactions, vendors = ingest_data_dir(data_dir, registry, load_mappings())
    transactions = validate_transactions(transactions, known_ids)
    vendors = validate_vendors(vendors, known_ids)

    if args.entity:
        unknown = set(args.entity) - known_ids
        if unknown:
            raise SystemExit(f"Unknown entity id(s): {sorted(unknown)} — see config/entities.yaml")
        transactions = transactions[transactions["entity_id"].isin(args.entity)]
        vendors = vendors[vendors["entity_id"].isin(args.entity)]

    if args.since:
        transactions = transactions[transactions["date"] >= args.since]
    if args.until:
        transactions = transactions[transactions["date"] <= args.until]

    print(f"  {len(transactions)} transactions, {len(vendors)} vendors across "
          f"{transactions['entity_id'].nunique()} entities"
          + (f" ({args.since or 'start'} → {args.until or 'latest'})"
             if args.since or args.until else ""))

    ctx = RunContext(transactions=transactions, vendors=vendors,
                     registry=registry, config=config)
    findings = run_all(ctx)
    print(f"  {len(findings)} findings")

    output = args.output or str(
        REPO_ROOT / "output" / f"exceptions_{datetime.now():%Y%m%d_%H%M}.xlsx")
    path = write_workbook(findings, registry, output)
    print(f"Workbook written: {path}")


if __name__ == "__main__":
    main()
