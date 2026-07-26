"""Dynamic prompt generation for Schema Discovery (Tab 2 / POC 1).

We keep the full metadata, but send it in a denser prompt with less
repeated prose so the model gets the same facts with fewer tokens.
"""

import json


def build_schema_discovery_prompt(metadata: dict) -> tuple:
    filename = metadata.get("filename", "unknown.csv")
    file_size_mb = metadata.get("file_size_mb", 0)
    file_size_kb = metadata.get("file_size_kb", 0)
    total_rows = metadata.get("total_rows", 0)
    total_columns = metadata.get("total_columns", 0)
    schema = metadata.get("schema", {})
    columns = metadata.get("columns", [])
    sample_rows = metadata.get("sample_rows", [])

    size_str = f"{file_size_mb} MB" if file_size_mb > 0 else f"{file_size_kb} KB"

    system_prompt = f"""You are a senior data engineer and schema analyst.
Create a complete schema profile from CSV metadata.

FILE
- name: {filename}
- size: {size_str}
- rows: {total_rows:,}
- cols: {total_columns}

INPUT
- s = verified schema types
- c = column stats (nulls, distincts, ranges, value_counts)
- r = sample rows

TASK
1. Verify/correct types from samples.
2. Infer business meaning from name + data.
3. Flag quality issues, PII/security, imbalance, invalid formats.
4. Pick key columns and recommended indexes.
5. Return ONLY valid JSON.
"""

    metadata_payload = {
        "schema": schema,
        "column_statistics": [
            {
                "name": col.get("name"),
                "type": col.get("dtype"),
                "null_count": col.get("null_count", 0),
                "null_pct": col.get("null_pct", 0),
                "distinct_count": col.get("unique_count", 0),
                **({"min": col["min"], "max": col["max"]}
                   if col.get("min") and col.get("max") else {}),
                **({"value_counts": col["value_counts"]}
                   if col.get("value_counts") else {}),
            }
            for col in columns
        ],
        "sample_rows": sample_rows,
    }

    instructions_section = """
ANALYSIS
1. verify/correct types from samples
2. infer meaning from name + data
3. assess nulls/distincts/ranges/value_counts
4. identify key columns (IDs, dates, amounts, status)
5. flag critical data points (quality/PII/business impact/format)
6. recommend indexes with reason
7. add dependency checks where one column controls another

QUALITY
- Use value_counts to detect imbalance or rare bad values.
- Keep all metadata; do not invent unsupported facts.
- For dependencies, generate failed-rows checks.

RETURN JSON:
{
  "table_name": "derived from filename",
  "total_columns": 0,
  "total_rows": 0,
  "file_size_mb": 0,
  "schema": [
    {
      "column": "exact name",
      "business_meaning": "what this represents",
      "data_type": "corrected dtype based on actual samples (not just Polars inference!)",
      "nullable": true,
      "null_count": 0,
      "null_pct": 0,
      "unique_count": 0,
      "validation_rule": "rule to enforce",
      "sample_values": ["val1", "val2", "val3"],
      "categorical_values": ["val1", "val2"]
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
  "quality_concerns": ["Specific concern with explanation"],
  "recommended_indexes": [
    {
      "column": "applicantid",
      "index_type": "B-Tree",
      "reason": "Primary key — enables fast lookups and join operations"
    }
  ],
  "data_completeness_pct": 0
}
"""

    user_prompt = (
        "CSV_METADATA=\n"
        f"{json.dumps(metadata_payload, separators=(',', ':'), default=str)}\n"
        f"{instructions_section}"
    )

    return system_prompt, user_prompt
