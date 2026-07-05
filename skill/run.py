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
from core.model import validate_cost_lines, validate_transactions, validate_vendors
from ingest.normalize import ingest_data_dir, load_mappings
from persistence import apply_disposition_memory
from reporting.workbook import write_workbook
import rules  # noqa: F401  — registers all Tier 1 rule modules
import analytics  # noqa: F401  — registers all Tier 2 statistical rules
import bank.methodology  # noqa: F401  — lists Tier 4 on the Methodology sheet
from rules.engine import RunContext, run_all
from tier3 import HeuristicJudge, apply_tier3, build_packets, select_for_review


def _qbo_window(cfg, since, until):
    """Report date window: --since/--until when given, else a rolling lookback so a
    weekly run pulls a comfortably-overlapping window (default 120 days)."""
    from datetime import date, timedelta

    end = until or date.today().isoformat()
    if since:
        return since, end
    lookback = int((cfg or {}).get("lookback_days", 120))
    return (date.today() - timedelta(days=lookback)).isoformat(), end


def _maybe_pull_qbo(args, registry) -> set:
    """Pull configured entities' reports straight from QuickBooks Online into the
    data dir before ingest, writing the same data/<entity_id>/qb__*.csv files the
    export path drops. Returns the set of entity ids sourced from QBO so the
    SharePoint pull skips them — each entity is pulled from exactly one place.

    Skips cleanly when not set up (no config or missing QBO_* creds) unless
    `--pull-qbo on` demanded it, so a local run needs no extra flags."""
    if args.pull_qbo == "off":
        return set()
    from ingest.qbo import (QBO_ENV, EnvRefreshTokenStore, QboAuth, QboClient,
                            api_base, load_qbo_config, pull_all, resolve_token_endpoint)

    cfg = load_qbo_config(args.qbo_config)
    if cfg is None:
        if args.pull_qbo == "on":
            raise SystemExit(f"--pull-qbo on needs {args.qbo_config} "
                             "(copy config/qbo.example.yaml)")
        return set()
    missing = [v for v in QBO_ENV if not os.environ.get(v)]  # unset OR empty
    if missing:
        if args.pull_qbo == "on":
            raise SystemExit(f"--pull-qbo on needs env: {', '.join(missing)}")
        print(f"  QBO pull skipped (missing {', '.join(missing)}).")
        return set()

    env_by_entity = {eid: (ent or {}).get("refresh_token_env")
                     for eid, ent in (cfg.get("entities") or {}).items()}
    realm_overrides: dict = {}
    if args.store == "supabase":
        from persistence.qbo_token_store import SupabaseRefreshTokenStore
        token_store = SupabaseRefreshTokenStore.from_env(args.supabase_schema, env_by_entity)
        # Companies authorized from the review UI store their realm + refresh token in
        # qbo_connections. Discover them so a connection is enough to get pulled — no
        # realm to copy into config/qbo.yaml — and include any not yet listed there.
        try:
            realm_overrides = token_store.list_connections()
        except Exception as exc:  # noqa: BLE001 — fall back to config realms
            print(f"  ~ QBO: could not read stored connections ({type(exc).__name__}); "
                  "using config/qbo.yaml realms.")
        if realm_overrides:
            merged = dict(cfg.get("entities") or {})
            for eid in realm_overrides:
                merged.setdefault(eid, {})
            cfg = {**cfg, "entities": merged}
            print(f"  {len(realm_overrides)} QBO connection(s) from the review UI")
    else:
        token_store = EnvRefreshTokenStore(env_by_entity)

    # Resolve the OAuth token endpoint from Intuit's discovery document (falls back
    # to the documented URL if discovery is unreachable) rather than hard-coding it.
    environment = (cfg.get("environment") or "production")
    token_endpoint = resolve_token_endpoint(environment)
    auth = QboAuth(os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"], token_store,
                   token_endpoint=token_endpoint)
    client = QboClient(auth, base_url=api_base(cfg))
    start, end = _qbo_window(cfg, args.since, args.until)
    print(f"Pulling reports from QuickBooks Online ({start} → {end}) …")
    pulled = pull_all(cfg, args.data_dir, registry, client=client, start=start, end=end,
                      entities=set(args.entity) if args.entity else None,
                      realm_overrides=realm_overrides,
                      on_file=lambda n, sz: print(f"  ↓ {n} ({sz // 1024} KB)"))
    total = sum(len(v) for v in pulled.values())
    print(f"  Pulled {total} file(s) across {len(pulled)} "
          f"entit{'y' if len(pulled) == 1 else 'ies'} from QBO")
    return set(pulled)


def _maybe_pull_sharepoint(args, registry, mappings, skip_entities=None) -> None:
    """Download configured SharePoint folders into the data dir before ingest.

    `skip_entities` (those already pulled from QBO) are excluded so an entity is
    sourced from exactly one place. Skips cleanly when not set up (no config or
    missing GRAPH_* env) unless `--pull-sharepoint on` demanded it, so a local run
    needs no extra flags."""
    if args.pull_sharepoint == "off":
        return
    from ingest.sharepoint import GRAPH_ENV, load_sharepoint_config, pull_all

    cfg = load_sharepoint_config(args.sharepoint_config)
    if cfg is None:
        if args.pull_sharepoint == "on":
            raise SystemExit(f"--pull-sharepoint on needs {args.sharepoint_config} "
                             "(copy config/sharepoint.example.yaml)")
        return
    if skip_entities:
        # Any entity already pulled from QBO is dropped from the SharePoint folder
        # pull so its data dir isn't populated from two sources (bank-statement pulls
        # below are driven by bank_accounts.yaml and are unaffected).
        cfg = {**cfg, "entities": {eid: e for eid, e in (cfg.get("entities") or {}).items()
                                   if eid not in skip_entities}}
    has_drive = bool(cfg.get("drive_id") or os.environ.get("GRAPH_DRIVE_ID"))
    missing = [v for v in GRAPH_ENV if not os.environ.get(v)]  # unset OR empty
    if missing or not has_drive:
        need = missing + ([] if has_drive else ["GRAPH_DRIVE_ID"])
        if args.pull_sharepoint == "on":
            raise SystemExit(f"--pull-sharepoint on needs env: {', '.join(need)}")
        print(f"  SharePoint pull skipped (missing {', '.join(need)}).")
        return
    print("Pulling exports from SharePoint …")
    pulled = pull_all(cfg, args.data_dir, registry, mappings,
                      on_file=lambda n, sz: print(f"  ↓ {n} ({sz // 1024} KB)"))
    total = sum(len(v) for v in pulled.values())
    print(f"  Pulled {total} file(s) across {len(pulled)} "
          f"entit{'y' if len(pulled) == 1 else 'ies'}")
    _pull_bank_statements(args, registry, cfg)


def _pull_bank_statements(args, registry, cfg) -> None:
    """Pull each configured account's statements from SharePoint into the bank-dir
    before Tier 4. Skips silently when no account names a `sharepoint_folder`."""
    accounts_path = Path(args.bank_accounts)
    if not accounts_path.exists():
        return
    from bank.accounts import load_bank_accounts
    from ingest.sharepoint import pull_bank_statements

    accounts = load_bank_accounts(accounts_path)
    if args.entity:
        accounts = [a for a in accounts if a.entity_id in set(args.entity)]
    accounts = [a for a in accounts if a.sharepoint_folder]
    if not accounts:
        return
    bank_dir = Path(args.bank_dir) if args.bank_dir else Path(args.data_dir) / "bank"
    print("Pulling bank statements from SharePoint …")
    pulled = pull_bank_statements(accounts, bank_dir, config=cfg,
                                  on_file=lambda n, sz: print(f"  ↓ {n} ({sz // 1024} KB)"))
    total = sum(len(v) for v in pulled.values())
    print(f"  Pulled {total} statement file(s) across {len(pulled)} account(s)")


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


def _tier3_cap(mode: str) -> int:
    """Max NEW findings to send to Tier 3 in one run (TIER3_MAX_REVIEW; 0 =
    unlimited). Bounds wall-clock so a large first-time backlog can't exceed the
    job time limit — the rest are reviewed on later runs as this run's reviews
    persist and stop being re-reviewed. Already-assessed findings are carried
    forward for free and don't count against the cap.

    Batch mode exists precisely to drain a backlog in one run, so it defaults to
    unlimited; synchronous modes default to 200. TIER3_MAX_REVIEW overrides both."""
    default = "0" if mode == "batch" else "200"
    try:
        return max(0, int(os.environ.get("TIER3_MAX_REVIEW", default)))
    except ValueError:
        return int(default)


def _make_judge(mode: str, model: str | None):
    """Resolve --tier3 mode to a judge, or None to skip Tier 3.

    auto: Claude judge if ANTHROPIC_API_KEY is set, else skip (no degraded run).
    on: Claude judge, required.  batch: Claude via the Message Batches API (half
    cost, built for large backlogs), required.  heuristic: deterministic offline
    judge.  off: skip.
    """
    if mode == "off":
        return None
    if mode == "heuristic":
        return HeuristicJudge()
    if mode == "auto" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  Tier 3 skipped (no ANTHROPIC_API_KEY; use --tier3 heuristic for offline triage).")
        return None
    from tier3.anthropic_judge import MODEL, AnthropicBatchJudge, AnthropicJudge
    cls = AnthropicBatchJudge if mode == "batch" else AnthropicJudge
    judge = cls(model=model or MODEL)
    if mode in ("on", "batch"):
        # Fail fast at startup rather than deep into the run: force the client so
        # a missing SDK or unresolved credentials errors before ingest/rules.
        try:
            _ = judge.client
        except Exception as exc:  # noqa: BLE001 — surface a clear startup error
            raise SystemExit(
                f"--tier3 {mode} requires the Anthropic SDK and credentials: {exc}") from exc
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
    findings += _run_check_images(args, registry, config, bank, transactions, accounts, bank_dir)
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


def _statement_pdf_check_source(account, bank_dir):
    """Build a check-image source from the account's own statement PDF(s): render the
    embedded cancelled-check faces (keyed by check number). Returns None — skipping
    the account, never erroring — if the layout is missing or no images are found.

    With `check_images.latest_only`, images are read from only the most recent
    statement file (statement filenames sort chronologically: EStatement_..._YYYYMMDD),
    so a multi-month backfill reconciles every statement but vision-reads just the
    latest month's checks."""
    from bank.check_image_source import PdfStatementCheckImages
    from bank.statement_extract import extract_check_images

    if not account.layout:
        print(f"  Check-image reads skipped for {account.entity_id}/{account.label} "
              "(statement_pdf source needs a `layout`).")
        return None
    paths = sorted(Path(bank_dir).glob(account.statement_glob))
    if account.check_images.get("latest_only") and paths:
        paths = paths[-1:]
    images: dict[str, bytes] = {}
    for path in paths:
        try:
            images.update(extract_check_images(path, layout=account.layout))
        except Exception as exc:  # noqa: BLE001 — one bad statement shouldn't sink reads
            print(f"  Check-image extraction skipped for {path.name}: {exc}")
    if not images:
        print(f"  Check-image reads skipped for {account.entity_id}/{account.label} "
              "(no embedded check images found in the statement).")
        return None
    print(f"  Extracted {len(images)} check image(s) from "
          f"{len(paths)} statement(s) for {account.entity_id}/{account.label}")
    return PdfStatementCheckImages(images, label=account.label)


def _check_image_source_factory(args, bank_dir):
    """Return a callable `account -> CheckImageSource | None`. An account configured
    with `check_images.source: statement_pdf` reads its cancelled-check images out of
    its own statement; otherwise images come from the local sync / SharePoint.

    The local/graph fallback is built lazily, so an all-statement_pdf run never emits
    a spurious 'no image dir' skip message for a backend it doesn't use."""
    fallback = {"factory": None, "built": False}

    def make_source(account):
        if (account.check_images or {}).get("source") == "statement_pdf":
            return _statement_pdf_check_source(account, bank_dir)
        if not fallback["built"]:
            fallback["factory"] = _file_check_image_source_factory(args)
            fallback["built"] = True
        return fallback["factory"](account) if fallback["factory"] is not None else None

    return make_source


def _file_check_image_source_factory(args):
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


def _run_check_images(args, registry, config, bank, transactions, accounts, bank_dir) -> list:
    """Read cancelled-check images for accounts that configure them and emit
    T4-03/04/05 findings. Skips cleanly when reads are off, no account configures
    images, no reader is available, or the image source isn't reachable."""
    accts = [a for a in accounts if a.check_images]
    if not accts:
        return []
    reader = _make_check_reader(args.check_images, args.check_image_model)
    if reader is None:
        return []
    make_source = _check_image_source_factory(args, bank_dir)

    from bank.check_images import verify_check_images
    from core.fingerprint import account_fingerprint

    findings: list = []
    for account in accts:
        mask = bank["account_fingerprint"] == account_fingerprint(account.account_number())
        if not mask.any():
            continue
        source = make_source(account)
        if source is None:
            continue
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
    parser.add_argument("--pull-sharepoint", choices=["auto", "on", "off"], default="off",
                        help="Pull weekly exports from SharePoint via Microsoft Graph "
                             "into the data dir before ingest (needs "
                             "config/sharepoint.yaml + GRAPH_* env). auto: pull if both "
                             "are present, else skip; on: require them.")
    parser.add_argument("--sharepoint-config",
                        default=str(REPO_ROOT / "config" / "sharepoint.yaml"),
                        help="SharePoint pull config (see config/sharepoint.example.yaml).")
    parser.add_argument("--pull-qbo", choices=["auto", "on", "off"], default="off",
                        help="Pull reports straight from QuickBooks Online via the "
                             "Intuit API into the data dir before ingest (needs "
                             "config/qbo.yaml + QBO_CLIENT_ID/QBO_CLIENT_SECRET + each "
                             "entity's refresh-token secret). auto: pull if the config "
                             "and app creds are present, else skip; on: require them. "
                             "Entities pulled from QBO are skipped by the SharePoint pull.")
    parser.add_argument("--qbo-config",
                        default=str(REPO_ROOT / "config" / "qbo.yaml"),
                        help="QuickBooks Online pull config (see config/qbo.example.yaml).")
    parser.add_argument("--entity", action="append", default=None,
                        help="Limit to specific entity id(s); default = all active")
    parser.add_argument("--since", default=None,
                        help="Only analyze transactions on/after this date (YYYY-MM-DD). "
                             "Weekly runs should scope to the recent window.")
    parser.add_argument("--until", default=None,
                        help="Only analyze transactions on/before this date (YYYY-MM-DD)")
    parser.add_argument("--tier3", choices=["auto", "on", "batch", "off", "heuristic"],
                        default="auto",
                        help="AI judgment layer: auto (Claude if ANTHROPIC_API_KEY set, "
                             "else skip), on (require Claude, one call per finding), "
                             "batch (require Claude via the Message Batches API — half "
                             "cost, made for large backlogs), heuristic (offline), off.")
    parser.add_argument("--tier3-model", default=None,
                        help="Override the Claude model id for Tier 3.")
    parser.add_argument("--store", choices=["none", "supabase"], default="none",
                        help="Findings history for disposition memory: none (default) "
                             "or supabase (needs SUPABASE_URL + SUPABASE_SERVICE_KEY).")
    parser.add_argument("--supabase-schema", default=None,
                        help="Supabase schema holding the findings table "
                             "(default: financial_forensics).")
    parser.add_argument("--update-baselines", action="store_true",
                        help="After the run, recompute Tier 2 baselines (vendor "
                             "share-of-spend) from this data and store them "
                             "(needs --store supabase).")
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
    mappings = load_mappings()
    # Pull QBO entities straight from the Intuit API, then SharePoint for the rest
    # (Hines Homes, Titan House). QBO-sourced entities are skipped by the SharePoint
    # pull so each entity's data dir comes from exactly one source.
    qbo_ids = _maybe_pull_qbo(args, registry)
    _maybe_pull_sharepoint(args, registry, mappings, skip_entities=qbo_ids)
    if not data_dir.exists():
        raise SystemExit(f"Data dir {data_dir} not found — drop weekly exports there first "
                         "(see skill/SKILL.md).")

    print(f"Ingesting exports from {data_dir} …")
    transactions, vendors, cost_lines = ingest_data_dir(data_dir, registry, mappings)
    transactions = validate_transactions(transactions, known_ids)
    vendors = validate_vendors(vendors, known_ids)
    cost_lines = validate_cost_lines(cost_lines, known_ids)

    if args.entity:
        unknown = set(args.entity) - known_ids
        if unknown:
            raise SystemExit(f"Unknown entity id(s): {sorted(unknown)} — see config/entities.yaml")
        transactions = transactions[transactions["entity_id"].isin(args.entity)]
        vendors = vendors[vendors["entity_id"].isin(args.entity)]
        cost_lines = cost_lines[cost_lines["entity_id"].isin(args.entity)]

    if args.since:
        transactions = transactions[transactions["date"] >= args.since]
        cost_lines = cost_lines[cost_lines["date"] >= args.since]
    if args.until:
        transactions = transactions[transactions["date"] <= args.until]
        cost_lines = cost_lines[cost_lines["date"] <= args.until]

    print(f"  {len(transactions)} transactions, {len(vendors)} vendors, "
          f"{len(cost_lines)} cost lines across "
          f"{transactions['entity_id'].nunique()} entities"
          + (f" ({args.since or 'start'} → {args.until or 'latest'})"
             if args.since or args.until else ""))

    # Seed Supabase (registry first — it's the FK target) before anything that
    # references entities (transactions, vendors, bank lines) is persisted, and
    # load any prior Tier 2 baselines so T2-05 has something to compare against.
    baselines = None
    prior_vendors = None
    if args.store == "supabase":
        from persistence.baseline_store import BaselineStore
        from persistence.source_store import VendorStore
        # Read the prior vendor master BEFORE re-syncing (T1-14 diffs against it).
        prior_vendors = VendorStore.from_env(args.supabase_schema).load()
        _sync_sources(args.supabase_schema, registry, transactions, vendors)
        baselines = BaselineStore.from_env(args.supabase_schema).load()
        if len(baselines):
            print(f"  Loaded {len(baselines)} Tier 2 baselines")

    ctx = RunContext(transactions=transactions, vendors=vendors,
                     registry=registry, config=config, baselines=baselines,
                     prior_vendors=prior_vendors, cost_lines=cost_lines)
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
    reviewed: list = []
    if judge is not None and findings:
        # Incremental: reuse the stored assessment for findings already reviewed in a
        # prior run; only send genuinely new ones to the model — plus, when this run
        # HAS a model judge, ones whose stored assessment was only a provisional
        # heuristic stub — capped per run so a large first-time backlog still finishes
        # inside the job's time limit (the remainder is reviewed on later runs once
        # these persist).
        to_review, carried, deferred = select_for_review(
            findings, prior, _tier3_cap(args.tier3), judge_kind=judge.kind)
        if to_review:
            note = f"{len(carried)} carried forward" if carried else ""
            if deferred:
                note += f", {deferred} deferred to a later run" if note else \
                        f"{deferred} deferred to a later run"
            print(f"  Tier 3 review ({type(judge).__name__}) over {len(to_review)} new "
                  f"finding(s){f' ({note})' if note else ''} …")
            packets = build_packets(to_review, ctx, prior_findings=prior)
            apply_tier3(to_review, packets, judge, entities_by_id)
            reviewed = to_review
        elif carried:
            print(f"  Tier 3: all {len(carried)} findings carried forward from prior "
                  "review; nothing new to assess.")
        findings.sort(key=lambda f: (-int(f.severity), f.rule_id))

    if store is not None:
        store.save(findings)
        # Persist this run's fresh assessments onto their (now-existing) rows so the
        # next run recognizes them as already-reviewed and skips them.
        if reviewed:
            store.persist_assessments(reviewed)

    if args.update_baselines and args.store == "supabase":
        from analytics.baselines import vendor_share_baselines
        from persistence.baseline_store import BaselineStore
        records = vendor_share_baselines(cost_lines, set(ctx.active_entity_ids))
        print(f"  Updated {BaselineStore.from_env(args.supabase_schema).save(records)} "
              "Tier 2 baselines")

    output = args.output or str(
        REPO_ROOT / "output" / f"exceptions_{datetime.now():%Y%m%d_%H%M}.xlsx")
    path = write_workbook(findings, registry, output, suppressed=suppressed,
                          prior=prior)
    print(f"Workbook written: {path}")


if __name__ == "__main__":
    main()
