"""
Manufacturing POC 3a — Schema Validation (vs Per-Dataset Contract)
====================================================================
Answers : Does the CURRENT upload of THIS file match the schema we
          locked in for THIS file the first time it was profiled?

CHANGE: the contract used to be one hardcoded dict shared by every
CSV you uploaded, so a different file would show every column as
"missing" and every one of its own columns as "extra". Now app.py
builds and locks a contract per dataset_id (from that dataset's
own first schema profile) and passes it in here. This module no
longer hardcodes any column names.
"""

SYSTEM = """You are a data pipeline engineer validating a manufacturing
CSV schema against a contract. Return valid JSON only. No markdown."""

PROMPT = """
Validate the actual schema from the latest profile against the schema
contract for this dataset.

Rules:
- Every column in the contract must exist in the actual schema (missing = CRITICAL)
- Every column in actual schema must exist in the contract (extra = MEDIUM)
- For each column present in both, compare actual type vs contract type (mismatch = HIGH)
- Severity: critical = missing column | high = type mismatch | medium = extra column | none = perfect match
- can_pipeline_proceed = false if severity is critical or high

SCHEMA CONTRACT (column -> expected type, locked from this dataset's first profile):
{contract}

LATEST SCHEMA PROFILE:
{poc1_schema}

Return EXACT JSON:
{{
  "validation_passed": true,
  "severity": "critical|high|medium|none",
  "can_pipeline_proceed": true,
  "summary": "one sentence summary",
  "missing_columns": ["col1"],
  "extra_columns": ["col1"],
  "type_mismatches": [
    {{"column": "col", "expected_type": "integer", "actual_type": "string"}}
  ],
  "null_issues": [
    {{"column": "col", "null_count": 0, "recommendation": "action"}}
  ],
  "recommended_actions": ["action 1"]
}}
"""