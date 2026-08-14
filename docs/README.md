# Manufacturing Data Quality POC Suite
## AI-Powered Pipeline · AWS Bedrock · DuckDB · SODA Core

6 POCs demonstrating AI-powered data quality on manufacturing CSV data.
01 Signal → 02 Tune → 03 Compose → 04 Soundcheck → 05 Resonance → 06 Pitch Drift → 07 Retune

---

## Package Contents

```
poc_package/
├── setup.sh                     ← Run this FIRST (one-time setup)
├── app.py                       ← Flask web UI backend
├── run_manufacturing.py         ← CLI runner for all POCs
├── bedrock_client.py            ← AWS Bedrock helper
├── duckdb_helper.py             ← DuckDB ingest + sampling (no Polars)
├── data_loader.py               ← Polars CSV reader (CLI POCs only)
├── ingest_to_duckdb.py          ← Standalone DuckDB ingestion script
├── requirements.txt             ← All Python dependencies
│
├── engines/                     ← All POC logic
│   ├── mfg_poc3a_drift.py       ← Schema Validation (hardcoded contract)
│   ├── mfg_poc3b_drift.py       ← Schema Change Discovery (snapshots)
│   ├── mfg_poc7_soda_yaml.py    ← SODA YAML Generator
│
├── static/                      ← Web UI
│   ├── index.html
│   ├── style.css
│   └── app.js
│
├── outputs/                     ← Auto-created, timestamped POC outputs
├── uploads/                     ← Auto-created, uploaded CSV versions
├── snapshots/                   ← Auto-created, schema snapshots for drift
└── contracts/                   ← Auto-created, locked schema contracts
```

---

## Prerequisites

| Requirement              | How to get it                        |
|--------------------------|--------------------------------------|
| AWS Account              | https://aws.amazon.com/free/         |
| Python 3.9+              | https://www.python.org/downloads/    |
| AWS CLI v2               | https://aws.amazon.com/cli/          |
| IAM User + Bedrock access| See Step 1 below                     |
| DBeaver (optional)       | https://dbeaver.io — to inspect DuckDB |

---

## Step-by-Step Setup

### Step 1 — Enable Bedrock Model Access (AWS Console, 2 minutes)

1. AWS Console → search **Bedrock** → open **Amazon Bedrock**
2. Left menu → **Model access** → **Manage model access**
3. Find **Anthropic** → check **Claude Haiku 4.5**
4. Fill in the one-time use-case form → **Save changes**
5. Approved within 60 seconds

### Step 2 — Create IAM User (if you don't have one)

1. AWS Console → **IAM** → **Users** → **Create user**
2. User name: `bedrock-poc-user`
3. Attach policy: **AmazonBedrockFullAccess**
4. **Security credentials** → **Create access key** → **Local code**
5. Download the CSV — you need Access Key ID and Secret Access Key

### Step 3 — Add AWS Credentials to `.env`

Create or update `.env` in the project root or `poc_package/`:

```env
AWS_ACCESS_KEY_ID=your_access_key_id
AWS_SECRET_ACCESS_KEY=your_secret_access_key
AWS_SESSION_TOKEN=your_session_token_optional
AWS_REGION=us-east-1
```

### Step 4 — Run Setup Script

```bash
cd poc_package
bash setup.sh
```

This will:
- Create a Python virtual environment
- Install all packages from `requirements.txt`
  (boto3, flask, duckdb, polars, langfuse, soda-core-duckdb, pyyaml, etc.)
- Verify AWS credentials from `.env`
- Test a live Bedrock API call

### Step 5 — Run the Web UI

```bash
source venv/bin/activate
python app.py
# Open: http://localhost:5000
```

### Step 6 — Run CLI POCs (optional, no UI)

```bash
source venv/bin/activate

# Run all POCs
python run_manufacturing.py

# Run specific POCs
python run_manufacturing.py --poc 1
python run_manufacturing.py --poc 1 3
```

---

## Web UI Flow (Tab by Tab)

| Tab | POC | What it does | Data source |
|-----|-----|--------------|-------------|
| 1 Signal | — | Upload CSV → auto-ingest into DuckDB | `f.save()` stream, no RAM |
| 2 Tune | AI profiles every column (types, nulls, samples) | DuckDB SUMMARIZE + 500-row SAMPLE |
| 3 SODA YAML | POC 7 | AI generates SODA Core checks from schema profile | POC 1 JSON only |
| 4 Data Quality | POC 2 | AI designs + runs DQ checks, scores batches | DuckDB 1000-row SAMPLE |
| 5 Schema Validation | POC 3a | Compares schema against locked contract | POC 1 JSON only |
| 6 Schema Changes | POC 3b | Compares current vs previous upload snapshot | POC 1 snapshots only |

---

## Performance — How Large Files Are Handled

| Step | Old approach (slow) | New approach (fast) |
|------|--------------------|--------------------|
| Upload | `f.read()` → 2GB in Python RAM | `f.save()` → streams disk-to-disk |
| DuckDB ingest | `sample_size=-1` scans all rows | `sample_size=200000` fast type detection |
| Tune | DuckDB SUMMARIZE on full dataset, 500-row SAMPLE sent to AI |
| Data Quality | Polars reads full file | DuckDB 1000-row reservoir SAMPLE sent to AI |
| SODA / Validation / Changes | — | JSON only, zero data reads |

DuckDB memory limit is set to `2GB` — files larger than that spill to `/tmp` and never crash the laptop.

---

## Versioning & Snapshots

Every POC run saves two files under `outputs/<dataset_id>/`:
- `poc1_20260619_143022.json` — permanent timestamped version
- `poc1_latest.json` — always points to most recent run

Schema contracts are locked on first POC 1 run per dataset:
- `contracts/<dataset_id>_contract.json` — never overwritten
- POC 3a validates every future run against this locked contract

Schema snapshots enable drift detection:
- Each POC 1 run saves a snapshot
- POC 3b compares current vs second-to-last snapshot
- First-ever upload shows "no baseline yet" instead of false positives

---

## Common Errors & Fixes

| Error | Fix |
|-------|-----|
| `AccessDeniedException` | Enable model access in Bedrock console (Step 1) |
| `NoCredentialsError` | Add `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` to `.env` |
| `IO Error: Could not set lock on DuckDB file` | Close DBeaver — DuckDB allows only one connection |
| `JSONDecodeError` | Claude returned non-JSON — usually throttle; retry |
| `ThrottlingException` | Wait 5s between calls — already handled by `bedrock_client.py` |
| `cannot import name 'X' from 'duckdb_helper'` | Check `duckdb_helper.py` is the latest version |
| `soda scan: No valid checks found` | Regenerate YAML in Tab 3 — old YAML may use unsupported syntax |

---

## Architecture

```
CSV Upload (any size)
        │
        ▼  f.save() — stream to disk, no RAM
   uploads/<dataset_id>/<timestamp>.csv
        │
        ▼  DuckDB read_csv_auto (sample_size=10000)
   data_resonance.duckdb  →  tbl_<dataset_id>
        │
        ├──► DuckDB SUMMARIZE  ──► POC 1 (full-dataset stats)
        │
        ├──► DuckDB USING SAMPLE 500  ──► POC 1 AI prompt
        │
        ├──► DuckDB USING SAMPLE 1000 ──► POC 2 AI prompt
        │
        ├──► POC 1 JSON  ──► POC 7 SODA YAML
        │
        ├──► POC 1 JSON  ──► POC 3a Schema Validation
        │
        └──► POC 1 snapshots  ──► POC 3b Schema Change Detection
                │
                ▼
        AWS Bedrock (Claude Haiku)
                │
                ▼
        outputs/<dataset_id>/
        ├── poc1_<timestamp>.json
        ├── poc1_latest.json
        ├── soda_latest.yaml
        └── ...
```

---

## Next Steps After POC

1. **Connect real data** — point `CSV_FILE` to S3 presigned URLs or RDS exports
2. **Add Bedrock Guardrails** — PII redaction before data enters prompts
3. **Wrap in AWS Lambda** — trigger on S3 PutObject events automatically
4. **Add Step Functions** — orchestrate multi-step DQ workflows
5. **Scale DuckDB** — swap for MotherDuck (DuckDB cloud) for multi-user access
6. **Automate SODA scans** — schedule `soda scan` via cron after each upload
