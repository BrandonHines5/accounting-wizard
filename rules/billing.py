"""Billing & payments rules (T1-01 … T1-08).

Implemented here: T1-01, T1-02, T1-04, T1-07 — these run on QB data alone.
T1-03/05/06/08 need Adaptive/Buildertrend ingest and are declared pending.

Amounts are evaluated as absolute values: QB exports disbursements as
negatives, bills as positives.
"""
from __future__ import annotations

import re

import pandas as pd

from core.findings import Finding, Severity
from rules.engine import RunContext, pending_rule, rule

# All money leaving: dup detection also covers bills (a duplicate bill entry
# is a duplicate payment waiting to happen) and card charges.
PAYMENT_TYPES = {"check", "ach", "wire", "bill_payment", "card"}
DUP_TYPES = PAYMENT_TYPES | {"bill"}
# AP-workflow disbursements only (threshold splitting / batch-day rules don't
# apply to card swipes).
AP_TYPES = {"check", "ach", "wire", "bill_payment"}


def _payments(ctx: RunContext, entity_id: str, types: set[str] = PAYMENT_TYPES) -> pd.DataFrame:
    df = ctx.entity_transactions(entity_id)
    df = df[df["txn_type"].isin(types) & df["vendor_name"].notna()]
    return df.assign(amount=df["amount"].abs())


def _norm_invoice(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if pd.notna(value) else ""


@rule("T1-01", "Duplicate payment — exact", requires="QB Vendor Transaction Detail")
def duplicate_payment_exact(ctx: RunContext):
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, DUP_TYPES)
        pay = pay[pay["invoice_no"].notna() & (pay["invoice_no"].astype(str).str.strip() != "")]
        groups = pay.groupby(["vendor_name", "amount", pay["invoice_no"].astype(str)])
        for (vendor, amount, invoice), grp in groups:
            # Same doc number + amount entered as both bill and payment is the
            # normal bill→payment pair, not a duplicate
            if len(grp) < 2 or grp["txn_type"].nunique() == len(grp):
                continue
            dates = ", ".join(grp["date"].dt.date.astype(str))
            yield Finding(
                rule_id="T1-01",
                severity=Severity.CRITICAL,
                entity_ids=[entity_id],
                question=(
                    f"{vendor} document {invoice} appears {len(grp)} times "
                    f"(${amount:,.2f} each, on {dates}). Was one a void/reissue, "
                    "or was this entered/paid twice?"
                ),
                details={"vendor": vendor, "amount": amount, "invoice_no": invoice},
                transactions=list(grp["source_id"].astype(str)),
            )


@rule("T1-02", "Duplicate payment — fuzzy", requires="QB Vendor Transaction Detail")
def duplicate_payment_fuzzy(ctx: RunContext):
    tol = float(ctx.config.param("fuzzy_dup_amount_tolerance"))
    window = int(ctx.config.param("fuzzy_dup_window_days"))
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, DUP_TYPES).sort_values("date")
        seen_pairs: set[tuple[str, str]] = set()
        for vendor, grp in pay.groupby("vendor_name"):
            # Recurring obligations (rent, loan payments, dues): 3+ equal
            # amounts spaced ≥ 15 days apart are a cadence, not duplicates —
            # don't flag undocumented pairs within them.
            recurring_amounts = set()
            for amount, agrp in grp.groupby("amount"):
                gaps = agrp["date"].sort_values().diff().dt.days.dropna()
                if len(agrp) >= 3 and not gaps.empty and gaps.min() >= 15:
                    recurring_amounts.add(amount)
            rows = grp.to_dict("records")
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    if (b["date"] - a["date"]).days > window:
                        break  # rows are date-sorted
                    if abs(a["amount"] - b["amount"]) > tol:
                        continue
                    inv_a, inv_b = _norm_invoice(a["invoice_no"]), _norm_invoice(b["invoice_no"])
                    if not inv_a and not inv_b and a["amount"] in recurring_amounts \
                            and b["amount"] in recurring_amounts:
                        continue
                    same_invoice = bool(inv_a) and inv_a == inv_b
                    if same_invoice and a["amount"] == b["amount"] \
                            and a["txn_type"] == b["txn_type"]:
                        continue  # exact duplicate — T1-01's finding
                    if {a["txn_type"], b["txn_type"]} == {"bill", "bill_payment"} \
                            or {a["txn_type"], b["txn_type"]} == {"bill", "check"}:
                        continue  # a bill and its own payment
                    invoice_variant = (
                        inv_a and inv_b and inv_a != inv_b
                        and (inv_a.startswith(inv_b) or inv_b.startswith(inv_a)
                             or inv_a.endswith(inv_b) or inv_b.endswith(inv_a))
                    )
                    # Flag near-identical amounts where invoices are variants of
                    # each other, identical (amount differs slightly), or absent.
                    if not (invoice_variant or same_invoice or (not inv_a and not inv_b)):
                        continue
                    key = tuple(sorted([str(a["source_id"]), str(b["source_id"])]))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    yield Finding(
                        rule_id="T1-02",
                        severity=Severity.CRITICAL,
                        entity_ids=[entity_id],
                        question=(
                            f"Two payments to {vendor} look like possible duplicates: "
                            f"${a['amount']:,.2f} on {a['date'].date()} "
                            f"(doc {a['invoice_no'] or '—'}) and ${b['amount']:,.2f} on "
                            f"{b['date'].date()} (doc {b['invoice_no'] or '—'}). "
                            "Are these for distinct obligations?"
                        ),
                        details={"vendor": vendor},
                        transactions=[str(a["source_id"]), str(b["source_id"])],
                    )


@rule("T1-04", "Threshold splitting", requires="QB + approval threshold config")
def threshold_splitting(ctx: RunContext):
    window = int(ctx.config.param("threshold_split_window_days"))
    for entity in ctx.registry.active():
        threshold = ctx.config.approval_threshold(entity)
        pay = _payments(ctx, entity.id, AP_TYPES).sort_values("date")
        for vendor, grp in pay.groupby("vendor_name"):
            below = grp[grp["amount"] < threshold]
            if len(below) < 2:
                continue
            rows = below.to_dict("records")
            reported: set[frozenset] = set()
            for i in range(len(rows)):
                cluster = [rows[i]]
                for j in range(i + 1, len(rows)):
                    if (rows[j]["date"] - rows[i]["date"]).days <= window:
                        cluster.append(rows[j])
                total = sum(r["amount"] for r in cluster)
                if len(cluster) >= 2 and total > threshold:
                    ids = frozenset(str(r["source_id"]) for r in cluster)
                    if any(ids <= prev for prev in reported):
                        continue
                    reported.add(ids)
                    yield Finding(
                        rule_id="T1-04",
                        severity=Severity.HIGH,
                        entity_ids=[entity.id],
                        question=(
                            f"{vendor} received {len(cluster)} payments within {window} days, "
                            f"each under the ${threshold:,.0f} approval threshold but totaling "
                            f"${total:,.2f}. Is this one obligation split across payments?"
                        ),
                        details={"vendor": vendor, "total": total, "threshold": threshold},
                        transactions=sorted(ids),
                    )


@rule("T1-07", "Payment outside AP run", requires="QB + confirmed AP batch days",
      notes="Disabled until ap_run_weekdays is set in rules.yaml (Hines data "
            "shows Wednesday-dominant cadence but ~38% off-day volume).")
def payment_outside_ap_run(ctx: RunContext):
    ap_days = set(ctx.config.param("ap_run_weekdays") or [])
    if not ap_days:
        return  # cadence not confirmed — rule off
    for entity_id in ctx.active_entity_ids:
        pay = _payments(ctx, entity_id, AP_TYPES)
        off_cycle = pay[~pay["date"].dt.weekday.isin(ap_days)]
        for _, row in off_cycle.iterrows():
            yield Finding(
                rule_id="T1-07",
                severity=Severity.MEDIUM,
                entity_ids=[entity_id],
                question=(
                    f"Payment of ${row['amount']:,.2f} to {row['vendor_name']} was cut on "
                    f"{row['date'].strftime('%A %Y-%m-%d')}, outside the normal AP batch days. "
                    "What prompted the off-cycle payment?"
                ),
                details={"vendor": row["vendor_name"], "check_no": row["check_no"]},
                transactions=[str(row["source_id"])],
            )


pending_rule("T1-03", "Approval bypass", requires="QB + Adaptive bills/approvals export",
             notes="Needs Adaptive ingest (ingest/adaptive.py).")
pending_rule("T1-05", "Bill exceeds PO", requires="Adaptive/BT POs + QB",
             notes="Needs PO ingest.")
pending_rule("T1-06", "Missing PO", requires="Adaptive/BT + QB + PO-required cost-code list")
pending_rule("T1-08", "Manual check on AP vendor", requires="QB + Adaptive vendor-workflow map")
