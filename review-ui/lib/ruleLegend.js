// Human-readable legend for finding "type" codes (rule IDs like T1-01).
//
// The authoritative definitions live in DETECTION_SPEC.md at the repo root; rule
// IDs are stable and never renumbered (see CLAUDE.md), so keying this static map
// by them is safe. When a rule is added or its meaning changes in DETECTION_SPEC,
// mirror it here. Descriptions are intentionally one-liners — the spec has detail.

export const RULE_GROUPS = [
  {
    tier: "Tier 1 · Billing & payments",
    rules: [
      { id: "T1-01", label: "Duplicate payment — exact", desc: "Same vendor + amount + invoice no. paid two or more times." },
      { id: "T1-02", label: "Duplicate payment — fuzzy", desc: "Near-identical vendor/amount or invoice no. within a short window." },
      { id: "T1-03", label: "Approval bypass", desc: "Payment with no matching approved bill in the AP workflow." },
      { id: "T1-04", label: "Threshold splitting", desc: "Several sub-threshold payments to one vendor summing above it." },
      { id: "T1-05", label: "Bill exceeds PO", desc: "Bill amount over the purchase order (default 2% tolerance)." },
      { id: "T1-06", label: "Missing PO", desc: "Bill on a PO-required cost code with no PO reference." },
      { id: "T1-07", label: "Payment outside AP run", desc: "Check/ACH cut outside the normal AP batch days." },
      { id: "T1-08", label: "Manual check on AP vendor", desc: "Handwritten/manual check to a vendor normally paid via workflow." },
    ],
  },
  {
    tier: "Tier 1 · Vendor master hygiene",
    rules: [
      { id: "T1-10", label: "Fuzzy duplicate vendors", desc: "Similar name, or shared address / phone / EIN." },
      { id: "T1-11", label: "New vendor + large payment", desc: "Large first payment soon after the vendor was created." },
      { id: "T1-12", label: "Vendor ↔ employee overlap", desc: "Vendor address, phone, or bank matches an employee." },
      { id: "T1-13", label: "Shell-company indicators", desc: "PO box only, no SoS reg, sequential invoices, round amounts." },
      { id: "T1-14", label: "Vendor bank-detail change", desc: "Any change to vendor payment details — callback-verify." },
      { id: "T1-15", label: "SoS registration check", desc: "New vendor not found / not in good standing with the AR SoS." },
    ],
  },
  {
    tier: "Tier 1 · Coding & job cost",
    rules: [
      { id: "T1-20", label: "Vendor / cost-code mismatch", desc: "Vendor billed to a cost code outside its historical pattern." },
      { id: "T1-21", label: "Cost transfer between jobs", desc: "Journal entries moving costs from one job to another." },
      { id: "T1-22", label: "Cost on closed / late job", desc: "Phase-inconsistent cost, or a cost posted to a closed job." },
      { id: "T1-23", label: "Wrong entity", desc: "Cost characteristics matching a different registry entity." },
      { id: "T1-24", label: "Inter-company imbalance", desc: "A-owes-B ≠ B-owed-by-A at month end, per entity pair." },
    ],
  },
  {
    tier: "Tier 1 · Credits, refunds, write-offs",
    rules: [
      { id: "T1-30", label: "Credit memo listing", desc: "Credit memos / write-offs above threshold, with who entered them." },
      { id: "T1-31", label: "Expected credit tracking", desc: "Logged refund/return not received in the bank within the window." },
    ],
  },
  {
    tier: "Tier 1 · Expense reimbursement / cards",
    rules: [
      { id: "T1-40", label: "Duplicate receipt", desc: "Same merchant + date + amount on a reimbursement and a card." },
      { id: "T1-41", label: "Personal-purchase indicators", desc: "Odd-hour/weekend or no-job-coded material purchases." },
      { id: "T1-42", label: "Fuel reasonableness", desc: "Fuel volume vs. plausible mileage for the assigned jobs." },
    ],
  },
  {
    tier: "Tier 2 · Statistical anomalies",
    rules: [
      { id: "T2-01", label: "Price creep", desc: "Unit-cost drift vs. peer vendors and the vendor's own history." },
      { id: "T2-02", label: "Benford / round-number", desc: "First-digit and round-amount patterns by vendor and enterer." },
      { id: "T2-03", label: "Margin trajectory", desc: "Abnormal cost-to-budget erosion vs. the historical curve." },
      { id: "T2-04", label: "Material quantity reasonableness", desc: "Quantities per job vs. sq-ft-based expected ranges." },
      { id: "T2-05", label: "Vendor concentration shift", desc: "Sudden swing of spend share to one vendor within a trade." },
      { id: "T2-06", label: "Change-order patterns", desc: "CO frequency/size outliers; round-number COs without backup." },
      { id: "T2-07", label: "Sub win-rate by PM", desc: "One PM repeatedly using one sub at above-median cost." },
      { id: "T2-08", label: "Labor vs. schedule activity", desc: "Labor charged on days the schedule shows no site activity." },
      { id: "T2-09", label: "AR aging anomalies", desc: "Receivables aging then clearing in lapping-like patterns." },
      { id: "T2-10", label: "Payment-timing anomalies", desc: "Per-vendor payment cadence outliers." },
    ],
  },
  {
    tier: "Tier 4 · Independent verification (bank ↔ books ↔ approvals)",
    rules: [
      { id: "T4-01", label: "Statement extraction", desc: "Parse the bank statement register (pipeline step)." },
      { id: "T4-02", label: "Three-way match", desc: "Bank ↔ books ↔ approval; unmatched on either side." },
      { id: "T4-03", label: "Check image payee read", desc: "Vision payee vs. the booked payee for that check no." },
      { id: "T4-04", label: "Amount alteration", desc: "Cleared amount ≠ the recorded amount." },
      { id: "T4-05", label: "Endorsement review", desc: "Back-image issues: individual or double endorsement." },
      { id: "T4-06", label: "Clearing-gap analysis", desc: "Recorded vs. cleared date outliers (kiting / holding)." },
      { id: "T4-07", label: "Deposit-side match", desc: "Client payments / donations vs. bank deposits; short or missing." },
      { id: "T4-08", label: "Nonprofit donation reconciliation", desc: "Donation acknowledgments / pledges vs. actual deposits." },
      { id: "T4-09", label: "Non-check disbursement sweep", desc: "Every ACH, wire, or debit matched to a book entry." },
    ],
  },
];

// Flat id → { id, label, desc } map for quick lookups (e.g. card tooltips).
export const RULE_INFO = Object.fromEntries(
  RULE_GROUPS.flatMap((g) => g.rules.map((r) => [r.id, r])),
);
