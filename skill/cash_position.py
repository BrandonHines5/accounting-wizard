"""One-shot cash-position pull: live bank balances + monthly P&L per QBO company.

Answers the treasury question the detection battery can't ("which companies hold
more checking cash than they need?"): for every company connected to QuickBooks
Online it pulls, straight from the Accounting API,

  * every Bank-type account's name (digit runs redacted — no account numbers in
    logs or artifacts), subtype, and CurrentBalance, and
  * a monthly Profit & Loss summary (Income / COGS / Expenses / Other / Net
    Income) over the trailing complete months,

then prints one JSON document between BEGIN/END markers (so an Actions log is
machine-readable) and writes the same data to output/ CSVs for the artifact.

It reuses the weekly run's QBO wiring end to end — SupabaseRefreshTokenStore
(so Intuit's ~daily refresh-token rotation is persisted and connections are
discovered from `qbo_connections`, exactly like `--pull-qbo`), QboAuth, and
QboClient — and adds no new auth surface. Read-only: Account query + P&L report.

Run from CI (needs QBO_CLIENT_ID/SECRET + SUPABASE_URL/SERVICE_KEY, plus the
QBO_REFRESH_TOKEN_* seeds for any company not yet rotated into Supabase):

    python -m skill.cash_position [--months 6]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from core.entities import EntityRegistry
from core.redaction import redact_digits
from ingest.qbo import (QBO_ENV, QboAuth, QboClient, api_base, load_qbo_config,
                        resolve_token_endpoint)
from persistence.qbo_token_store import SupabaseRefreshTokenStore

# P&L summary groups worth carrying: enough to see whether income covers spend.
_PNL_GROUPS = ("Income", "COGS", "GrossProfit", "Expenses",
               "OtherIncome", "OtherExpenses", "NetIncome")


def trailing_months(n: int, today: date | None = None) -> list[tuple[str, str, str]]:
    """The n most recent COMPLETE months as (label, start, end) ISO tuples."""
    today = today or date.today()
    year, month = today.year, today.month
    out: list[tuple[str, str, str]] = []
    for _ in range(n):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        start = date(year, month, 1)
        end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1))
        out.append((f"{year}-{month:02d}", start.isoformat(),
                    (end - date.resolution).isoformat()))
    return list(reversed(out))


def pnl_summary(report: dict) -> dict:
    """Group → total from a P&L report's summary rows (recursive: sections nest)."""
    out: dict = {}

    def walk(rows) -> None:
        for row in rows or []:
            group = row.get("group")
            if group in _PNL_GROUPS:
                cols = (row.get("Summary") or {}).get("ColData") or []
                if cols:
                    try:
                        out[group] = float(cols[-1].get("value") or 0)
                    except (TypeError, ValueError):
                        pass
            walk((row.get("Rows") or {}).get("Row"))

    walk((report.get("Rows") or {}).get("Row"))
    return out


def bank_accounts(client: QboClient, entity_id: str, realm_id: str) -> list[dict]:
    """Every Bank-type account with its live balance. Names are digit-redacted so
    an account number embedded in a QBO account name never reaches logs/artifacts
    (CLAUDE.md: no raw account numbers anywhere)."""
    payload = client.query(entity_id, realm_id,
                           "select * from Account where AccountType = 'Bank'")
    accounts = (payload.get("QueryResponse") or {}).get("Account") or []
    return [{
        "name": redact_digits(str(a.get("Name") or "")),
        "subtype": a.get("AccountSubType") or "",
        "balance": float(a.get("CurrentBalance") or 0),
        "active": bool(a.get("Active", True)),
    } for a in accounts]


def pull_cash_position(months: int, config_path=None, registry=None) -> dict:
    cfg = load_qbo_config(config_path) if config_path else load_qbo_config()
    if cfg is None:
        raise SystemExit("config/qbo.yaml missing (copy config/qbo.example.yaml)")
    missing = [v for v in QBO_ENV if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    registry = registry or EntityRegistry.load()
    active = {e.id for e in registry.active()}

    env_by_entity = {eid: (ent or {}).get("refresh_token_env")
                     for eid, ent in (cfg.get("entities") or {}).items()}
    token_store = SupabaseRefreshTokenStore.from_env(None, env_by_entity)
    realms = token_store.list_connections()
    # UI-authorized connections are the source of truth; config entries with an
    # explicit realm_id join them (path B), mirroring _maybe_pull_qbo.
    for eid, ent in (cfg.get("entities") or {}).items():
        realm = (ent or {}).get("realm_id")
        if realm:
            realms.setdefault(eid, str(realm))

    auth = QboAuth(os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"],
                   token_store,
                   token_endpoint=resolve_token_endpoint(cfg.get("environment") or "production"))
    client = QboClient(auth, base_url=api_base(cfg))
    windows = trailing_months(months)

    companies: dict = {}
    for entity_id, realm_id in sorted(realms.items()):
        if entity_id not in active:
            print(f"  - {entity_id}: not an active registry entity — skipped")
            continue
        try:
            accounts = bank_accounts(client, entity_id, realm_id)
            monthly = {}
            for label, start, end in windows:
                monthly[label] = pnl_summary(
                    client.report(entity_id, realm_id, "ProfitAndLoss",
                                  start=start, end=end))
            companies[entity_id] = {"bank_accounts": accounts, "pnl_by_month": monthly}
            total = sum(a["balance"] for a in accounts)
            print(f"  ✓ {entity_id}: {len(accounts)} bank account(s), "
                  f"${total:,.2f} total, {len(monthly)} P&L month(s)")
        except Exception as exc:  # noqa: BLE001 — one company must not abort the batch
            print(f"  ! {entity_id}: FAILED ({type(exc).__name__}: {exc}) — skipped")
            companies[entity_id] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"as_of": date.today().isoformat(),
            "months": [w[0] for w in windows], "companies": companies}


def write_outputs(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cash_position.json").write_text(json.dumps(result, indent=1))
    bal_rows = ["entity,account,subtype,active,balance"]
    pnl_rows = ["entity,month," + ",".join(_PNL_GROUPS)]
    for eid, data in result["companies"].items():
        for a in data.get("bank_accounts", []):
            name = a["name"].replace(",", " ")
            bal_rows.append(f"{eid},{name},{a['subtype']},{a['active']},{a['balance']}")
        for month, groups in (data.get("pnl_by_month") or {}).items():
            pnl_rows.append(f"{eid},{month}," + ",".join(
                str(groups.get(g, "")) for g in _PNL_GROUPS))
    (out_dir / "cash_position_balances.csv").write_text("\n".join(bal_rows))
    (out_dir / "cash_position_pnl.csv").write_text("\n".join(pnl_rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--months", type=int, default=6,
                        help="trailing complete months of P&L to pull (default 6)")
    parser.add_argument("--output", default="output",
                        help="directory for the JSON/CSV artifacts (default output/)")
    args = parser.parse_args()

    print(f"Pulling cash position ({args.months} trailing months) …")
    result = pull_cash_position(args.months)
    write_outputs(result, Path(args.output))
    print("=== CASH POSITION BEGIN ===")
    print(json.dumps(result, indent=1))
    print("=== CASH POSITION END ===")


if __name__ == "__main__":
    main()
