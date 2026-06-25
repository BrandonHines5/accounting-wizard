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
- **T2-05 — Vendor concentration shift** (`concentration.py` + `baselines.py`):
  compares each vendor's current share of spend within a cost code (over the
  item-coded **cost lines** from Purchases by Item Detail) against a stored
  baseline; flags a sharp jump to dominance. MEDIUM. Period-over-period: it reads
  `ctx.baselines` (loaded from the `baselines` table) and is a no-op until a
  baseline exists. Refresh baselines with
  `skill.run --update-baselines --store supabase`.

Thresholds live in `config/rules.yaml`. The single-window checks (T2-02, T2-10)
compute inline; T2-05 uses the persisted baseline. The remaining checks (T2-01,
T2-03, T2-04, T2-06 … T2-09) need feeds we don't ingest yet — quantities, schedule
activity, AR aging, change orders, PM↔sub assignments — and are declared `pending`
(in `analytics/__init__.py`) so the Methodology sheet shows honest coverage.

Like Tier 1, all checks iterate the entity registry — per-entity baselines, no
hardcoded entity names.
