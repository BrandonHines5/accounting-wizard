-- financial_forensics schema (Phase 2 baseline) — DRAFT, apply when Phase 2 starts.
-- Entity coverage is registry-driven: the entities table mirrors
-- config/entities.yaml and every fact table references it. No raw bank account
-- numbers anywhere — hashed fingerprints only. Images stay in SharePoint;
-- only path references are stored here.

create schema if not exists financial_forensics;

create table financial_forensics.entities (
  id          text primary key,            -- registry slug, e.g. 'hines-homes'
  name        text not null,
  legal_type  text not null check (legal_type in ('llc','corp','sole_prop','nonprofit_501c3')),
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

create table financial_forensics.vendors (
  id               bigint generated always as identity primary key,
  entity_id        text not null references financial_forensics.entities(id),
  vendor_id        text not null,
  vendor_name      text not null,
  address          text,
  phone            text,
  ein              text,
  bank_fingerprint text,                   -- SHA-256 of account details, never raw
  sos_status       text,                   -- AR Secretary of State standing
  first_seen       date,
  unique (entity_id, vendor_id)
);

create table financial_forensics.transactions (
  id            bigint generated always as identity primary key,
  entity_id     text not null references financial_forensics.entities(id),
  source_system text not null,             -- qb | adaptive | buildertrend | bank | card
  source_id     text not null,
  txn_type      text not null,
  date          date not null,
  vendor_id     text,
  vendor_name   text,
  job_id        text,
  cost_code     text,
  account       text,
  amount        numeric(14,2) not null,
  check_no      text,
  invoice_no    text,
  memo          text,
  entered_by    text,
  ingested_at   timestamptz not null default now(),
  unique (entity_id, source_system, source_id)
);

create table financial_forensics.bank_transactions (
  id           bigint generated always as identity primary key,
  entity_id    text not null references financial_forensics.entities(id),
  account_fingerprint text not null,       -- hashed, never the account number
  date         date not null,
  description  text,
  amount       numeric(14,2) not null,
  check_no     text,
  payee_read   text,                       -- vision read of cancelled check
  amount_read  numeric(14,2),
  read_confidence numeric(5,2),            -- < 90 → human review queue
  image_ref    text,                       -- SharePoint path, never the image
  matched_transaction_id bigint references financial_forensics.transactions(id)
);

create table financial_forensics.findings (
  id             bigint generated always as identity primary key,
  rule_id        text not null,            -- stable DETECTION_SPEC id, e.g. 'T1-01'
  severity       text not null check (severity in ('CRITICAL','HIGH','MEDIUM','INFO')),
  entity_ids     text[] not null,
  question       text not null,            -- verification question, not accusation
  details        jsonb not null default '{}',
  transaction_refs text[] not null default '{}',
  ai_assessment  text,                     -- Tier 3 plain-English review
  disposition    text not null default 'open'
                 check (disposition in ('open','legit','error_corrected','escalated')),
  dispositioned_by text,
  dispositioned_at timestamptz,
  fingerprint    text not null,            -- dedupe key: dispositioned findings never resurface
  created_at     timestamptz not null default now(),
  unique (fingerprint)
);

create table financial_forensics.intercompany_ledger (
  id            bigint generated always as identity primary key,
  debtor_id     text not null references financial_forensics.entities(id),
  creditor_id   text not null references financial_forensics.entities(id),
  as_of         date not null,
  debtor_books  numeric(14,2) not null,    -- balance per debtor's books
  creditor_books numeric(14,2) not null,   -- balance per creditor's books
  reconciled    boolean not null default false,
  check (debtor_id <> creditor_id)
);

create table financial_forensics.baselines (
  id          bigint generated always as identity primary key,
  entity_id   text not null references financial_forensics.entities(id),
  kind        text not null,               -- unit_cost | payment_timing | vendor_cost_code
  key         text not null,               -- e.g. vendor|cost_code
  stats       jsonb not null,
  computed_at timestamptz not null default now(),
  unique (entity_id, kind, key)
);

create index on financial_forensics.transactions (entity_id, date);
create index on financial_forensics.transactions (entity_id, vendor_name);
create index on financial_forensics.findings (disposition, severity);
create index on financial_forensics.bank_transactions (entity_id, date);
