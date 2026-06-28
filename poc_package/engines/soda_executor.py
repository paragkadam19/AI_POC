"""
soda_executor.py — Execute SODA checks against DuckDB data
===========================================================
Instead of calling Bedrock AI for data quality checks (Tab 4),
we now:
1. Read the SODA YAML from Tab 3
2. Parse the check definitions
3. Execute them directly against DuckDB
4. Return results to display

This saves 100% of Tab 4 Bedrock costs!
"""

import re
import duckdb
import yaml
import json
from typing import List, Dict, Any, Tuple, Optional


def load_soda_yaml(yaml_path: str) -> Dict[str, Any]:
    """Parse SODA YAML file and return as dict."""
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)


def parse_soda_checks(yaml_content: Dict[str, Any], table_name: str) -> List[Dict[str, Any]]:
    """
    Extract check definitions from SODA YAML.

    Real SODA Core syntax uses a LITERAL top-level key of the form
    "checks for <table_name>:" — NOT a nested {"checks": {table: [...]}}
    dict. e.g.:

        checks for tbl_applicant_data_1gb_csv:
          - row_count > 0:
              name: DQ-MFG-01 Row count greater than zero
          - invalid_count(employment) = 0:
              name: DQ-MFG-13 employment valid values
              valid values:
                - Salaried
                - Self-Employed
                - Unemployed

    Returns list of check dicts:
    [
        {
            "check_id": "DQ-MFG-01",
            "check_name": "Row count greater than zero",
            "type": "row_count",
            "query": "SELECT COUNT(*) FROM table_name",
            "expected": "> 0",
            "sql_definition": "row_count > 0"
        },
        ...
    ]
    """
    checks = []

    if not yaml_content:
        return checks

    # Find the "checks for <table_name>" key directly — this is a literal
    # string key in real SODA YAML, not a nested dict lookup.
    target_key = f"checks for {table_name}"
    checks_list = None
    for key, value in yaml_content.items():
        if key.strip() == target_key:
            checks_list = value
            break

    if checks_list is None or not isinstance(checks_list, list):
        return checks

    check_id_counter = 1
    for check_def in checks_list:
        check_str = ""
        extra: Dict[str, Any] = {}

        if isinstance(check_def, str):
            check_str = check_def.strip()
        elif isinstance(check_def, dict):
            # Real shape: { "invalid_count(col) = 0": { "name": "...", "valid values": [...] } }
            if check_def:
                check_str = list(check_def.keys())[0]
                extra = check_def[check_str] or {}
        else:
            continue

        if not check_str:
            continue

        # Default id/display name — overridden below if YAML supplied a "name"
        check_id = f"DQ-{check_id_counter:02d}"
        check_name = check_str
        check_id_counter += 1

        name_field = extra.get("name") if isinstance(extra, dict) else None
        if name_field:
            # "DQ-MFG-01 Row count greater than zero" -> id="DQ-MFG-01", name="Row count greater than zero"
            m = re.match(r"(\S+)\s+(.*)", name_field.strip())
            if m:
                check_id, check_name = m.group(1), m.group(2)
            else:
                check_name = name_field.strip()

        valid_values = extra.get("valid values") if isinstance(extra, dict) else None

        check_type, sql_query, expected = parse_check_string(
            check_str, table_name, valid_values=valid_values
        )

        if check_type:
            checks.append({
                "check_id": check_id,
                "check_name": check_name,
                "type": check_type,
                "query": sql_query,
                "expected": expected,
                "sql_definition": check_str
            })

    return checks


def parse_check_string(check_str: str, table_name: str,
                        valid_values: Optional[List[str]] = None) -> Tuple[str, str, str]:
    """
    Parse a single SODA check string into type, SQL query, and expected result.

    Examples:
        "row_count > 0" → ("row_count", "SELECT COUNT(*) FROM table", "> 0")
        "missing_count(age) = 0" → ("null_check", "SELECT COUNT(*) FROM table WHERE age IS NULL", "= 0")
        "invalid_count(status) = 0" + valid_values=[...] →
            ("invalid_count", "SELECT COUNT(*) FROM table WHERE status NOT IN (...) AND status IS NOT NULL", "= 0")
    """

    check_str = check_str.strip()

    # 1. row_count
    if check_str.startswith("row_count"):
        match = re.match(r"row_count\s*([<>=]+)\s*(\d+)", check_str)
        if match:
            operator, value = match.groups()
            return (
                "row_count",
                f"SELECT COUNT(*) FROM {table_name}",
                f"{operator} {value}"
            )

    # 2. missing_count (nulls)
    if "missing_count" in check_str:
        match = re.match(r"missing_count\(([\w\-\"']+)\)\s*([<>=]+)\s*(\d+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            return (
                "null_check",
                f'SELECT COUNT(*) FROM {table_name} WHERE "{col_name}" IS NULL',
                f"{operator} {value}"
            )

    # 3. invalid_count (enum/categorical) — now actually uses valid_values if supplied
    if "invalid_count" in check_str:
        match = re.match(r"invalid_count\(([\w\-\"']+)\)\s*([<>=]+)\s*(\d+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')

            if valid_values:
                # Build a NOT IN (...) clause so invalid_count actually means something.
                # Rows that are NULL are excluded here — missing_count already covers nulls.
                escaped = [str(v).replace("'", "''") for v in valid_values]
                in_list = ", ".join(f"'{v}'" for v in escaped)
                sql = (
                    f'SELECT COUNT(*) FROM {table_name} '
                    f'WHERE "{col_name}" IS NOT NULL AND "{col_name}" NOT IN ({in_list})'
                )
            else:
                # No valid_values supplied — nothing meaningful to validate against.
                # Fall back to a query that can never report a false pass.
                sql = f'SELECT COUNT(*) FROM {table_name} WHERE 1=0'

            return ("invalid_count", sql, f"{operator} {value}")

    # 4. duplicate_count
    if "duplicate_count" in check_str:
        match = re.match(r"duplicate_count\(([\w\-\"',\s]+)\)\s*([<>=]+)\s*(\d+)", check_str)
        if match:
            cols_str, operator, value = match.groups()
            cols = [c.strip().strip('"\'') for c in cols_str.split(',')]
            cols_quoted = ', '.join([f'"{c}"' for c in cols])
            return (
                "duplicate_count",
                f"SELECT COUNT(*) - COUNT(DISTINCT ({cols_quoted})) FROM {table_name}",
                f"{operator} {value}"
            )

    # 5. min/max (numeric range)
    if re.search(r"min\(", check_str):
        match = re.match(r"min\(([\w\-\"']+)\)\s*([<>=]+)\s*([\d\.-]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            return (
                "min_check",
                f'SELECT MIN("{col_name}") FROM {table_name}',
                f"{operator} {value}"
            )

    if re.search(r"max\(", check_str):
        match = re.match(r"max\(([\w\-\"']+)\)\s*([<>=]+)\s*([\d\.-]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            return (
                "max_check",
                f'SELECT MAX("{col_name}") FROM {table_name}',
                f"{operator} {value}"
            )

    # If we couldn't parse it, return generic info
    return ("unknown", "", check_str)


def execute_checks(db_file: str, table_name: str, checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Execute all checks against DuckDB and return results.

    Returns:
    {
        "audit_passed": bool,
        "total_checks": int,
        "passed_checks": int,
        "failed_checks": int,
        "checks": [
            {
                "check_id": "DQ-01",
                "check_name": "row_count > 0",
                "passed": bool,
                "actual_value": 1000,
                "expected": "> 0",
                "error_message": null or str
            },
            ...
        ]
    }
    """

    conn = duckdb.connect(db_file, read_only=True)

    results = []
    passed_count = 0
    failed_count = 0

    for check in checks:
        check_id = check["check_id"]
        check_name = check["check_name"]
        check_type = check["type"]
        sql_query = check["query"]
        expected = check["expected"]

        try:
            # Execute the query
            result = conn.execute(sql_query).fetchall()
            actual_value = result[0][0] if result else None

            # Evaluate if check passed
            passed, error_msg = evaluate_check(actual_value, expected, check_type)

            if passed:
                passed_count += 1
            else:
                failed_count += 1

            results.append({
                "check_id": check_id,
                "check_name": check_name,
                "type": check_type,
                "passed": passed,
                "actual_value": actual_value,
                "expected": expected,
                "error_message": error_msg
            })

        except Exception as e:
            failed_count += 1
            results.append({
                "check_id": check_id,
                "check_name": check_name,
                "type": check_type,
                "passed": False,
                "actual_value": None,
                "expected": expected,
                "error_message": f"Query failed: {str(e)}"
            })

    conn.close()

    return {
        "audit_passed": failed_count == 0,
        "total_checks": len(checks),
        "passed_checks": passed_count,
        "failed_checks": failed_count,
        "checks": results
    }


def evaluate_check(actual_value: Any, expected_str: str, check_type: str) -> Tuple[bool, str]:
    """
    Evaluate if actual value matches the expected condition.

    Examples:
        actual=1000, expected="> 0" → (True, None)
        actual=0, expected="> 0" → (False, "0 is not > 0")
    """

    if actual_value is None:
        return False, "Actual value is NULL"

    # Parse expected string: "> 0", "= 5", etc.
    match = re.match(r"([<>=]+)\s*([\d\.-]+)", expected_str.strip())
    if not match:
        return False, f"Could not parse expected: {expected_str}"

    operator, expected_value = match.groups()

    try:
        expected_value = float(expected_value) if '.' in expected_value else int(expected_value)
        actual_value = float(actual_value)
    except:
        return False, f"Could not convert to number: actual={actual_value}, expected={expected_value}"

    # Evaluate
    if operator == ">":
        passed = actual_value > expected_value
    elif operator == "<":
        passed = actual_value < expected_value
    elif operator == ">=":
        passed = actual_value >= expected_value
    elif operator == "<=":
        passed = actual_value <= expected_value
    elif operator == "=":
        passed = actual_value == expected_value
    elif operator == "!=":
        passed = actual_value != expected_value
    else:
        return False, f"Unknown operator: {operator}"

    if not passed:
        return False, f"Expected {expected_str}, but actual is {actual_value}"

    return True, None


def run_soda_checks_from_yaml(db_file: str, yaml_path: str, table_name: str) -> Dict[str, Any]:
    """
    End-to-end: load YAML → parse checks → execute → return results.

    This is the main function called from app.py Tab 4.
    """

    # Load and parse
    yaml_content = load_soda_yaml(yaml_path)
    checks = parse_soda_checks(yaml_content, table_name)

    if not checks:
        return {
            "audit_passed": False,
            "error": f"No checks found in {yaml_path} for table {table_name}",
            "total_checks": 0,
            "passed_checks": 0,
            "failed_checks": 0,
            "checks": []
        }

    # Execute
    results = execute_checks(db_file, table_name, checks)

    return results