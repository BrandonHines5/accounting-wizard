# Detection Specification

Every check maps a fraud scheme or error class to: data sources, rule logic,
severity, and tier. **Payroll schemes are explicitly out of scope** (owner-run).

Rule IDs are stable — findings reference them, and dispositions are tracked per rule.

All rules run per entity across **every active entity in the registry**
(`config/entities.yaml`); cross-entity rules (T1-23, T1-24) evaluate every pair of
active entities. Severity escalation for nonprofit entities keys off
`legal_type: nonprofit_501c3` in the registry, never an entity name.

---

## TIER 1 — Deterministic rules (every weekly run)

### Billing & payments

| ID | Check | Logic | Sources | Severity |
|---|---|---|---|---|
| T1-01 | Duplicate payment — exact | Same vendor + amount + invoice no. paid 2+ times | QB Check/Vendor Detail | CRITICAL |
| T1-02 | Duplicate payment — fuzzy | Same vendor, amount within $1 or invoice no. differing by suffix/prefix, within 60 days. Recurring-cadence and payment-processor fees (`merchant_processor_patterns`, at/below the fee ceiling — QuickBooks Payments/Intuit debits a fee every settlement) are excluded | QB | CRITICAL |
| T1-03 | Approval bypass | Payment in QB with no matching approved bill in Adaptive (AP items) | QB + Adaptive | CRITICAL |
| T1-04 | Threshold splitting | 2+ payments to one vendor within 7 days, each below approval threshold, sum above it | QB + Adaptive | HIGH |
| T1-05 | Bill exceeds PO | Bill amount > PO amount (tolerance configurable, default 2%) | Adaptive/BT POs + QB | HIGH |
| T1-06 | Missing PO | Bill on a PO-required cost code with no PO reference | Adaptive/BT + QB | MEDIUM |
| T1-07 | Payment outside AP run | Check/ACH cut outside normal AP batch days | QB Check Detail | MEDIUM |
| T1-08 | Manual check on AP vendor | Handwritten/manual check to a vendor normally paid via Adaptive workflow | QB | HIGH |
| T1-09 | Payment without matching invoice | Per vendor with bills, each payment must reconcile to one bill or a sum of bills (amount-based; QB doesn't link payment→bill). Flags payments matching no invoice/combination that exceed outstanding invoices (unsupported/overpaid/double-paid). Progress/partial payments not flagged. | QB Vendor Transaction Detail | MEDIUM |

### Vendor master hygiene

| ID | Check | Logic | Sources | Severity |
|---|---|---|---|---|
| T1-10 | Fuzzy duplicate vendors | Name similarity (token sort ratio > 85) or shared address/phone/EIN | QB vendor list | HIGH |
| T1-11 | New vendor + large payment | First payment > configurable threshold within 30 days of vendor creation | QB | HIGH |
| T1-12 | Vendor ↔ employee overlap | Vendor address, phone, or bank fingerprint matches an employee | QB + team roster | CRITICAL |
| T1-13 | Shell-company indicators | PO Box only + no SoS registration + sequential invoice numbers + round amounts (composite score) | QB + AR SoS lookup | HIGH |
| T1-14 | Vendor bank detail change | Any change to vendor payment details since last run | Adaptive/QB | CRITICAL until callback-verified |
| T1-15 | SoS registration check | New vendor LLC/corp not found or not in good standing with AR Secretary of State | SoS lookup | MEDIUM |

### Coding & job cost

| ID | Check | Logic | Sources | Severity |
|---|---|---|---|---|
| T1-20 | Vendor/cost-code mismatch | Vendor billed to a cost code outside its historical pattern | QB + baseline | MEDIUM |
| T1-21 | Cost transfer between jobs | Journal entries moving costs job-to-job — list every one | QB GL + Audit Trail | HIGH (always human-reviewed) |
| T1-22 | Cost on closed/late-stage job | Phase-inconsistent cost (e.g., framing lumber billed during trim) or cost on closed job | QB + BT/project-manager schedule | MEDIUM |
| T1-23 | Wrong entity | Cost characteristics (vendor, job, cost code) matching one entity posted to another — evaluated across all registry entities | QB (all entities) | HIGH; nonprofit involvement = HIGH minimum, escalate |
| T1-24 | Inter-company imbalance | A-owes-B ≠ B-owed-by-A at month end, for every pair of active entities | QB all entities | HIGH |

### Credits, refunds, write-offs

| ID | Check | Logic | Sources | Severity |
|---|---|---|---|---|
| T1-30 | Credit memo listing | Every credit memo / write-off above threshold listed with entered_by | QB | MEDIUM (review list) |
| T1-31 | Expected credit tracking | Returned materials, insurance refunds, deposit returns logged → verify bank receipt within window | Manual log + bank | HIGH if missing |

### Expense reimbursement / cards

| ID | Check | Logic | Sources | Severity |
|---|---|---|---|---|
| T1-40 | Duplicate receipt | Same merchant + date + amount across reimbursement and card statement | QB + card exports | HIGH |
| T1-41 | Personal-purchase indicators | Weekend/odd-hour material purchases; supply-house purchases coded to no job | Card/supplier exports | MEDIUM |
| T1-42 | Fuel reasonableness | Fuel card volume vs. plausible mileage for assigned jobs | Card exports + job locations | INFO→MEDIUM |

---

## TIER 2 — Statistical anomalies (requires Supabase baseline)

| ID | Check | Logic | Severity |
|---|---|---|---|
| T2-01 | Price creep | Per-vendor, per-cost-code unit cost trend; flag drift > band vs. peer vendors and own history | MEDIUM→HIGH |
| T2-02 | Benford / round-number analysis | First-digit distribution and round-amount frequency per vendor and per entered_by | INFO |
| T2-03 | Margin trajectory | Job cost-to-budget curve vs. historical curve at same completion %; flag abnormal erosion | HIGH |
| T2-04 | Material quantity reasonableness | Quantities per job vs. sq-ft–based expected ranges (lumber, shingles, concrete, etc.) | MEDIUM |
| T2-05 | Vendor concentration shift | Sudden share-of-spend shift to one vendor within a trade | INFO→MEDIUM |
| T2-06 | Change order patterns | CO frequency/size by PM and by vendor vs. peers; round-number COs without backup docs | HIGH |
| T2-07 | Sub win-rate by PM | One PM's jobs consistently using the same sub at above-median cost | HIGH (kickback indicator) |
| T2-08 | Labor vs. schedule activity | Labor charged on days schedule shows no site activity (uses gap-analysis data) | MEDIUM |
| T2-09 | AR aging anomalies | Receivables aging oddly then clearing in patterns (lapping indicator) | HIGH |
| T2-10 | Payment timing anomalies | Per-vendor payment cadence outliers | INFO |

---

## TIER 3 — AI judgment layer

Runs on every Tier 1/2/4 flag before human review. For each finding, Claude receives:
the transaction(s), vendor history, job context, memo lines, who entered/approved,
related prior findings and dispositions. It outputs:

1. Plain-English assessment (2–4 sentences)
2. Severity confirmation or adjustment (with reason)
3. False-positive probability + the specific innocent explanation if likely
   (void/reissue, bank fee, timing, known vendor quirk)
4. Recommended next step (clear / verify with X / escalate)

Goal: the human disposition session reviews a **short, readable list**, not raw rule
output. Tier 3 may downgrade but never silently delete a CRITICAL finding.

---

## TIER 4 — Independent verification (bank ↔ books ↔ approvals)

Monthly per entity per account.

**Coverage scoping (both directions):** book→bank findings are asserted only
inside the window the statements cover; bank→book findings only inside the
window the BOOKS cover. Bank lines outside books coverage (e.g. a multi-year
statement backfill predating the earliest book export) roll up into ONE INFO
coverage-gap note (T4-01) per entity/side instead of per-line CRITICALs.
Check-number matches are date-constrained (`check_match_max_days`) because
numbers recycle over an account's life. Unmatched lines below
`bank_min_critical_amount` surface as INFO. The deposit side is skipped (with an
INFO ingest-gap note) for an entity whose books contain no receipt transactions.

**Internal cash-management sweeps.** An automatic same-institution movement
between an entity's operating account and a linked sweep sub-account (a Cash
Manager that parks balances above a floor overnight to earn interest and returns
them) clears the bank with no third-party book entry by design. Bank lines whose
description matches a configured `sweep_transfer_patterns` entry (`config/rules.yaml`)
are recognized as internal transfers on both the disbursement (T4-09) and deposit
(T4-07) sides — matched and rolled into ONE INFO note per entity/side instead of a
false CRITICAL. Only NEW money in the sweep account (interest income) is a genuine
unmatched item, and it lands on the sweep account's own statement. Patterns name no
account numbers (CLAUDE.md) — the counterpart is matched by the bank's generic
"Account Ending in NNNN" wording; full pair-level verification requires ingesting
the sweep account's statement so each transfer matches its mirror.

| ID | Check | Logic | Severity |
|---|---|---|---|
| T4-01 | Statement extraction | Parse statement register: date, description, amount, check no. | — (pipeline) |
| T4-02 | Three-way match | Bank txn ↔ QB ↔ Adaptive approval; check no. + amount + date tolerance. Unmatched on either side = finding | CRITICAL (unrecorded disbursement) / HIGH (book-only) |
| T4-03 | Check image payee read | Vision read of payee, amount, date per cancelled check; compare payee vs. QB payee for that check no. | CRITICAL on mismatch |
| T4-04 | Amount alteration | Cleared amount ≠ recorded amount | CRITICAL |
| T4-05 | Endorsement review | Back-image read for checks > threshold or to new vendors; flag individual endorsement on business payee, double endorsement | HIGH |
| T4-06 | Clearing-gap analysis | Recorded-date vs. cleared-date outliers (kiting/holding indicators) | MEDIUM |
| T4-07 | Deposit-side match | Client payments / donations recorded in QB ↔ bank deposits; short or missing deposits | CRITICAL |
| T4-08 | Nonprofit donation reconciliation | Donation acknowledgments/pledges vs. actual deposits — runs for every nonprofit entity in the registry (currently Hope Filled Homes) | CRITICAL |
| T4-09 | Non-check disbursement sweep | Every ACH, wire, debit-card bank line matched to a book entry; recognized internal cash-management sweeps (`sweep_transfer_patterns`) and payment-processor fees (`merchant_*` — QuickBooks Payments/Intuit, reconciled against the processor's gross deposits at the expected rate) are matched, not flagged | CRITICAL if unmatched |

**Image handling:** confidence score per read; < 90% confidence → human review queue.
Images stay in SharePoint (restricted); Supabase stores reads + path reference only.

---

## Standing principles (encode in skill SKILL.md)

1. **Independent source matching** — fraud lives in the gaps between systems nobody
   cross-references. Books vs. bank, vendor vs. SoS, cost vs. physical reality.
2. **Segregation-of-duties monitoring** — map who-creates / who-approves / who-pays
   from QB Audit Trail + Adaptive history; flag concentration. Small family company
   = imperfect segregation = detective controls matter more, not less.
3. **Disposition memory** — a cleared finding never resurfaces. A repeated
   pattern after clearing follows the rule's recurrence policy: fraud-pattern
   rules (T1-01/02/04/12/14, T4-03/04/05) escalate; cadence/operational rules
   (T1-07/08/20/22, T2-10, T4-02/06/09) suppress the recurrence with the
   reviewer's original reason attached (CRITICALs are never auto-suppressed);
   everything else passes through annotated with the prior reason.
4. **Tone** — findings are written as questions to verify, not accusations.
   Errors will outnumber fraud 100:1.
5. **Entity-agnostic by construction** — every rule consumes the entity registry;
   onboarding a new entity requires only a registry entry and its export drops.
