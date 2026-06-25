# Tier 2 — statistical anomalies (T2-01 … T2-10)

First slice built. Tier 2 rules register with the shared `rules.engine`, so they
run in the weekly battery alongside Tier 1 (`run_all`) and appear on the workbook
Methodology sheet:

- **T2-02 — Benford / round-number** (`benford.py`): round-number concentration
  per `entered_by` (too many exact multiples of a round dollar amount), plus a
  chi-square first-digit (Benford) test per entity. INFO.
- **T2-10 — Payment timing** (`payment_timing.py`): per-vendor cadence outliers
  via a robust (MAD) modified z-score on the vendor's own inter-payment gaps.
  INFO.

Thresholds live in `config/rules.yaml`; each rule computes its baseline inline
over the run window. The remaining checks (T2-01, T2-03 … T2-09) need feeds we
don't ingest yet — quantities, schedule activity, AR aging, change orders, PM↔sub
assignments — and are declared `pending` (in `analytics/__init__.py`) so the
Methodology sheet shows honest coverage. Persisted period-over-period baselines
(the `baselines` table) are a later slice.

Like Tier 1, all checks iterate the entity registry — per-entity baselines, no
hardcoded entity names.
