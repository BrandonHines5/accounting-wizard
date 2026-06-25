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
    """Slim one transaction row down to the fields the reviewer needs."""
    keep = ["source_id", "txn_type", "date", "vendor_name", "amount", "account",
            "job_id", "cost_code", "check_no", "invoice_no", "memo", "entered_by"]
    return {k: _jsonable(row.get(k)) for k in keep}


@dataclass
class JudgmentPacket:
    """The context bundle the judgment layer reviews for a single finding."""

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
    """Vendor names involved in a finding (from rule details + the txn rows)."""
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


@dataclass
class _VendorIndex:
    """Vendor lookups precomputed once per run.

    `build_packets` may touch a vendor's history for many findings; without this
    each lookup would rescan the full transaction frame (O(findings × vendors × N)).
    Grouping once up front turns each lookup into a dict access."""

    by_entity_vendor: dict          # (entity_id, vendor_name) -> txn DataFrame
    entities_by_vendor: dict        # vendor_name -> sorted entity ids that billed it
    first_seen: dict                # (entity_id, vendor_name) -> creation date

    @classmethod
    def build(cls, ctx: RunContext) -> "_VendorIndex":
        named = ctx.transactions[ctx.transactions["vendor_name"].notna()]
        by_ev = {key: grp for key, grp in named.groupby(["entity_id", "vendor_name"])}
        ent_by_vendor = (named.groupby("vendor_name")["entity_id"]
                         .agg(lambda s: sorted(set(map(str, s)))).to_dict())
        first_seen = {}
        for _, row in ctx.vendors.iterrows():
            first_seen.setdefault((row["entity_id"], row["vendor_name"]),
                                  row.get("first_seen"))
        return cls(by_ev, ent_by_vendor, first_seen)

    def history(self, entity_id: str, vendor_name: str) -> dict:
        """Per-vendor history within `entity_id`, plus which other entities use it."""
        here = self.by_entity_vendor.get((entity_id, vendor_name))
        cost_codes = ([] if here is None
                      else sorted(str(c) for c in here["cost_code"].dropna().unique()))
        hist = {
            "vendor_name": vendor_name,
            "txn_count_in_entity": 0 if here is None else len(here),
            "total_amount_in_entity": 0.0 if here is None else float(here["amount"].abs().sum()),
            "cost_codes_seen": cost_codes,
            "also_billed_entities": [e for e in self.entities_by_vendor.get(vendor_name, [])
                                     if e != str(entity_id)],
        }
        if (entity_id, vendor_name) in self.first_seen:
            hist["first_seen"] = _jsonable(self.first_seen[(entity_id, vendor_name)])
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
                "reason": row.get("disposition_note"),  # the human's stated "why"
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
    vendor_index = _VendorIndex.build(ctx)
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
            vendor_index.history(primary_id, name)
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
