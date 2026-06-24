"""Build the per-finding context packet the judgment layer reviews.

A `JudgmentPacket` bundles everything Claude needs to assess one finding without
re-querying the dataset: the finding itself, the involved transactions, vendor
history within and across entities, and any prior dispositions for the same
rule/entity (disposition memory — a cleared finding should not be re-raised the
same way). Packet assembly is pure pandas/Python and fully testable; the model
call lives in `tier3.judge`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.findings import Finding
from rules.engine import RunContext

# Detail keys that carry a vendor name across the rule modules.
_VENDOR_DETAIL_KEYS = ("vendor", "vendor_a", "vendor_b")


def _jsonable(value):
    """Coerce pandas/NumPy scalars to plain JSON-serializable Python values."""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if hasattr(value, "item"):          # numpy scalar
        return value.item()
    return value


def _row_summary(row: pd.Series) -> dict:
    keep = ["source_id", "txn_type", "date", "vendor_name", "amount", "account",
            "job_id", "cost_code", "check_no", "invoice_no", "memo", "entered_by"]
    return {k: _jsonable(row.get(k)) for k in keep}


@dataclass
class JudgmentPacket:
    finding: Finding
    entity: dict                                    # id, name, legal_type, is_nonprofit
    transactions: list[dict] = field(default_factory=list)
    vendor_history: list[dict] = field(default_factory=list)
    prior_findings: list[dict] = field(default_factory=list)

    def to_prompt_dict(self) -> dict:
        """The JSON object handed to the model — finding + assembled context."""
        f = self.finding
        return {
            "rule_id": f.rule_id,
            "current_severity": str(f.severity),
            "verification_question": f.question,
            "rule_details": {k: _jsonable(v) for k, v in f.details.items()},
            "entity": self.entity,
            "transactions": self.transactions,
            "vendor_history": self.vendor_history,
            "prior_dispositions": self.prior_findings,
        }


def _vendor_names(finding: Finding, txns: pd.DataFrame) -> list[str]:
    names: list[str] = []
    for key in _VENDOR_DETAIL_KEYS:
        val = finding.details.get(key)
        if isinstance(val, str) and val:
            names.append(val)
    if "vendor_name" in txns.columns:
        names.extend(str(v) for v in txns["vendor_name"].dropna().unique())
    # de-dup, preserve order
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _vendor_history(ctx: RunContext, entity_id: str, vendor_name: str) -> dict:
    all_txns = ctx.transactions
    here = all_txns[(all_txns["entity_id"] == entity_id)
                    & (all_txns["vendor_name"] == vendor_name)]
    other = all_txns[(all_txns["entity_id"] != entity_id)
                     & (all_txns["vendor_name"] == vendor_name)]
    cost_codes = sorted(str(c) for c in here["cost_code"].dropna().unique())
    hist = {
        "vendor_name": vendor_name,
        "txn_count_in_entity": int(len(here)),
        "total_amount_in_entity": float(here["amount"].abs().sum()),
        "cost_codes_seen": cost_codes,
        "also_billed_entities": sorted(str(e) for e in other["entity_id"].unique()),
    }
    vendors = ctx.vendors
    match = vendors[(vendors["entity_id"] == entity_id)
                    & (vendors["vendor_name"] == vendor_name)]
    if not match.empty:
        hist["first_seen"] = _jsonable(match.iloc[0].get("first_seen"))
    return hist


def _prior_for(finding: Finding, prior: pd.DataFrame | None) -> list[dict]:
    """Prior dispositioned findings for the same rule touching the same entities."""
    if prior is None or prior.empty:
        return []
    same_rule = prior[prior["rule_id"] == finding.rule_id]
    out = []
    for _, row in same_rule.iterrows():
        row_entities = row.get("entity_ids")
        if isinstance(row_entities, str):
            row_entities = [e.strip() for e in row_entities.split(",")]
        elif not isinstance(row_entities, (list, tuple, set)):
            row_entities = []   # NaN / None / scalar — no entities recorded
        if set(row_entities) & set(finding.entity_ids):
            out.append({
                "disposition": row.get("disposition"),
                "question": row.get("question"),
                "dispositioned_at": _jsonable(row.get("dispositioned_at")),
            })
    return out


def build_packets(
    findings: list[Finding],
    ctx: RunContext,
    prior_findings: pd.DataFrame | None = None,
) -> list[JudgmentPacket]:
    """Assemble one packet per finding. `prior_findings` is the Supabase
    `findings` history (Phase 2); None until then — disposition memory is simply
    empty, never an error."""
    txns = ctx.transactions
    source_col = txns["source_id"].astype(str)
    packets: list[JudgmentPacket] = []
    for finding in findings:
        primary_id = finding.entity_ids[0] if finding.entity_ids else None
        entity_obj = ctx.registry.get(primary_id) if primary_id else None
        entity = {
            "id": primary_id,
            "name": entity_obj.name if entity_obj else None,
            "legal_type": entity_obj.legal_type if entity_obj else None,
            "is_nonprofit": bool(entity_obj.is_nonprofit) if entity_obj else False,
            "other_entities_involved": finding.entity_ids[1:],
        }

        ids = [str(t) for t in finding.transactions]
        involved = txns[source_col.isin(ids) & txns["entity_id"].isin(finding.entity_ids)]
        transactions = [_row_summary(row) for _, row in involved.iterrows()]

        history = [
            _vendor_history(ctx, primary_id, name)
            for name in _vendor_names(finding, involved)
        ] if primary_id else []

        packets.append(JudgmentPacket(
            finding=finding,
            entity=entity,
            transactions=transactions,
            vendor_history=history,
            prior_findings=_prior_for(finding, prior_findings),
        ))
    return packets
