"""
Manufacturing POC 1 — Schema Discovery & Column Profiling
==========================================================
Answers : What does this CSV look like? Is it valid?

This module only defines the prompt used by app.py's /api/poc1/run
route. app.py resolves the actual CSV path per dataset_id and calls
ask_json(PROMPT.format(raw_csv=...), SYSTEM) directly — there is no
hardcoded filename here, so a differently-named CSV uploaded at any
point works the same way as the first one.
"""

SYSTEM = """
You are a senior data engineer specialising in manufacturing data pipelines.
Your job is to perform a thorough schema audit on a raw CSV file so that
downstream ETL pipelines can be safely built on top of it.

Rules you must follow:
- Count rows by counting non-header lines in the CSV.
- Infer the data type of each column by inspecting actual values, not just
  the header name. Use: string, integer, float, date, boolean, categorical,
  percentage.
- For categorical columns, list the distinct values you observe.
- Count nulls by counting blank cells, "NULL", "null", "N/A", or "NaN"
  in each column.
- Never guess — only report what you can directly read from the CSV.
- Return valid JSON only. No markdown, no prose outside the JSON.
"""

PROMPT = """
You are performing a full schema discovery audit on CSV metadata.

IMPORTANT: The metadata includes BOTH:
1. Polars-inferred data types (may not be 100% accurate)
2. Actual sample rows (raw values from the CSV)

Your job is to:
- Review the Polars inferred types
- Look at the actual sample values
- If they match → accept the inferred type
- If they DON'T match → correct the type based on the actual samples

For example:
  Polars says: "age" is Int64
  But samples show: [25, 32, "N/A", 45]
  You should correct: "age" is String (not Int64), because "N/A" is not a number

Work through these steps:

STEP 1 — VERIFY INFERRED TYPES
  For each column, check if Polars' inferred dtype matches the actual sample values.
  Correct any mismatches based on what you see in the samples.

STEP 2 — UNDERSTAND THE DATA
  Review column names, corrected data types, null counts, ranges, and uniqueness.

STEP 3 — PER-COLUMN ANALYSIS
  For every column, produce:
  a) business_meaning   — based on column name and sample values
  b) data_type          — CORRECTED dtype (if Polars was wrong, fix it!)
  c) nullable           — true if null_count > 0
  d) null_count         — from metadata
  e) validation_rule    — based on dtype and sample values
  f) sample_values      — from the samples provided
  g) categorical_values — if unique_count < 20, list distinct values from samples

STEP 4 — KEY COLUMNS
  Identify 3-5 most critical columns.

STEP 5 — QUALITY CONCERNS
  Based on the metadata and samples, flag any data quality issues.

STEP 6 — RECOMMENDED INDEXES
  List columns that should be indexed.

CSV METADATA (column structure + sample rows):
{csv_metadata}

Return this EXACT JSON structure — no markdown fences, no extra keys:
{{
  "table_name": "manufacturing_quality_data",
  "total_columns": 0,
  "total_rows": 0,
  "schema": [
    {{
      "column": "exact column name",
      "business_meaning": "what this represents",
      "data_type": "corrected dtype based on actual samples (not just Polars inference!)",
      "nullable": true,
      "null_count": 0,
      "validation_rule": "rule to enforce",
      "sample_values": ["val1", "val2", "val3"],
      "categorical_values": ["val1", "val2"]
    }}
  ],
  "key_columns": ["col1", "col2"],
  "quality_concerns": ["Specific concern with explanation"],
  "recommended_indexes": ["col1", "col2"]
}}
"""