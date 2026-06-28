"""
Manufacturing POC 2 — Data Quality Checks + Batch Analysis
============================================================
This module only defines the prompts used by app.py's /api/poc2/run
route. app.py resolves the actual CSV and schema-profile paths per
dataset_id and calls ask_json() directly — no hardcoded filenames
here, so a differently-named CSV uploaded at any point is handled
the same way as the first one.

Approach: Step 1 — ask AI to design a list of data quality checks
          based on the schema profile (column names/types change
          per CSV, so the checks must be generated dynamically,
          not hardcoded).
          Step 2 — run those checks + batch quality analysis against
          the raw CSV in a second call.
"""

SYSTEM_DESIGN = """You are a manufacturing data quality engineer.
Given a schema profile, design a list of meaningful data quality
checks tailored to those exact columns. Return valid JSON only."""

PROMPT_DESIGN = """
Design 5-8 data quality checks for this manufacturing schema.
Base every check on the ACTUAL columns in the schema profile below —
do not invent column names that aren't listed.

Consider check types such as:
- Duplicate key combinations (an ID column + a sequence/step column)
- Required fields that must never be null
- Date/string format consistency
- Numeric columns that must be non-negative
- Referential/balance checks (e.g. component parts summing to a total,
  if such columns exist in this schema)

SCHEMA PROFILE:
{schema_profile}

Return EXACT JSON:
{{
  "checks": [
    {{
      "check_id": "DQ01",
      "check_name": "short name",
      "description": "what this check verifies, in plain English, referencing actual column names"
    }}
  ]
}}
"""

SYSTEM_RUN = """You are a manufacturing data quality engineer.
Run the given data quality checks against the raw CSV, then perform
batch quality analysis. Return valid JSON only. No markdown, no prose."""

PROMPT_RUN = """
Run these data quality checks against the CSV, then perform batch
quality analysis.

DATA QUALITY CHECKS TO RUN:
{checks_list}

BATCH QUALITY ANALYSIS (after DQ checks):
- Group rows by the batch/ID column you identified in the schema
- Per batch: sum the input/output quantity columns relevant to this data
- scrap_rate_pct = scrapped_qty / input_qty * 100 (use the equivalent
  columns from this schema)
- quality_score = 100 - (scrap_rate * 3) - (rework_rate * 1.5), clamp 0-100
- Decision: PASS < 5%, REVIEW 5-10%, REJECT > 10%
- Flag steps: P0 Critical > 15% scrap, P1 Attention 8-15%, P2 Observe 3-8%

RAW CSV:
{raw_csv}

Return EXACT JSON:
{{
  "data_quality_audit": {{
    "audit_passed": true,
    "total_violations": 0,
    "checks": [
      {{
        "check_id": "DQ01",
        "check_name": "name",
        "passed": true,
        "violation_count": 0,
        "examples": []
      }}
    ]
  }},
  "total_batches_analysed": 0,
  "pass_count": 0,
  "review_count": 0,
  "reject_count": 0,
  "batches": [
    {{
      "batch_id": "B001",
      "overall_quality_score": 0,
      "total_units_input": 0,
      "total_units_scrapped": 0,
      "total_defects": 0,
      "overall_scrap_rate_pct": 0.0,
      "decision": "PASS|REVIEW|REJECT",
      "summary": "one line summary",
      "critical_steps": [
        {{
          "step": "step name",
          "issue": "what is wrong",
          "scrap_rate": "x.x%",
          "severity": "P0 Critical|P1 Attention|P2 Observe"
        }}
      ],
      "worst_process_step": "step name",
      "recommendations": ["rec 1", "rec 2"]
    }}
  ]
}}
"""