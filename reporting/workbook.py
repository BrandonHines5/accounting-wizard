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
                                     "transactions", "disposition"])
    return pd.DataFrame([f.to_row() for f in findings])


def write_workbook(
    findings: list[Finding],
    registry: EntityRegistry,
    output_path: Path | str,
    run_label: str | None = None,
) -> Path:
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
        pd.DataFrame([
            {"Run": run_label,
             "Entities": ", ".join(e.name for e in registry.active()),
             "Total findings": len(findings),
             "Reminder": "Findings are verification questions, not accusations. "
                         "Disposition each one: legit / error_corrected / escalated."}
        ]).to_excel(writer, sheet_name="Run Info", index=False)

    return output_path
