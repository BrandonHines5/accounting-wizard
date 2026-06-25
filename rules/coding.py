"""Coding & job-cost rules (T1-20 … T1-24).

Implemented: T1-20 (vendor/cost-code mismatch), T1-21 (journal cost transfers),
T1-23 (wrong entity, heuristic), T1-24 (inter-company imbalance across every pair
of registry entities). T1-22 needs schedule data — declared pending.

These are the rules the entity registry exists for: they iterate every active
entity and never reference an entity by name.
"""
from __future__ import annotations

import re

import pandas as pd

from core.findings import Finding, Severity
from rules.engine import RunContext, pending_rule, rule


@rule("T1-21", "Cost transfer between jobs", requires="QB GL journal entries")
def cost_transfer_between_jobs(ctx: RunContext):
    for entity_id in ctx.active_entity_ids:
        journals = ctx.entity_transactions(entity_id)
        journals = journals[journals["txn_type"] == "journal"]
        # A journal source_id touching 2+ jobs is a job-to-job cost transfer.
        for source_id, grp in journals.groupby("source_id"):
            jobs = sorted(set(grp["job_id"].dropna().astype(str))) if not grp.empty else []
            if len(jobs) < 2:
                continue
            total = grp["amount"].abs().max()
            yield Finding(
                rule_id="T1-21",
                severity=Severity.HIGH,
                entity_ids=[entity_id],
                question=(
                    f"Journal entry {source_id} moves ~${total:,.2f} between jobs "
                    f"{' and '.join(jobs)} (entered by {grp['entered_by'].iloc[0] or 'unknown'}). "
                    "What is the business reason for this cost transfer?"
                ),
                details={"jobs": ", ".join(jobs), "entered_by": grp["entered_by"].iloc[0]},
                transactions=[str(source_id)],
            )


@rule("T1-23", "Wrong entity", requires="QB all entities")
def wrong_entity(ctx: RunContext):
    established_min = int(ctx.config.param("wrong_entity_established_min"))
    stray_max = int(ctx.config.param("wrong_entity_stray_max"))
    txns = ctx.transactions[
        ctx.transactions["entity_id"].isin(ctx.active_entity_ids)
        & ctx.transactions["vendor_name"].notna()
    ]
    counts = txns.groupby(["vendor_name", "entity_id"]).size().unstack(fill_value=0)
    for vendor, row in counts.iterrows():
        home_entities = [e for e, n in row.items() if n >= established_min]
        stray_entities = [e for e, n in row.items() if 0 < n <= stray_max]
        if not home_entities or not stray_entities:
            continue
        for stray in stray_entities:
            stray_txns = txns[(txns["vendor_name"] == vendor) & (txns["entity_id"] == stray)]
            home = ctx.registry.get(home_entities[0])
            yield Finding(
                rule_id="T1-23",
                severity=Severity.HIGH,
                entity_ids=[stray, *home_entities],
                question=(
                    f"{vendor} normally bills {home.name} "
                    f"({int(row[home_entities[0]])} transactions) but has "
                    f"{int(row[stray])} posting(s) on {ctx.registry.get(stray).name}. "
                    "Do these costs belong to the entity they were posted to?"
                ),
                details={"vendor": vendor,
                         "home_entity": home_entities[0], "stray_entity": stray},
                transactions=list(stray_txns["source_id"].astype(str)),
            )


@rule("T1-24", "Inter-company imbalance", requires="QB all entities (Due to/from + Loan to/from accounts)")
def intercompany_imbalance(ctx: RunContext):
    tolerance = float(ctx.config.param("intercompany_tolerance"))
    pattern = re.compile(ctx.config.intercompany_account_pattern)
    # Direction semantics: "Due from X" / "Loan to X" are assets (X owes me);
    # "Due to X" / "Loan from X" are liabilities (I owe X).
    balances: dict[tuple[str, str, str], float] = {}
    txns = ctx.transactions[ctx.transactions["account"].notna()]
    for _, row in txns.iterrows():
        m = pattern.search(str(row["account"]).strip())
        if not m:
            continue
        kind, direction, counterparty_text = (m.group(1).lower(), m.group(2).lower(),
                                              m.group(3))
        counterparty = ctx.registry.resolve_name(counterparty_text)
        if counterparty is None or counterparty.id == row["entity_id"]:
            continue
        me = row["entity_id"]
        owes_me = (kind, direction) in {("due", "from"), ("loan", "to")}
        debtor, creditor = (counterparty.id, me) if owes_me else (me, counterparty.id)
        key = (debtor, creditor, me)  # third element = whose books
        balances[key] = balances.get(key, 0.0) + float(row["amount"])

    entities_with_books = set(ctx.transactions["entity_id"].unique())
    active = ctx.active_entity_ids
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            for debtor, creditor in [(a, b), (b, a)]:
                # abs(): export sign conventions differ by report; the two
                # sides are compared on net magnitude
                per_debtor_books = abs(balances.get((debtor, creditor, debtor), 0.0))
                per_creditor_books = abs(balances.get((debtor, creditor, creditor), 0.0))
                if per_debtor_books == 0.0 and per_creditor_books == 0.0:
                    continue
                debtor_name = ctx.registry.get(debtor).name
                creditor_name = ctx.registry.get(creditor).name
                missing = {debtor, creditor} - entities_with_books
                if missing:
                    # Only one side's books are in this run — can't reconcile,
                    # but record the open balance so it isn't invisible.
                    missing_name = ctx.registry.get(next(iter(missing))).name
                    balance = max(per_debtor_books, per_creditor_books)
                    yield Finding(
                        rule_id="T1-24",
                        severity=Severity.INFO,
                        entity_ids=[debtor, creditor],
                        question=(
                            f"Inter-company activity of ${balance:,.2f} recorded between "
                            f"{debtor_name} (debtor) and {creditor_name} (creditor), but "
                            f"{missing_name}'s books were not part of this run. Reconcile "
                            f"both directions once {missing_name}'s exports are ingested."
                        ),
                        details={"debtor": debtor, "creditor": creditor,
                                 "books_missing_for": ", ".join(sorted(missing))},
                    )
                    continue
                diff = per_debtor_books - per_creditor_books
                if abs(diff) <= tolerance:
                    continue
                yield Finding(
                    rule_id="T1-24",
                    severity=Severity.HIGH,
                    entity_ids=[debtor, creditor],
                    question=(
                        f"Inter-company balance disagrees: {debtor_name}'s books show it owes "
                        f"{creditor_name} ${per_debtor_books:,.2f}, but {creditor_name}'s books "
                        f"show ${per_creditor_books:,.2f} receivable (difference "
                        f"${abs(diff):,.2f}). Which side is missing an entry?"
                    ),
                    details={"debtor": debtor, "creditor": creditor,
                             "debtor_books": per_debtor_books,
                             "creditor_books": per_creditor_books},
                )


@rule("T1-20", "Vendor/cost-code mismatch",
      requires="QB Purchases by Item Detail (vendor + cost_code item lines)")
def vendor_costcode_mismatch(ctx: RunContext):
    """A vendor billed to a cost code outside its established pattern — the vendor
    has a home code (used >= established_min times) and a rare stray on another.
    Reads item-coded cost lines; a clean no-op until those are ingested."""
    established_min = int(ctx.config.param("vendor_costcode_established_min"))
    stray_max = int(ctx.config.param("vendor_costcode_stray_max"))
    for entity_id in ctx.active_entity_ids:
        df = ctx.entity_cost_lines(entity_id)
        df = df[df["vendor_name"].notna() & df["cost_code"].notna()]
        for vendor, grp in df.groupby("vendor_name"):
            counts = grp["cost_code"].astype(str).value_counts()
            home_codes = counts[counts >= established_min].index.tolist()
            stray_codes = counts[counts <= stray_max].index.tolist()
            if not home_codes or not stray_codes:
                continue
            home = home_codes[0]
            for stray in stray_codes:
                stray_txns = grp[grp["cost_code"].astype(str) == stray]
                yield Finding(
                    rule_id="T1-20",
                    severity=Severity.MEDIUM,
                    entity_ids=[entity_id],
                    question=(
                        f"{vendor} normally bills cost code {home} "
                        f"({int(counts[home])} times) but has {int(counts[stray])} "
                        f"posting(s) on {stray}. Is the {stray} coding correct?"
                    ),
                    details={"vendor": vendor, "home_cost_code": home, "stray_cost_code": stray},
                    transactions=list(stray_txns["source_id"].astype(str)),
                )


pending_rule("T1-22", "Cost on closed/late-stage job",
             requires="QB + BT/project-manager schedule export")
