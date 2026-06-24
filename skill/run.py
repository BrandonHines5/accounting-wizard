"""Weekly run: ingest export drops → Tier 1 rules → Tier 3 AI review → workbook.

Usage:
    python -m skill.run [--data-dir data] [--output output/exceptions.xlsx]
                        [--entity <id> ...] [--tier3 auto|on|off|heuristic]
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

from core.config import RulesConfig
from core.entities import REPO_ROOT, EntityRegistry
from core.model import validate_transactions, validate_vendors
from ingest.normalize import ingest_data_dir, load_mappings
from reporting.workbook import write_workbook
import rules  # noqa: F401  — registers all rule modules
from rules.engine import RunContext, run_all
from tier3 import HeuristicJudge, apply_tier3, build_packets


def _make_judge(mode: str, model: str | None):
    """Resolve --tier3 mode to a judge, or None to skip Tier 3.

    auto: Claude judge if ANTHROPIC_API_KEY is set, else skip (no degraded run).
    on: Claude judge, required.  heuristic: deterministic offline judge.  off: skip.
    """
    if mode == "off":
        return None
    if mode == "heuristic":
        return HeuristicJudge()
    if mode == "auto" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  Tier 3 skipped (no ANTHROPIC_API_KEY; use --tier3 heuristic for offline triage).")
        return None
    from tier3.anthropic_judge import MODEL, AnthropicJudge
    judge = AnthropicJudge(model=model or MODEL)
    if mode == "on":
        # Fail fast at startup rather than deep into the run: force the client so
        # a missing SDK or unresolved credentials errors before ingest/rules.
        try:
            _ = judge.client
        except Exception as exc:  # noqa: BLE001 — surface a clear startup error
            raise SystemExit(
                f"--tier3 on requires the Anthropic SDK and credentials: {exc}") from exc
    return judge


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
    parser.add_argument("--tier3", choices=["auto", "on", "off", "heuristic"],
                        default="auto",
                        help="AI judgment layer: auto (Claude if ANTHROPIC_API_KEY set, "
                             "else skip), on (require Claude), heuristic (offline), off.")
    parser.add_argument("--tier3-model", default=None,
                        help="Override the Claude model id for Tier 3.")
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

    judge = _make_judge(args.tier3, args.tier3_model)
    if judge is not None and findings:
        print(f"  Tier 3 review ({type(judge).__name__}) over {len(findings)} findings …")
        entities_by_id = {e.id: e for e in registry}
        packets = build_packets(findings, ctx)
        findings = apply_tier3(findings, packets, judge, entities_by_id)

    output = args.output or str(
        REPO_ROOT / "output" / f"exceptions_{datetime.now():%Y%m%d_%H%M}.xlsx")
    path = write_workbook(findings, registry, output)
    print(f"Workbook written: {path}")


if __name__ == "__main__":
    main()
