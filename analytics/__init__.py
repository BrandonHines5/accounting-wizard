# Importing the rule modules registers their Tier 2 rules with the shared engine,
# so run_all executes them alongside Tier 1 over the canonical dataset.
from analytics import benford, concentration, payment_timing  # noqa: F401

from rules.engine import pending_rule

# Spec'd Tier 2 rules whose data sources we don't ingest yet (quantities,
# schedule activity, AR aging, change orders, PM↔sub assignments). Declared so the
# Methodology sheet shows honest Tier 2 coverage, exactly like the Tier 1 pendings.
pending_rule("T2-01", "Price creep", requires="Unit cost = amount ÷ quantity; quantity feed not ingested")
pending_rule("T2-03", "Margin trajectory", requires="Job cost-to-budget curves + completion %")
pending_rule("T2-04", "Material quantity reasonableness", requires="Quantities + sq-ft per job")
pending_rule("T2-06", "Change order patterns", requires="Change orders by PM/vendor + backup docs")
pending_rule("T2-07", "Sub win-rate by PM", requires="PM↔sub assignment + bid data")
pending_rule("T2-08", "Labor vs schedule activity", requires="Schedule activity (gap-analysis) feed")
pending_rule("T2-09", "AR aging anomalies", requires="AR aging snapshots over time")
