"""
prompt_builder.py — Dynamic Prompt Generation from Metadata
============================================================
Instead of hardcoded prompts, we:
1. Read metadata (filename, rows, columns, schema, statistics, samples)
2. Build prompts dynamically based on actual data
3. System prompt and user prompt both generated from metadata
4. AI gets accurate context about the specific data

This allows prompts to adapt to any CSV, any structure, any data.
"""

import json


def build_schema_discovery_prompt(metadata: dict) -> tuple:
    """
    Build SYSTEM and USER prompts for schema discovery from metadata.
    
    Input: metadata dict with:
    - filename, file_size_mb, total_rows, total_columns
    - schema (column names + types)
    - columns (with null_count, null_pct, unique_count, min/max)
    - sample_rows (actual data samples)
    
    Returns: (system_prompt, user_prompt)
    """
    
    filename = metadata.get("filename", "unknown.csv")
    file_size_mb = metadata.get("file_size_mb", 0)
    file_size_kb = metadata.get("file_size_kb", 0)
    total_rows = metadata.get("total_rows", 0)
    total_columns = metadata.get("total_columns", 0)
    schema = metadata.get("schema", {})
    columns = metadata.get("columns", [])
    sample_rows = metadata.get("sample_rows", [])
    
    # Format file size nicely
    if file_size_mb > 0:
        size_str = f"{file_size_mb} MB"
    else:
        size_str = f"{file_size_kb} KB"
    
    # ========== BUILD SYSTEM PROMPT (dynamically) ==========
    system_prompt = f"""You are an expert data engineer and schema analyst.

Your task is to analyze a CSV file and produce a comprehensive schema profile.

FILE INFORMATION:
- Filename: {filename}
- File Size: {size_str}
- Total Rows: {total_rows:,}
- Total Columns: {total_columns}

You will receive:
1. Polars-verified schema (100% accurate data types)
2. Column statistics (nulls, distinct counts, ranges)
3. Sample rows (actual data from the file)

Your job is to:
1. Understand what each column represents based on name and data
2. Verify Polars' inferred types against sample values
3. If types don't match samples, correct them
4. Infer business meaning for each column
5. Identify key columns critical for data quality
6. Identify CRITICAL DATA POINTS (columns with data quality issues, security concerns, or business impact)
7. Flag quality concerns (high nulls, duplicates, invalid formats)
8. Recommend database indexes for performance

Return ONLY valid JSON with no markdown or extra text.
"""
    
    # ========== BUILD USER PROMPT (dynamically) ==========
    
    # Section 1: Schema information
    schema_section = "POLARS-VERIFIED SCHEMA (100% accurate, no guessing):\n"
    for col_name, dtype in schema.items():
        schema_section += f"  {col_name}: {dtype}\n"
    
    # Section 2: Column statistics
    stats_section = "\nCOLUMN STATISTICS (Nulls, Distinct Counts, Ranges, Categorical Values):\n"
    for col in columns:
        col_name = col.get("name", "unknown")
        null_count = col.get("null_count", 0)
        null_pct = col.get("null_pct", 0)
        unique_count = col.get("unique_count", 0)
        
        stats_section += f"\n  {col_name}\n"
        stats_section += f"    Type: {col.get('dtype', '?')}\n"
        stats_section += f"    Null Count: {null_count} ({null_pct}%)\n"
        stats_section += f"    Distinct Count: {unique_count}\n"
        
        if "min" in col and "max" in col and col["min"] and col["max"]:
            stats_section += f"    Range: [{col['min']} to {col['max']}]\n"
        
        # NEW: Include distinct values for low-cardinality columns
        if "distinct_values" in col and col["distinct_values"]:
            stats_section += f"    Distinct Values: {col['distinct_values']}\n"
    
    # Section 3: Sample rows
    sample_section = "\nSAMPLE ROWS (actual data from CSV):\n"
    sample_section += "These samples let you verify if types match actual values:\n"
    for i, row in enumerate(sample_rows, 1):
        sample_section += f"\n  Row {i}:\n"
        for k, v in row.items():
            sample_section += f"    {k}: {repr(v)}\n"
    
    # Section 4: Analysis instructions
    instructions_section = """
ANALYSIS STEPS:

STEP 1 — VERIFY TYPES
  Review Polars-verified schema and sample rows.
  If samples show different types than schema, correct it.
  Example: Schema says Int64, but samples show ["25", "N/A"] → correct to Utf8/String

STEP 2 — UNDERSTAND BUSINESS MEANING
  Based on column name and sample values, what does this column represent?
  Example: "date_of_birth" + samples ["1990-05-15", "2000-01-20"] → "Customer birth date"

STEP 3 — DATA QUALITY ASSESSMENT
  Look at null counts, distinct counts, ranges.
  For low-cardinality columns, review the distinct values to understand all possible categories.
  Flag issues: high nulls (>50%), all same value, duplicates, future dates, unexpected values, etc.
  Example: "employment" should only have ["salaried", "business", "self-employed"] — if you see other values, flag it!

STEP 4 — IDENTIFY KEY COLUMNS
  Which 3-5 columns are most critical for quality analysis?
  Usually: IDs, dates, amounts, status fields

STEP 5 — IDENTIFY CRITICAL DATA POINTS
  Which columns have:
  - Data quality issues (high nulls, duplicates, inconsistent formats)?
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
      "data_type": "CORRECTED type (not just Polars)",
      "nullable": boolean,
      "null_count": number,
      "null_pct": number,
      "unique_count": number,
      "validation_rule": "rule to enforce",
      "sample_values": ["val1", "val2", "val3"],
      "categorical_values": ["val1", "val2"] or []
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
    },
    {
      "column": "createddt",
      "index_type": "B-Tree",
      "reason": "Date field frequently used in range queries and report filtering"
    },
    {
      "column": "employment",
      "index_type": "Hash",
      "reason": "Low-cardinality status field — improves WHERE clause selectivity"
    }
  ],
  "data_completeness_pct": number (average of non-null percentages)
}
"""
    
    user_prompt = f"""{schema_section}{stats_section}{sample_section}{instructions_section}"""
    
    return system_prompt, user_prompt


def build_data_quality_prompt(metadata: dict, soda_yaml: str) -> tuple:
    """
    Build prompts for data quality analysis using SODA checks.
    """
    
    filename = metadata.get("filename", "unknown.csv")
    total_rows = metadata.get("total_rows", 0)
    total_columns = metadata.get("total_columns", 0)
    schema = metadata.get("schema", {})
    
    system_prompt = f"""You are a data quality expert.

Your task is to design comprehensive data quality checks for the dataset:
- File: {filename}
- Rows: {total_rows:,}
- Columns: {total_columns}

You have access to the SODA YAML checks already generated.
Use this to understand what quality rules have been defined.

Return ONLY valid JSON with quality assessment and recommendations.
"""
    
    user_prompt = f"""Dataset Information:
- Filename: {filename}
- Total Rows: {total_rows:,}
- Total Columns: {total_columns}

Schema:
{json.dumps(schema, indent=2)}

SODA Quality Checks Generated:
{soda_yaml}

Analyze the SODA checks and provide:
1. Which checks are most critical
2. Which columns need special attention
3. What data quality issues might exist
4. Recommendations for data cleaning

Return JSON format:
{{
  "critical_checks": ["list of important checks"],
  "columns_needing_attention": ["column names"],
  "potential_issues": ["issue descriptions"],
  "recommendations": ["action items"]
}}
"""
    
    return system_prompt, user_prompt


def build_soda_yaml_prompt(metadata: dict) -> tuple:
    """
    Build prompts for SODA YAML generation from metadata.
    """
    
    filename = metadata.get("filename", "unknown.csv")
    total_rows = metadata.get("total_rows", 0)
    schema = metadata.get("schema", {})
    columns = metadata.get("columns", [])
    
    system_prompt = f"""You are a SODA Core expert.

Your task is to generate SODA quality check YAML for the dataset:
- File: {filename}
- Rows: {total_rows:,}
- Columns: {len(schema)}

Generate SODA Core 3.x compliant YAML checks based on the schema and statistics.
Include checks for: row count, nulls, duplicates, ranges, valid values.

Return ONLY the SODA YAML content, no markdown or extra text.
"""
    
    # Build column info for prompt
    column_info = "Columns and their statistics:\n"
    for col in columns:
        col_name = col.get("name", "?")
        dtype = col.get("dtype", "?")
        null_pct = col.get("null_pct", 0)
        unique_count = col.get("unique_count", 0)
        
        column_info += f"\n  {col_name} ({dtype})"
        column_info += f"\n    Nulls: {null_pct}% | Distinct: {unique_count}"
    
    user_prompt = f"""Generate SODA YAML checks for this dataset:

{column_info}

Design checks for:
1. Row count (must be > 0)
2. Non-nullable columns (missing_count = 0)
3. Unique/key columns (duplicate_count = 0)
4. Numeric ranges (min/max)
5. Enum/categorical validity (valid_values)
6. Data quality thresholds

Format: Standard SODA Core 3.x YAML
Start with: checks for table_name:
Include check names like DQ-MFG-01, DQ-MFG-02, etc.
"""
    
    return system_prompt, user_prompt


def format_metadata_summary(metadata: dict) -> str:
    """Format metadata as a human-readable summary."""
    
    summary = f"""
═══════════════════════════════════════════════════════
METADATA SUMMARY
═══════════════════════════════════════════════════════
File: {metadata.get('filename', 'unknown')}
Size: {metadata.get('file_size_kb', 0)} KB
Rows: {metadata.get('total_rows', 0):,}
Columns: {metadata.get('total_columns', 0)}

SCHEMA:
{json.dumps(metadata.get('schema', {}), indent=2)}

COLUMN STATISTICS:
"""
    
    for col in metadata.get('columns', []):
        summary += f"\n  {col.get('name', '?')}"
        summary += f"\n    Type: {col.get('dtype', '?')}"
        summary += f"\n    Nulls: {col.get('null_count', 0)} ({col.get('null_pct', 0)}%)"
        summary += f"\n    Distinct: {col.get('unique_count', 0)}\n"
    
    return summary