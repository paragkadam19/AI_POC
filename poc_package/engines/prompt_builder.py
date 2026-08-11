"""Dynamic prompt generation for Schema Discovery (Tab 2 / POC 1).

We keep the full metadata, but send it in a denser prompt with less
repeated prose so the model gets the same facts with fewer tokens.
"""

import json


def _compact_value(value, limit: int = 80):
    if value is None:
        return None
    text = str(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return value


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

IMPORTANT
- Do not include audit columns such as system_date, system_active, or file_path in critical_data, quality_concerns, or recommended_indexes unless the user explicitly asks for them.

TASK
1. Verify/correct types from samples.
   The output `data_type` is the AI-inferred type, while the app will also
   preserve the actual DuckDB type separately as `bronze_datatype`.
   Return both fields for every column, even if they are the same.
   For `bronze_datatype`, copy the exact type from the input schema value
   for that column. Do not change it, infer it, or leave it blank.
2. Write a short but useful column description.
3. Write a detailed business meaning for each column.
4. Provide 1 example value or sample for each column.
5. Flag quality issues, PII/security, imbalance, invalid formats.
6. Pick key columns and recommended indexes.
7. Return ONLY valid JSON.
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
        "sample_rows": [
            {
                key: _compact_value(value)
                for key, value in row.items()
            }
            for row in sample_rows[:3]
        ],
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
      "column_description": "short useful description",
      "business_meaning": "what this represents",
      "data_type": "corrected dtype based on actual samples (not just Polars inference!)",
      "bronze_datatype": "actual DuckDB type from the uploaded table",
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

    metadata_json = json.dumps(
        metadata_payload,
        separators=(',', ':'),
        default=str,
        ensure_ascii=True
    )

    user_prompt = (
        "CSV_METADATA=\n"
        f"{metadata_json}\n"
        f"{instructions_section}"
    )

    return system_prompt, user_prompt