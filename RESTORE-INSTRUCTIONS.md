# RESTORE-INSTRUCTIONS.md — rebuilding `accounting-wizard`

This file ships inside every backup folder produced by
`.github/workflows/database-backup.yml`. It is written for an **AI coding agent**
(or an engineer) who has only the backup folder and needs to stand the project
back up from nothing.

## What this project is

`accounting-wizard` is a **registry-driven forensic-accounting agent** for the
Hines Homes group of entities. It is a **Python** application (pandas + openpyxl,
no web frontend) that ingests financial exports, runs a tiered detection battery,
and writes severity-ranked exceptions workbooks. Findings are persisted to a
**Supabase Postgres** database under the `financial_forensics` schema.

- **Language / runtime:** Python 3 (see `requirements.txt`: pandas, openpyxl, PyYAML, pytest).
- **Entry point:** the Claude Code skill in `skill/` (`python -m skill.run`).
- **Database:** Supabase Postgres, schema `financial_forensics`
  (tables: `entities`, `vendors`, `transactions`, `bank_transactions`, `findings`,
  `intercompany_ledger`, `baselines` — see `supabase/migrations/`).
- **Storage:** Supabase Storage is used only for **check / bank-statement image
  references** — the repo policy is that the **images themselves stay in SharePoint**
  and only path references + hashed fingerprints live in Postgres. Any Storage
  bucket files captured under `storage/` in this backup should be restored as-is,
  but expect this to be small or empty depending on configuration.
- **Hosting:** none for this repo. It runs as a scheduled/manual CLI + Claude Code
  skill, not a deployed web service. (The customer-facing review UI lives in a
  **separate** CRM repo — Next.js on Vercel — and is out of scope for this restore.)
- **Edge functions:** none in this repo (`supabase/functions/` does not exist). If a
  future backup contains a `supabase/functions/` directory in the code zip, deploy
  those per the optional step below; otherwise skip edge-function steps.

> **Agent: never invent secrets, project refs, URLs, keys, or account names.**
> Wherever a value is needed below, **ask the user** for it (Supabase project ref,
> database password, S3 keys, SharePoint paths, etc.). Do not guess.

## What's in this backup folder

| File | Contents |
|---|---|
| `backup_<timestamp>.sql.gz` | gzipped `pg_dump` (plain SQL) of the Supabase Postgres database — schema **and** data, `--no-owner --no-privileges`. |
| `code_<timestamp>.zip` | `git archive` of the repository source at the backed-up commit (HEAD). |
| `storage/` | Mirror of the Supabase Storage bucket files (present only if S3 creds were configured at backup time; may be empty). |
| `RESTORE-INSTRUCTIONS.md` | This file. |

Pick the **newest matching timestamps** for the `.sql.gz` and `.zip` when restoring.

## Prerequisites (ask the user to provide / authorize)

- A Supabase account + organization, and authorization to create a project.
- Postgres client tools (`psql`, matching the dump's major version — default **17**).
- `rclone` configured with the same `sharepoint` remote used for backups (only
  needed if you must pull files back from SharePoint, or push storage back).
- Python 3.11+ and `pip`.

## Rebuild steps (in order)

### 1. Unpack the source code

```bash
unzip code_<timestamp>.zip -d accounting-wizard
cd accounting-wizard
```

### 2. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest        # sanity check against synthetic fixtures (no DB needed)
```

### 3. Create a new Supabase project

Ask the user to create a new Supabase project (or provide an existing target),
then collect from them:
- the **project ref**,
- the **database connection URI** (Settings > Database > Connection string),
- the **region**.

Do not proceed with invented values.

### 4. Restore the database (schema + data)

The dump produced by this workflow is a **full** plain-SQL dump (schema and data
together), so you normally restore it in one shot rather than re-running migrations:

```bash
gunzip -c backup_<timestamp>.sql.gz | psql "<NEW_DB_CONNECTION_URI>"
```

If you instead want a clean schema-only rebuild (e.g. starting fresh and importing
data selectively), apply the committed migrations from the code zip first:

```bash
# from the unzipped repo, using the Supabase CLI or psql:
psql "<NEW_DB_CONNECTION_URI>" -f supabase/migrations/0001_financial_forensics_schema.sql
# ...then load data as needed.
```

After restore, verify the schema exists:

```bash
psql "<NEW_DB_CONNECTION_URI>" -c "\dt financial_forensics.*"
```

You should see `entities`, `vendors`, `transactions`, `bank_transactions`,
`findings`, `intercompany_ledger`, `baselines`.

### 5. Restore Storage bucket files (only if `storage/` is non-empty)

This project keeps check/statement **images in SharePoint**, so the Supabase
Storage footprint is intentionally minimal. If `storage/` contains files:

1. Recreate the bucket(s) in the new Supabase project (same names/visibility as the
   originals — ask the user if unknown; do not assume bucket names).
2. Copy the files back to the new project's S3 endpoint with rclone, mirroring the
   approach in `database-backup.yml`:

```bash
export RCLONE_CONFIG_SUPASTORAGE_TYPE=s3
export RCLONE_CONFIG_SUPASTORAGE_PROVIDER=Other
export RCLONE_CONFIG_SUPASTORAGE_ENV_AUTH=false
export RCLONE_CONFIG_SUPASTORAGE_ACCESS_KEY_ID="<NEW_S3_ACCESS_KEY_ID>"
export RCLONE_CONFIG_SUPASTORAGE_SECRET_ACCESS_KEY="<NEW_S3_SECRET_ACCESS_KEY>"
export RCLONE_CONFIG_SUPASTORAGE_ENDPOINT="https://<new-ref>.storage.supabase.co/storage/v1/s3"
export RCLONE_CONFIG_SUPASTORAGE_REGION="<region, e.g. us-east-2>"

rclone copy ./storage supastorage: --fast-list --transfers 8 --checkers 8
```

(Get the new project's S3 access key, secret, endpoint, and region from the user —
Supabase > Settings > Storage.)

### 6. Edge functions (only if present in the code zip)

This repo has **no** `supabase/functions/` directory today. If a future backup's
code zip contains one, deploy each function and set its secrets:

```bash
supabase functions deploy <name> --project-ref <new-ref>
supabase secrets set KEY=VALUE --project-ref <new-ref>   # ask the user for the values
```

Otherwise skip this step.

### 7. Set environment / configuration

This project reads its **entity registry and rule thresholds from committed YAML**
(`config/entities.yaml`, `config/rules.yaml`) rather than environment variables, so
there is little runtime config to recreate. For Phase 2+ Supabase persistence, wire
up the database connection the same way the original deployment did — ask the user
how the connection string was provided (e.g. a `.env` / CI secret named
`SUPABASE_DB_URL`) and set it accordingly. **Do not invent variable names or
values** beyond what you find referenced in the unzipped source.

### 8. Deploy / run

There is no hosted service to deploy. Restore the **operating cadence** instead:

- Re-create the GitHub Actions backup workflow secrets/variables (see the header of
  `.github/workflows/database-backup.yml`) so backups resume.
- Run the agent as before:

```bash
python -m skill.run                       # all active entities
python -m skill.run --entity hines-homes  # pilot scope
```

### 9. Verify

- `pytest` passes against synthetic fixtures.
- `\dt financial_forensics.*` shows all expected tables with row counts matching the
  pre-restore database (`select count(*) from financial_forensics.findings;` etc.).
- A weekly run produces an exceptions workbook in `output/` without errors.
- The backup workflow's next scheduled run uploads to the correct SharePoint folder.

## Notes

- **Never commit real financial data.** `data/` and `output/` are gitignored and are
  not part of the source zip — that is by design. Real exports live locally or in
  SharePoint, not in Git or in these backups' code zip.
- Bank account numbers exist only as hashed fingerprints; do not attempt to
  reconstruct raw account numbers from any restored data.
- If anything above requires a credential, project ref, bucket name, or account you
  do not have, **stop and ask the user** rather than guessing.
