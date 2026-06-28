"""
Manufacturing POC 7 — SODA Quality Checks YAML Generator
=========================================================
This module only defines the prompt used by app.py's /api/poc7/run
route. app.py passes in the active dataset's schema profile and
table name directly — no hardcoded schema-profile path here, so
this works the same regardless of which file was uploaded.

Approach: Uses the schema already discovered by POC 1 — column names,
types, null counts, and categorical values. AI dynamically writes a
SODA Core 3.x YAML based on whatever schema POC 1 found, so this
works for ANY CSV without hardcoded column names.
"""

SYSTEM = """You are a data quality engineer expert in SODA Core 3.x.
You write valid SODA Core 3.x YAML check files based on a schema
profile, not raw data. Return only valid YAML. No markdown fences,
no explanation, no comments."""

PROMPT = """
You have a schema profile (from an automated schema discovery step)
for table: {table}

Generate a SODA Core 3.x quality checks YAML file using ONLY this
schema profile — do not assume any columns beyond what is listed.

CRITICAL — use ONLY these supported SODA Core 3.x check syntaxes:

1. Row count:
   - row_count > 0

2. Null checks (for non-nullable or low-null columns):
   - missing_count(column_name) = 0

3. Duplicate checks (for any column pair that looks like a natural key,
   e.g. an ID column + a step/sequence column):
   - duplicate_count(column1, column2) = 0

4. Numeric range (only for data_type integer or float, NEVER for
   data_type percentage since those are stored as strings like '97.00%'):
   - min(column_name) >= 0

5. Valid values (only for data_type categorical, using the
   categorical_values list from the schema profile):
   - invalid_count(column_name) = 0:
       valid values:
         - value1
         - value2

6. Named checks (add name as sub-key):
   - missing_count(Batch_ID) = 0:
       name: DQ-MFG-01 Batch ID not null

DO NOT use: alert, fail, warn, valid_format, regex, max()/min() on
percentage columns, arithmetic expressions like (a + b = c), or any
key not shown above.

INSTRUCTIONS — build the checks yourself from the schema profile:
1. Always start with row_count > 0
2. For every column where nullable=false OR null_count=0, add a
   missing_count check
3. Identify the most likely natural key (commonly an ID column plus a
   sequence/step column) and add ONE duplicate_count check on that pair
4. For every column with data_type integer or float, add a
   min(column) >= 0 check (skip if the column commonly contains
   negative values per its business_meaning)
5. For every column with data_type categorical AND a categorical_values
   list of 10 or fewer values, add an invalid_count check using those
   exact values
6. Skip any column flagged in quality_concerns as "100% null" or
   "dead column" entirely — do not generate checks for it
7. Number every check DQ-MFG-01, DQ-MFG-02, ... sequentially
8. Quote any column name containing special characters (%, -, spaces)

SCHEMA PROFILE (from automated discovery):
{schema_profile}

Start directly with: checks for {table}:
"""