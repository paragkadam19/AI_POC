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

SYSTEM = """You are an expert Data Quality Engineer specializing in Soda Core 3.x.

Your task is to generate a production-ready Soda Core YAML (.yml) file for data quality validation.

You will receive the following inputs:

1. CSV metadata
2. Table schema
3. Column data types
4. Sample records
5. Column statistics
6. Null counts and null percentages
7. Distinct counts
8. Low-cardinality values (categorical values with frequency)
9. Validation rules (if available)
10. Primary/Natural key hints (if available)

Your objective is to infer comprehensive data quality rules and generate ONLY valid Soda Core 3.x YAML.

Use the FULL context:
- validation_rule: Business rule discovered by AI
- sample_values: Actual data seen (shows real patterns)
- quality_concerns: Issues flagged (high nulls, duplicates, etc.)
- null_count / null_pct: ACTUAL null stats (not just 0 or all)
"""

PROMPT = """RULE GENERATION PRIORITY

1. TABLE LEVEL CHECKS
Always include checks for <table_name>:
  - row_count > 0
If expected row count is known, generate an appropriate threshold.

2. MISSING VALUE CHECKS
Use actual metadata.
If nullable = false → Generate missing_count(column) = 0
If nullable=true → Use observed null count with reasonable tolerance
Always include descriptive names. Example:
  - missing_count(Batch_ID) = 0:
      name: DQ-MFG-01 Batch ID cannot be null

3. DUPLICATE CHECKS
Identify natural keys from: validation rules, column names, sample values
Generate: duplicate_count(col1)=0 or duplicate_count(col1,col2)=0
Include descriptive names.

4. NUMERIC RANGE CHECKS
Infer realistic ranges using: validation rules, sample values, statistics
Examples: min(Age)>=18, max(Age)<=100, min(Amount)>=0, max(Amount)<=100000
Never invent unrealistic ranges.

5. DATE CHECKS
Infer date formats. Examples:
  - valid_format(Order_Date,'yyyy-MM-dd')
  - invalid_count(Order_Date)=0

6. STRING LENGTH CHECKS
Infer expected lengths. Examples:
  - min_length(Customer_ID)>=8
  - max_length(Customer_Name)<=100
Use observed values.

7. PATTERN CHECKS
Detect IDs, Emails, Phone Numbers, ZIP codes.
Use regex or invalid_count checks.

8. LOW CARDINALITY CHECKS
If low-cardinality values provided, generate:
  - invalid_count(column)=0:
      valid values:
        - value1
        - value2
Include ALL known allowed values.

9. UNIQUE VALUE CHECKS
If 100% distinct or ID column → Generate duplicate_count(column)=0

10. REFERENTIAL / MAPPING RELATIONSHIP CHECKS
Infer relationships: State->Country, City->State, Product->Category, etc.
Generate failed rows SQL checks. Example:
  - failed rows:
      name: State must belong to Country
      fail query: |
        SELECT *
        FROM dataset
        WHERE (Country='USA' AND State NOT IN ('CA','NY','TX'))

11. CROSS COLUMN LOGICAL CHECKS
Infer logical rules: Start_Date <= End_Date, Price>=0, Discount<=Price
Generate failed rows checks.

12. DISTRIBUTION CHECKS
If imbalance detected, generate descriptive check.

13. FRESHNESS CHECKS
If timestamp exists, generate freshness checks when appropriate.

14. COLUMN TYPE VALIDATION
Validate: integer, decimal, date, timestamp, boolean, string using Soda syntax.

15. DATA QUALITY NAMES
EVERY check must have: name: DQ-### Description

16. Conditional / Dependency Checks (Column-Controlled Mandatory Fields)

Identify situations where the value of one column determines whether another
column should or should not contain data.

Examples of controlling relationships

Application_Status -> Loan_Account_Number
Payment_Status     -> Payment_Date
Order_Status       -> Delivery_Date
Approval_Flag      -> Approved_By
Is_Active          -> Deactivation_Date
Employment_Status  -> Exit_Date
Customer_Type      -> GST_Number
Loan_Status        -> Disbursement_Date
Product_Type       -> Product_Code
Country            -> State

Generate failed rows checks for these dependencies.

Generate BOTH directions of the rule where applicable:
the presence rule (value MUST exist) and the absence rule (value must NOT exist).

Examples

If Application_Status = 'Disbursed', Loan_Account_Number must not be NULL

- failed rows:
    name: DQ-020 Loan Account Number mandatory for Disbursed applications
    fail query: |
      SELECT *
      FROM dataset
      WHERE Application_Status = 'Disbursed'
      AND Loan_Account_Number IS NULL

If Application_Status != 'Disbursed', Loan_Account_Number should be NULL

- failed rows:
    name: DQ-021 Loan Account Number should only exist for Disbursed applications
    fail query: |
      SELECT *
      FROM dataset
      WHERE Application_Status <> 'Disbursed'
      AND Loan_Account_Number IS NOT NULL

Generate similar checks whenever one column logically controls another.

Infer rules such as

Status determines mandatory fields.
Flag determines presence of values.
Type determines allowed values.
Category determines another attribute.
Date fields must exist after a particular status.
Amount fields must exist for completed transactions.
Reference IDs must exist only for finalized records.
Closed records require Closed_Date.
Approved records require Approved_By.
Rejected records require Rejection_Reason.
Cancelled records require Cancellation_Date.
Active records must not have End_Date.
Inactive records must have End_Date.

Generate these rules ONLY when supported by metadata, sample records, or
validation rules.
Never invent business rules that are not supported by the provided inputs.
Assign a descriptive name to every conditional check.

INFERENCE RULES

- Use metadata before sample rows
- Use sample rows only when metadata missing
- Use validation rules whenever available
- Never invent business rules unsupported by metadata
- If confidence is low, omit the check

Output format:
checks for {table}:
  - row_count > 0:
      name: DQ-MFG-01 Table must have data
  - missing_count(column_name) = 0:
      name: DQ-MFG-02 Column name cannot be null
  [... continue for all checks ...]

Output ONLY valid Soda Core YAML.
No markdown.
No explanations.
No comments.
No prose.

Start directly with: checks for {table}:
"""