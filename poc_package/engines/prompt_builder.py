"""
prompt_builder.py — Dynamic Prompt Generation from Metadata
============================================================
Builds SYSTEM and USER prompts for Schema Discovery (Tab 2 / POC 1).

Metadata is sent to the AI as compact JSON. Low-cardinality columns
now include value_counts (e.g. {"Yes": 1200, "No": 340}) instead of
just a bare list of distinct values — same single-pass DuckDB query,
more useful signal for the AI (it can now see class imbalance,
dominant categories, rare values, etc., not just which values exist).
"""

import json


def build_schema_discovery_prompt(metadata: dict) -> tuple:
    filename      = metadata.get("filename", "unknown.csv")
    file_size_mb  = metadata.get("file_size_mb", 0)
    file_size_kb  = metadata.get("file_size_kb", 0)
    total_rows    = metadata.get("total_rows", 0)
    total_columns = metadata.get("total_columns", 0)
    schema        = metadata.get("schema", {})
    columns       = metadata.get("columns", [])
    sample_rows   = metadata.get("sample_rows", [])

    size_str = f"{file_size_mb} MB" if file_size_mb > 0 else f"{file_size_kb} KB"

    system_prompt = f"""You are an expert data engineer and schema analyst.

Your task is to analyze a CSV file and produce a comprehensive schema profile.

FILE INFORMATION:
- Filename: {filename}
- File Size: {size_str}
- Total Rows: {total_rows:,}
- Total Columns: {total_columns}

You will receive metadata as JSON containing:
1. schema — verified column data types
2. column_statistics — nulls, distinct counts, ranges, and for low-cardinality
   columns, value_counts (each distinct value with its row count, e.g.
   {{"Yes": 1200, "No": 340}})
3. sample_rows — actual data from the file

Your job is to:
1. Understand what each column represents based on name and data
2. Verify the inferred types against sample values
3. If types don't match samples, correct them
4. Infer business meaning for each column
5. Identify key columns critical for data quality
6. Identify CRITICAL DATA POINTS (columns with data quality issues, security concerns, or business impact)
7. Flag quality concerns (high nulls, duplicates, invalid formats, class imbalance using value_counts)
8. Recommend database indexes for performance

Return ONLY valid JSON with no markdown or extra text.
"""

    metadata_payload = {
        "schema": schema,
        "column_statistics": [
            {
                "name":            col.get("name"),
                "type":            col.get("dtype"),
                "null_count":      col.get("null_count", 0),
                "null_pct":        col.get("null_pct", 0),
                "distinct_count":  col.get("unique_count", 0),
                **({"min": col["min"], "max": col["max"]}
                   if col.get("min") and col.get("max") else {}),
                # value_counts replaces the old bare distinct_values list —
                # e.g. {"PL": 800, "HL": 450, "BL": 90} for a loan_type column
                **({"value_counts": col["value_counts"]}
                   if col.get("value_counts") else {}),
            }
            for col in columns
        ],
        "sample_rows": sample_rows,
    }

    instructions_section = """
ANALYSIS STEPS:

STEP 1 — VERIFY TYPES
  Review the schema and sample_rows in the JSON below.
  If samples show different types than schema, correct it.
  Example: schema says Int64, but samples show ["25", "N/A"] → correct to Utf8/String

STEP 2 — UNDERSTAND BUSINESS MEANING
  Based on column name and sample values, what does this column represent?
  Example: "date_of_birth" + samples ["1990-05-15", "2000-01-20"] → "Customer birth date"

STEP 3 — DATA QUALITY ASSESSMENT
  Look at null counts, distinct counts, ranges in column_statistics.
  For low-cardinality columns, review value_counts to understand the
  full distribution of categories — not just which values exist, but
  how common each one is. Flag class imbalance (e.g. one value covering
  >95% of rows), unexpectedly rare values that might be data entry
  errors, or values outside the expected set.
  Example: loan_type value_counts = {"PL": 800, "HL": 450, "BL": 90, "XYZ": 2}
  → "XYZ" appearing only twice is likely a data entry error, flag it.

STEP 4 — IDENTIFY KEY COLUMNS
  Which 3-5 columns are most critical for quality analysis?
  Usually: IDs, dates, amounts, status fields

STEP 5 — IDENTIFY CRITICAL DATA POINTS
  Which columns have:
  - Data quality issues (high nulls, duplicates, inconsistent formats, class imbalance)?
  - Security concerns (sensitive data, PII)?
  - Business impact (revenue, transaction amounts, customer status)?
  - Format issues (mixed date formats, invalid values)?
  These are CRITICAL and must be flagged.

STEP 6 — RECOMMEND INDEXES
  Which columns should be indexed for fast queries?
  Think about:
  - Primary keys / IDs (always index)
  - Date fields used in WHERE/JOIN (always index)
  - Status/category fields with filtering (index if low-cardinality)
  - Foreign keys / relationships (always index)
  - High-cardinality fields unlikely to be filtered (skip)

  For each recommendation, explain WHY it should be indexed.

  Examples of good reasons:
  - "Primary key — fast lookups and joins"
  - "Frequently filtered in WHERE clauses"
  - "Date range queries for reports"
  - "Join condition with other tables"
  - "Sorts and aggregations on this column"
  - "Low-cardinality status field — improves filter selectivity"

Return JSON with schema profile:
{
  "table_name": "derived from filename",
  "total_columns": number,
  "total_rows": number,
  "file_size_mb": number,
  "schema": [
    {
      "column": "exact name",
      "business_meaning": "what this represents",
      "data_type": "CORRECTED type (not just inferred)",
      "nullable": boolean,
      "null_count": number,
      "null_pct": number,
      "unique_count": number,
      "validation_rule": "rule to enforce",
      "sample_values": ["val1", "val2", "val3"],
      "categorical_values": ["val1", "val2"] or [],
      "value_distribution": {"val1": 800, "val2": 450} or {}
    }
  ],
  "key_columns": ["col1", "col2", "col3"],
  "critical_data": [
    {
      "column": "column_name",
      "severity": "HIGH" or "MEDIUM",
      "concern": "Specific issue found",
      "impact": "Business/data quality impact"
    }
  ],
  "quality_concerns": ["Specific concern with details"],
  "recommended_indexes": [
    {
      "column": "applicantid",
      "index_type": "B-Tree",
      "reason": "Primary key — enables fast lookups and join operations"
    }
  ],
  "data_completeness_pct": number (average of non-null percentages)
}
"""

    user_prompt = (
        "CSV METADATA (JSON):\n"
        f"{json.dumps(metadata_payload, separators=(',', ':'), default=str)}\n"
        f"{instructions_section}"
    )

    return system_prompt, user_prompt