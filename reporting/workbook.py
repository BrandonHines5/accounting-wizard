"""Exceptions workbook generator.

Multi-sheet, severity-ranked Excel output in the buildertrend-gap-analysis
style: Summary → one sheet per severity → Methodology (every rule ID with its
implementation status, so coverage is honest).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from core.entities import EntityRegistry
from core.findings import Finding, Severity
from rules.engine import all_rules

SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.INFO]


def findings_frame(findings: list[Finding]) -> pd.DataFrame:
    if not findings:
        return pd.DataFrame(columns=["rule_id", "severity", "entities", "question",
                                     "ai_assessment", "recommended_action",
                                     "false_positive", "transactions", "disposition"])
    return pd.DataFrame([f.to_row() for f in findings])


def rule_precision_frame(prior: pd.DataFrame | None) -> pd.DataFrame:
    """Per-rule disposition history: how many findings each rule produced and how
    the human calls came out. 'Real-issue rate' = (error_corrected +
    cleanup_needed + escalated) / all dispositioned — the number that says which
    thresholds in rules.yaml to tune next (clean-up needed counts as real: the
    rule surfaced something worth fixing, even if benign). Empty frame when
    history is missing or malformed; rules with only open findings still get a
    row (blank rate) so coverage stays visible."""
    if (prior is None or len(prior) == 0
            or "rule_id" not in prior.columns or "disposition" not in prior.columns):
        return pd.DataFrame()
    counts = (prior.assign(disposition=prior["disposition"].astype(str))
              .groupby(["rule_id", "disposition"]).size().unstack(fill_value=0))
    for col in ("open", "legit", "error_corrected", "cleanup_needed", "escalated"):
        if col not in counts.columns:
            counts[col] = 0
    dispositioned = (counts["legit"] + counts["error_corrected"]
                     + counts["cleanup_needed"] + counts["escalated"])
    real = counts["error_corrected"] + counts["cleanup_needed"] + counts["escalated"]
    out = pd.DataFrame({
        "Rule ID": counts.index,
        "Open": counts["open"].values,
        "Cleared as legit": counts["legit"].values,
        "Error corrected": counts["error_corrected"].values,
        "Clean-up needed": counts["cleanup_needed"].values,
        "Escalated": counts["escalated"].values,
        "Real-issue rate": [
            f"{r / d:.0%}" if d else ""
            for r, d in zip(real.values, dispositioned.values, strict=True)],
    }).sort_values("Rule ID").reset_index(drop=True)
    return out


def write_workbook(
    findings: list[Finding],
    registry: EntityRegistry,
    output_path: Path | str,
    run_label: str | None = None,
    suppressed: list[Finding] | None = None,
    auto_resolved: list[Finding] | None = None,
    prior: pd.DataFrame | None = None,
) -> Path:
    suppressed = suppressed or []
    auto_resolved = auto_resolved or []
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_label = run_label or datetime.now().strftime("%Y-%m-%d %H:%M")

    by_severity = {sev: [f for f in findings if f.severity == sev] for sev in SEVERITY_ORDER}

    summary_rows = []
    for entity in registry.active():
        row = {"Entity": entity.name, "Type": entity.legal_type}
        for sev in SEVERITY_ORDER:
            row[str(sev)] = sum(1 for f in by_severity[sev] if entity.id in f.entity_ids)
        summary_rows.append(row)
    totals = {"Entity": "TOTAL", "Type": "",
              **{str(sev): len(by_severity[sev]) for sev in SEVERITY_ORDER}}
    summary = pd.DataFrame([*summary_rows, totals])

    methodology = pd.DataFrame([
        {
            "Rule ID": spec.rule_id,
            "Check": spec.title,
            "Status": "Implemented" if spec.implemented else "Pending data source",
            "Requires": spec.requires,
            "Notes": spec.notes,
        }
        for spec in all_rules()
    ])

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        for sev in SEVERITY_ORDER:
            sev_findings = by_severity[sev]
            frame = findings_frame(sev_findings)
            frame.to_excel(writer, sheet_name=str(sev), index=False)
        methodology.to_excel(writer, sheet_name="Methodology", index=False)
        # Only add the disposition-memory sheet when there's something to show, so
        # the default workbook shape (and its test) is unchanged.
        if suppressed:
            findings_frame(suppressed).to_excel(
                writer, sheet_name="Dispositioned", index=False)
        # Bank-verified auto-resolutions (T1-01/T1-02 low-dollar duplicates Tier 4
        # confirmed as recurring payments). Listed here, resolved — never silently
        # dropped. Only added when there's something to show, so the default
        # workbook shape (and its test) is unchanged.
        if auto_resolved:
            findings_frame(auto_resolved).to_excel(
                writer, sheet_name="Auto-resolved (verified)", index=False)
        # Per-rule precision from history: the tuning feedback loop. Only added
        # when there IS history, so the default workbook shape is unchanged.
        precision = rule_precision_frame(prior)
        if len(precision):
            precision.to_excel(writer, sheet_name="Rule Precision", index=False)
        tier3_reviewed = sum(1 for f in findings if f.ai_assessment)
        pd.DataFrame([
            {"Run": run_label,
             "Entities": ", ".join(e.name for e in registry.active()),
             "Total findings": len(findings),
             "Tier 3 reviewed": f"{tier3_reviewed} of {len(findings)}",
             "Suppressed (disposition memory)": len(suppressed),
             "Auto-resolved (bank-verified)": len(auto_resolved),
             "Reminder": "Findings are verification questions, not accusations. "
                         "Disposition each one: legit / error_corrected / "
                         "cleanup_needed / escalated."}
        ]).to_excel(writer, sheet_name="Run Info", index=False)

    return output_path
