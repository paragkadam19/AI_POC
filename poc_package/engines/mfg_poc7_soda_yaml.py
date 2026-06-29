"""
Manufacturing POC 7 — SODA Quality Checks YAML Generator (ENHANCED)
===================================================================
Uses FULL schema profile including:
- Validation rules (business rules discovered by AI)
- Null percentages (smart thresholds, not just 0)
- Sample values (actual data patterns)
- Quality concerns (issues to flag)
- Categorical values (with counts)
"""

SYSTEM = """You are a data quality engineer expert in SODA Core 3.x.
You write valid SODA Core 3.x YAML check files based on a schema
profile INCLUDING validation rules, sample values, quality concerns,
and actual null patterns — NOT just columns.

Return only valid YAML. No markdown fences, no explanation, no comments.

Use the FULL context:
- validation_rule: Business rule discovered by AI
- sample_values: Actual data seen (shows real patterns)
- quality_concerns: Issues flagged (high nulls, duplicates, etc.)
- null_count / null_pct: ACTUAL null stats (not just 0 or all)
"""

PROMPT = """
You have a COMPLETE schema profile (from automated schema discovery)
for table: {table}

This profile includes:
1. Column names and data types
2. Null counts AND percentages (actual data patterns)
3. Sample values (real data from the file)
4. Validation rules (business rules AI inferred)
5. Categorical values WITH frequency counts (class distribution)
6. Quality concerns (high nulls, duplicates, format issues, etc.)
7. Critical data points (security/business impact)

Generate a SODA Core 3.x quality checks YAML file using ALL this context.

CRITICAL — SODA Core 3.x check syntaxes:

1. Row count (always):
   - row_count > 0

2. Null checks (use ACTUAL null_pct, not just 0):
   - missing_count(column_name) = 0         # For nullable=false
   - missing_count(column_name) < 50        # If actual nulls=45, allow some
   - missing_count(column_name) < 1000      # If actual nulls=500, allow <1000

3. Duplicate checks (for natural keys identified in validation_rule):
   - duplicate_count(column1, column2) = 0

4. Numeric range (from validation_rule + sample_values):
   - min(column_name) >= 0
   - max(column_name) <= 1000000
   (Use sample_values and validation_rule to set realistic bounds)

5. Valid values (from categorical_values WITH distribution):
   - invalid_count(column_name) = 0:
       valid values:
         - value1
         - value2

6. Class balance check (if categorical with imbalance in quality_concerns):
   - missing_count(column_name) = 0:
       name: DQ-MFG-07 Column X class imbalance detected

7. Named checks (always add name):
   - missing_count(Batch_ID) = 0:
       name: DQ-MFG-01 Batch ID cannot be null (critical key)

DO NOT use: alert, fail, warn, valid_format, regex, or unsupported keys.

═════════════════════════════════════════════════════════════════

INSTRUCTIONS — use FULL schema context:

STEP 1: ROW COUNT (always)
  - Start with: row_count > 0
  - Name: DQ-MFG-01 Table must have data

STEP 2: NULL CHECKS (use actual null_pct, not just ≤0)
  For EVERY column:
    - If validation_rule says "Required" or "Cannot be null":
      missing_count(col) = 0 with name
    - If null_count > 0 but null_pct < 5%:
      missing_count(col) < 10 (allow observed nulls, warn if much higher)
    - If null_count > 0 and null_pct > 5%:
      missing_count(col) < (null_count * 2) (allow 2x normal)
    - If quality_concerns mentions "high nulls":
      flag with MEDIUM severity threshold

  Example from schema:
    column: "created_date"
    null_count: 0
    nullable: false
    validation_rule: "Valid DATE format, should not be future-dated"
    → Check: missing_count(created_date) = 0
                name: DQ-MFG-02 Creation date is required

    column: "notes"
    null_count: 500
    null_pct: 2.7
    nullable: true
    → Check: missing_count(notes) < 1000
                name: DQ-MFG-08 Notes nulls stay within 2.7% baseline

STEP 3: RANGE CHECKS (use sample_values + validation_rule)
  For every numeric column:
    - Extract min/max from sample_values
    - Read validation_rule for business bounds
    - Set realistic checks based on BOTH
    - Do NOT assume 0-100% for percentages (they're strings!)

  Example:
    column: "income"
    data_type: "FLOAT64"
    sample_values: ["50000", "75000", "120000", "500000"]
    validation_rule: "Annual salary, >= $30k, <= $2M"
    → Checks:
        - min(income) >= 30000
        - max(income) <= 2000000

  Example (DO NOT assume 0-100 for percentages):
    column: "approval_rate"
    data_type: "VARCHAR"  ← STRING, not numeric!
    sample_values: ["97.50%", "85.00%", "100%"]
    → DO NOT write: min(approval_rate) >= 0
    → Instead: Check only for format via categorical_values

STEP 4: DUPLICATE KEY CHECKS (from validation_rule + data_type)
  - Look for columns where validation_rule says "unique" or "primary key"
  - Look for ID-like columns (applicantid, transaction_id, etc.)
  - If you find a likely natural key (e.g., applicantid + sequence):
    duplicate_count(applicantid, sequence) = 0
    name: DQ-MFG-XX Natural key uniqueness

STEP 5: CATEGORICAL/VALID VALUES (from categorical_values + distribution)
  For EVERY column with categorical_values list:
    - Check length: if <= 10 unique values, add invalid_count check
    - Use actual categorical_values names exactly
    - Add name showing the valid set
    - If quality_concerns mentions unexpected values, flag them

  Example:
    column: "employment"
    data_type: "VARCHAR"
    categorical_values: ["Salaried", "Self-Employed", "Unemployed"]
    value_counts: {"Salaried": 1200, "Self-Employed": 450, "Unemployed": 90}
    quality_concerns: ["employment has only 3 values"]
    → Check:
        - invalid_count(employment) = 0:
            name: DQ-MFG-03 Employment only valid types
            valid values:
              - Salaried
              - Self-Employed
              - Unemployed

STEP 6: CLASS IMBALANCE (from quality_concerns + value_counts)
  If quality_concerns mentions "class imbalance" or one value > 95%:
    - Add check noting the concern
    - Use missing_count as marker:
      missing_count(imbalanced_column) < 50:
          name: DQ-MFG-XX Column X has class imbalance detected

STEP 7: QUALITY CONCERNS (read and flag)
  For EVERY quality_concern listed:
    - "High nulls (>50%)" → Add missing_count threshold
    - "Duplicates detected" → Add duplicate_count check
    - "Format inconsistency" → Add invalid_count or note
    - "Unexpected values" → Reference in categorical check

  Example quality_concern: "employment has only 3 values but 'XYZ' 
  appears 2x (likely error)"
  → Add comment in check: name: DQ-MFG-03 Employment validation 
    (XYZ flagged as potential error)

STEP 8: SAMPLE VALUE VALIDATION (use sample_values for reality check)
  Before finalizing min/max/ranges:
    - Look at sample_values
    - If they show different patterns, adjust check bounds
    - Example: min(income) sample is ["50000", ...], not ["0", ...]
      → Set min(income) >= 30000, not >= 0

STEP 9: BUSINESS RULES (from validation_rule)
  For EVERY column with validation_rule:
    - Encode the rule into the appropriate SODA check
    - Use rule text in the check name
    - Example rule: "Date should not be future-dated"
      → Add comment: name: DQ-MFG-05 Creation date not future-dated

STEP 10: NUMBERING
  - Start at DQ-MFG-01
  - Increment for every check
  - Group by category: nulls, ranges, categorical, keys, concerns

═════════════════════════════════════════════════════════════════

SCHEMA PROFILE (COMPLETE — use all fields):
{schema_profile}

Output format:
checks for {table}:
  - row_count > 0:
      name: DQ-MFG-01 Table must have data
  - missing_count(column_name) = 0:
      name: DQ-MFG-02 Column name cannot be null
  [... continue for all checks ...]

Start directly with: checks for {table}:
"""