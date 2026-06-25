"""Weekly run: ingest → Tier 1 rules → Tier 4 bank reconciliation → disposition
memory → Tier 3 → workbook.

Tier 4 runs only when config/bank_accounts.yaml exists and matching statement
files are found under --bank-dir; otherwise it is skipped and the run is exactly
the Tier 1 pipeline. Tier 4 findings flow through the same disposition memory,
Tier 3 review, persistence, and workbook as every other finding.

Usage:
    python -m skill.run [--data-dir data] [--output output/exceptions.xlsx]
                        [--entity <id> ...] [--tier3 auto|on|off|heuristic]
                        [--store none|supabase]
                        [--bank-dir <dir>] [--bank-accounts config/bank_accounts.yaml]
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
from persistence import apply_disposition_memory
from reporting.workbook import write_workbook
import rules  # noqa: F401  — registers all Tier 1 rule modules
import analytics  # noqa: F401  — registers all Tier 2 statistical rules
import bank.methodology  # noqa: F401  — lists Tier 4 on the Methodology sheet
from rules.engine import RunContext, run_all
from tier3 import HeuristicJudge, apply_tier3, build_packets


def _make_store(mode: str, schema: str | None):
    """Resolve --store mode to a FindingsStore, or None to disable persistence."""
    if mode == "none":
        return None
    from persistence.supabase_store import SupabaseFindingsStore
    return SupabaseFindingsStore.from_env(schema)


def _sync_sources(schema, registry, transactions, vendors) -> None:
    """Mirror the registry and canonical source data to Supabase. Entities go
    first: transactions/vendors/bank_transactions all FK to entities(id), so the
    targets must exist before anything referencing them is written."""
    from persistence.source_store import (EntityRegistryStore, TransactionStore,
                                          VendorStore)
    EntityRegistryStore.from_env(schema).save(registry)
    n_v = VendorStore.from_env(schema).save(vendors)
    n_t = TransactionStore.from_env(schema).save(transactions)
    print(f"  Synced {len(registry)} entities, {n_v} vendors, {n_t} transactions to Supabase")


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


def _run_tier4(args, registry, config, transactions, known_ids: set[str]) -> list:
    """Extract configured bank statements and reconcile them against the books.

    Skips cleanly (returning []) when Tier 4 isn't set up — no account registry,
    no statements directory, or nothing matching the window — so a Tier-1-only run
    needs no extra flags. Findings are merged into the main list and reviewed like
    any other."""
    accounts_path = Path(args.bank_accounts)
    if not accounts_path.exists():
        return []
    from bank.accounts import extract_statements, load_bank_accounts
    from bank.reconcile import reconcile_all

    accounts = load_bank_accounts(accounts_path)
    if args.entity:
        accounts = [a for a in accounts if a.entity_id in set(args.entity)]
    if not accounts:
        return []

    bank_dir = Path(args.bank_dir) if args.bank_dir else Path(args.data_dir) / "bank"
    if not bank_dir.exists():
        print(f"  Tier 4 skipped (no bank statements dir at {bank_dir}).")
        return []

    bank = extract_statements(
        accounts, bank_dir, known_ids,
        on_error=lambda path, exc: print(f"  Tier 4: skipped {path.name} — {exc}"))
    if args.since:
        bank = bank[bank["date"] >= args.since]
    if args.until:
        bank = bank[bank["date"] <= args.until]
    if bank.empty:
        print("  Tier 4 skipped (no bank statement lines matched the window).")
        return []

    print(f"  Tier 4: reconciling {len(bank)} bank lines across "
          f"{bank['entity_id'].nunique()} account-entities …")
    findings = reconcile_all(transactions, bank, registry, config)
    print(f"  {len(findings)} reconciliation findings")
    findings += _run_check_images(args, registry, config, bank, transactions, accounts)
    if args.store == "supabase":
        _persist_bank(bank, args.supabase_schema)
    return findings


def _make_check_reader(mode: str, model: str | None):
    """Resolve --check-images mode to a CheckReader, or None to skip vision reads."""
    if mode == "off":
        return None
    if mode == "auto" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  Check-image reads skipped (no ANTHROPIC_API_KEY).")
        return None
    from bank.check_images import MODEL, AnthropicCheckReader
    return AnthropicCheckReader(model=model or MODEL)


def _check_image_source_factory(args):
    """Return a callable `account -> CheckImageSource`, or None to skip image reads
    (missing local dir, or graph selected without GRAPH_* credentials)."""
    def cfg(account):
        return account.check_images

    if args.check_image_source == "graph":
        required = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
                    "GRAPH_DRIVE_ID")
        if any(var not in os.environ for var in required):
            print("  Check-image reads skipped (graph source needs GRAPH_* env vars).")
            return None
        from bank.check_image_source import GraphCheckImages
        return lambda account: GraphCheckImages.from_env(
            folder=cfg(account).get("dir", ""),
            front_pattern=cfg(account).get("front", "{check_no}_front.jpg"),
            back_pattern=cfg(account).get("back", "{check_no}_back.jpg"),
            label=account.label)

    image_dir = (Path(args.check_image_dir) if args.check_image_dir
                 else Path(args.data_dir) / "check-images")
    if not image_dir.exists():
        print(f"  Check-image reads skipped (no image dir at {image_dir}).")
        return None
    from bank.check_image_source import LocalCheckImages
    return lambda account: LocalCheckImages(
        image_dir / (cfg(account).get("dir") or ""),
        front_pattern=cfg(account).get("front", "{check_no}_front.jpg"),
        back_pattern=cfg(account).get("back", "{check_no}_back.jpg"),
        label=account.label)


def _run_check_images(args, registry, config, bank, transactions, accounts) -> list:
    """Read cancelled-check images for accounts that configure them and emit
    T4-03/04/05 findings. Skips cleanly when reads are off, no account configures
    images, no reader is available, or the image source isn't reachable."""
    accts = [a for a in accounts if a.check_images]
    if not accts:
        return []
    reader = _make_check_reader(args.check_images, args.check_image_model)
    if reader is None:
        return []
    make_source = _check_image_source_factory(args)
    if make_source is None:
        return []

    from bank.check_images import verify_check_images
    from core.fingerprint import account_fingerprint

    findings: list = []
    for account in accts:
        mask = bank["account_fingerprint"] == account_fingerprint(account.account_number())
        if not mask.any():
            continue
        source = make_source(account)
        enriched, account_findings = verify_check_images(
            source.attach(bank[mask]), transactions, reader, registry, config,
            fetch_front=source.read_front, fetch_back=source.read_back,
            media_type=source.media_type)
        # Write the reads back into the shared bank frame so persistence keeps them.
        read_cols = ["image_ref", "payee_read", "amount_read", "read_confidence"]
        bank.loc[enriched.index, read_cols] = enriched[read_cols]
        findings += account_findings
    if findings:
        print(f"  {len(findings)} check-image findings")
    return findings


def _persist_bank(bank, schema) -> None:
    from persistence.bank_store import BankTransactionsStore
    count = BankTransactionsStore.from_env(schema).save(bank)
    print(f"  Tier 4: persisted {count} bank lines to Supabase")


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
    parser.add_argument("--store", choices=["none", "supabase"], default="none",
                        help="Findings history for disposition memory: none (default) "
                             "or supabase (needs SUPABASE_URL + SUPABASE_SERVICE_KEY).")
    parser.add_argument("--supabase-schema", default=None,
                        help="Supabase schema holding the findings table "
                             "(default: financial_forensics).")
    parser.add_argument("--bank-dir", default=None,
                        help="Directory of bank statement exports for Tier 4 "
                             "(default: <data-dir>/bank). Gitignored.")
    parser.add_argument("--bank-accounts",
                        default=str(REPO_ROOT / "config" / "bank_accounts.yaml"),
                        help="Tier 4 account registry (see bank_accounts.example.yaml). "
                             "Tier 4 is skipped if this file does not exist.")
    parser.add_argument("--check-images", choices=["auto", "on", "off"], default="auto",
                        help="Tier 4 cancelled-check vision reads (T4-03/04/05): auto "
                             "(Claude if ANTHROPIC_API_KEY set + images present, else skip), "
                             "on, off.")
    parser.add_argument("--check-image-source", choices=["local", "graph"], default="local",
                        help="Where check images come from: local (synced under "
                             "--check-image-dir) or graph (SharePoint via Microsoft "
                             "Graph; needs GRAPH_* env vars).")
    parser.add_argument("--check-image-dir", default=None,
                        help="Directory of locally-synced cancelled-check images "
                             "(default: <data-dir>/check-images). Gitignored.")
    parser.add_argument("--check-image-model", default=None,
                        help="Override the Claude model id for check-image reads.")
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

    # Seed Supabase (registry first — it's the FK target) before anything that
    # references entities (transactions, vendors, bank lines) is persisted.
    if args.store == "supabase":
        _sync_sources(args.supabase_schema, registry, transactions, vendors)

    ctx = RunContext(transactions=transactions, vendors=vendors,
                     registry=registry, config=config)
    findings = run_all(ctx)
    print(f"  {len(findings)} Tier 1–2 rule findings")

    findings += _run_tier4(args, registry, config, transactions, known_ids)

    entities_by_id = {e.id: e for e in registry}

    # Disposition memory: drop exact re-occurrences a human already cleared, and
    # escalate patterns that recur after a clear. Runs before Tier 3 so we don't
    # review suppressed findings. `prior` also feeds Tier 3 context.
    store = _make_store(args.store, args.supabase_schema)
    prior = store.load_prior() if store is not None else None
    suppressed: list = []
    if prior is not None and len(prior):
        findings, suppressed = apply_disposition_memory(findings, prior, entities_by_id)
        if suppressed:
            print(f"  {len(suppressed)} suppressed by disposition memory "
                  f"(previously resolved); {len(findings)} active")

    judge = _make_judge(args.tier3, args.tier3_model)
    if judge is not None and findings:
        print(f"  Tier 3 review ({type(judge).__name__}) over {len(findings)} findings …")
        packets = build_packets(findings, ctx, prior_findings=prior)
        findings = apply_tier3(findings, packets, judge, entities_by_id)

    if store is not None:
        store.save(findings)

    output = args.output or str(
        REPO_ROOT / "output" / f"exceptions_{datetime.now():%Y%m%d_%H%M}.xlsx")
    path = write_workbook(findings, registry, output, suppressed=suppressed)
    print(f"Workbook written: {path}")


if __name__ == "__main__":
    main()
