"""
soda_executor.py — Execute SODA checks against DuckDB data
===========================================================
"""

import re
import json
import duckdb
import yaml
from datetime import date, datetime
from typing import List, Dict, Any, Tuple, Optional
from logger_config import get_logger

logger = get_logger(__name__)


def make_serializable(obj):
    """Convert non-JSON-serializable objects to strings."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    elif isinstance(obj, (list, tuple)):
        return [make_serializable(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    else:
        return obj


def _rows_to_dicts(description, rows, limit: int = 3) -> List[Dict[str, Any]]:
    cols = [d[0] for d in (description or [])]
    out = []
    for row in (rows or [])[:limit]:
        out.append({cols[i]: row[i] for i in range(min(len(cols), len(row)))})
    return out


def load_soda_yaml(yaml_path: str) -> Dict[str, Any]:
    """Parse SODA YAML file and return as dict."""
    with open(yaml_path, 'r') as f:
        try:
            return yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in {yaml_path}: {e}") from e


def sanitize_soda_yaml_text(yaml_text: str) -> str:
    """
    Best-effort cleanup for common YAML issues produced by the LLM.
    """
    lines = (yaml_text or "").splitlines()
    out = []

    def quote_scalar(value: str) -> str:
        value = value.strip()
        if value.startswith("'") and value.endswith("'"):
            return value
        if value.startswith('"') and value.endswith('"'):
            return value
        return "'" + value.replace("'", "''") + "'"

    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        if stripped.startswith("name:"):
            raw = stripped[len("name:"):].strip()
            if raw and not (raw.startswith("'") and raw.endswith("'")):
                if ":" in raw or '"' in raw or any(ch in raw for ch in ["<", ">", "=", "(", ")", "/"]):
                    stripped = "name: " + quote_scalar(raw)
                    line = indent + stripped

        if stripped.startswith("fail query:"):
            out.append(line)
            continue

        if re.match(r"^(min|max)\([^)]+\)\s*[<>=!]+\s*.+$", stripped):
            left, right = stripped.split(":", 1) if ":" in stripped else (None, None)
            # no-op here; the invalid YAML usually comes from name values
            pass

        out.append(line)

    return "\n".join(out)


def parse_soda_checks(yaml_content: Dict[str, Any], table_name: str, full_table_name: str = None) -> List[Dict[str, Any]]:
    """Extract check definitions from SODA YAML.
    
    table_name here is the bare table name used in YAML key matching
    (e.g. 'orders', not 'parag.orders').
    
    full_table_name is the fully qualified name for SQL generation
    (e.g. 'parag.orders'). If not provided, falls back to table_name.
    """
    checks = []

    if not yaml_content:
        return checks

    # Use bare table_name for YAML key matching
    target_key = f"checks for {table_name}"
    checks_list = None
    for key, value in yaml_content.items():
        if key.strip() == target_key:
            checks_list = value
            break

    if checks_list is None or not isinstance(checks_list, list):
        return checks

    # Use full_table_name for SQL generation, fallback to table_name
    sql_table = full_table_name or table_name

    check_id_counter = 1
    for check_def in checks_list:
        check_str = ""
        extra: Dict[str, Any] = {}

        if isinstance(check_def, str):
            check_str = check_def.strip()
        elif isinstance(check_def, dict):
            if check_def:
                check_str = list(check_def.keys())[0]
                extra = check_def[check_str] or {}
        else:
            continue

        if not check_str:
            continue

        check_id = f"DQ-{check_id_counter:03d}"
        check_name = check_str
        check_id_counter += 1

        name_field = extra.get("name") if isinstance(extra, dict) else None
        if name_field:
            m = re.match(r"(\S+)\s+(.*)", name_field.strip())
            if m:
                check_id, check_name = m.group(1), m.group(2)
            else:
                check_name = name_field.strip()

        valid_values = extra.get("valid values") if isinstance(extra, dict) else None

        check_type, sql_query, expected = parse_check_string(
            check_str, sql_table, valid_values=valid_values, extra=extra
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
                        valid_values: Optional[List[str]] = None,
                        extra: Optional[Dict] = None) -> Tuple[str, str, str]:
    """Parse a single SODA check string into type, SQL query, and expected result.
    
    table_name here is the fully qualified name used in SQL
    (e.g. 'parag.orders' for multi-tenant, or just 'orders' for single user).
    """

    check_str = check_str.strip()
    extra = extra or {}

    # 1. row_count
    if check_str.startswith("row_count"):
        match = re.match(r"row_count\s*([<>=!]+)\s*(\d+)", check_str)
        if match:
            operator, value = match.groups()
            if operator == "==":
                operator = "="
            return (
                "row_count",
                f"SELECT COUNT(*) FROM {table_name}",
                f"{operator} {value}"
            )

    # 2. missing_count (nulls)
    if "missing_count" in check_str:
        match = re.match(r"missing_count\(([\w\-\"']+)\)\s*([<>=!]+)\s*(\d+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            return (
                "null_check",
                f'SELECT COUNT(*) FROM {table_name} WHERE "{col_name}" IS NULL',
                f"{operator} {value}"
            )

    # 3. invalid_count (enum/categorical/regex)
    if "invalid_count" in check_str:
        match = re.match(r"invalid_count\(([\w\-\"']+)\)\s*([<>=!]+)\s*(\d+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="

            valid_regex = extra.get("valid regex") if isinstance(extra, dict) else None
            if valid_values:
                escaped = [str(v).replace("'", "''") for v in valid_values]
                in_list = ", ".join(f"'{v}'" for v in escaped)
                sql = (
                    f'SELECT COUNT(*) FROM {table_name} '
                    f'WHERE "{col_name}" IS NOT NULL AND "{col_name}" NOT IN ({in_list})'
                )
            elif valid_regex:
                sql = (
                    f'SELECT COUNT(*) FROM {table_name} '
                    f'WHERE "{col_name}" IS NOT NULL AND NOT regexp_matches("{col_name}", \'{valid_regex}\')'
                )
            else:
                sql = f'SELECT COUNT(*) FROM {table_name} WHERE 1=0'

            return ("invalid_count", sql, f"{operator} {value}")

    # 4. duplicate_count
    if "duplicate_count" in check_str:
        match = re.match(r"duplicate_count\(([\w\-\"',\s]+)\)\s*([<>=!]+)\s*(\d+)", check_str)
        if match:
            cols_str, operator, value = match.groups()
            cols = [c.strip().strip('"\'') for c in cols_str.split(',')]
            cols_quoted = ', '.join([f'"{c}"' for c in cols])
            null_filter = " AND ".join([f'"{c}" IS NOT NULL' for c in cols])
            where_clause = f"WHERE {null_filter}" if null_filter else ""
            if operator == "==":
                operator = "="
            return (
                "duplicate_count",
                f"SELECT COUNT(*) - COUNT(DISTINCT ({cols_quoted})) FROM {table_name} {where_clause}",
                f"{operator} {value}"
            )

    # 5. min_length / max_length
    if "min_length" in check_str:
        match = re.match(r"min_length\(([\w\-\"']+)\)\s*([<>=!]+)\s*(\d+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            return (
                "min_length",
                f'SELECT MIN(LENGTH(CAST("{col_name}" AS VARCHAR))) FROM {table_name}',
                f"{operator} {value}"
            )

    if "max_length" in check_str:
        match = re.match(r"max_length\(([\w\-\"']+)\)\s*([<>=!]+)\s*(\d+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            return (
                "max_length",
                f'SELECT MAX(LENGTH(CAST("{col_name}" AS VARCHAR))) FROM {table_name}',
                f"{operator} {value}"
            )

    # 6. avg check
    if re.search(r"avg\(", check_str):
        match = re.match(r"avg\(([\w\-\"']+)\)\s*([<>=!]+)\s*([\w\-\.: ]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            return (
                "avg_check",
                f'SELECT AVG("{col_name}") FROM {table_name}',
                f"{operator} {value}"
            )

    # 7. between check
    if re.search(r"between\(", check_str):
        match = re.match(r"between\(([\w\-\"']+)\)\s*([<>=!]+)\s*([\w\-\.: ]+)\s+and\s+([\w\-\.: ]+)", check_str, re.IGNORECASE)
        if match:
            col_name, operator, low, high = match.groups()
            col_name = col_name.strip('"\'')
            return (
                "between_check",
                f'SELECT COUNT(*) FROM {table_name} WHERE "{col_name}" < {low} OR "{col_name}" > {high}',
                "= 0"
            )

    # 8. freshness check
    if re.search(r"freshness\(", check_str):
        match = re.match(r"freshness\(([\w\-\"']+)\)\s*([<>=!]+)\s*(\d+)\s*(d|h|m)?", check_str, re.IGNORECASE)
        if match:
            col_name, operator, value, unit = match.groups()
            col_name = col_name.strip('"\'')
            unit = (unit or "d").lower()
            interval_map = {"d": "DAY", "h": "HOUR", "m": "MINUTE"}
            interval = interval_map.get(unit, "DAY")
            if operator == "==":
                operator = "="
            return (
                "freshness_check",
                f'SELECT COUNT(*) FROM {table_name} WHERE TRY_CAST("{col_name}" AS DATE) < CURRENT_DATE - INTERVAL {value} {interval}',
                "= 0"
            )

    # 9. missing_percent check
    if "missing_percent" in check_str:
        match = re.match(r"missing_percent\(([\w\-\"']+)\)\s*([<>=!]+)\s*([\w\-\.]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            return (
                "null_percent_check",
                f'SELECT ROUND(COUNT(*) FILTER (WHERE "{col_name}" IS NULL) * 100.0 / COUNT(*), 2) FROM {table_name}',
                f"{operator} {value}"
            )

    # 10. uniqueness_ratio check
    if "uniqueness_ratio" in check_str:
        match = re.match(r"uniqueness_ratio\(([\w\-\"']+)\)\s*([<>=!]+)\s*([\w\-\.]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            return (
                "uniqueness_ratio_check",
                f'SELECT ROUND(COUNT(DISTINCT "{col_name}") * 1.0 / COUNT(*), 4) FROM {table_name}',
                f"{operator} {value}"
            )

    # 11. min/max (numeric or date range)
    if re.search(r"min\(", check_str):
        match = re.match(r"min\(([\w\-\"']+)\)\s*([<>=!]+)\s*([\w\-\.: ]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            is_date_check = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()))
            col_expr = f'TRY_CAST("{col_name}" AS DATE)' if is_date_check else f'"{col_name}"'
            return (
                "min_check",
                f'SELECT MIN({col_expr}) FROM {table_name}',
                f"{operator} {value}"
            )

    if re.search(r"max\(", check_str):
        match = re.match(r"max\(([\w\-\"']+)\)\s*([<>=!]+)\s*([\w\-\.: ]+)", check_str)
        if match:
            col_name, operator, value = match.groups()
            col_name = col_name.strip('"\'')
            if operator == "==":
                operator = "="
            is_date_check = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()))
            col_expr = f'TRY_CAST("{col_name}" AS DATE)' if is_date_check else f'"{col_name}"'
            return (
                "max_check",
                f'SELECT MAX({col_expr}) FROM {table_name}',
                f"{operator} {value}"
            )

    # 12. failed rows
    if check_str == "failed rows":
        fail_query = extra.get("fail query", "")
        if fail_query:
            fail_query = fail_query.strip()
            fail_query = re.sub(
                r"\bFROM\s+dataset\b",
                f"FROM {table_name}",
                fail_query,
                flags=re.IGNORECASE,
            )
            fail_query = _normalize_duckdb_regex(fail_query)
            fail_query = _normalize_duckdb_dates(fail_query)
            return (
                "failed_rows",
                fail_query,
                "= 0"
            )

    return ("unknown", "", check_str)


def _normalize_duckdb_regex(sql: str) -> str:
    """
    Best-effort cleanup for regex expressions.
    The prompt should already generate DuckDB-safe regex syntax.
    """
    return sql


def _normalize_duckdb_dates(sql: str) -> str:
    """Normalize common date function spellings to DuckDB syntax."""
    if not sql:
        return sql
    sql = re.sub(r"\bDATEDIFF\s*\(\s*year\s*,",  "date_diff('year',",  sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDATEDIFF\s*\(\s*month\s*,", "date_diff('month',", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDATEDIFF\s*\(\s*day\s*,",   "date_diff('day',",   sql, flags=re.IGNORECASE)
    return sql


def execute_checks(db_file: str, table_name: str, checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Execute all checks against DuckDB and return results.
    table_name is the fully qualified name (e.g. parag.orders).
    """
    conn = duckdb.connect(db_file, read_only=True)

    results = []
    passed_count = 0
    failed_count = 0

    for check in checks:
        check_id   = check["check_id"]
        check_name = check["check_name"]
        check_type = check["type"]
        sql_query  = check["query"]
        expected   = check["expected"]

        try:
            logger.info(
                f"[soda] executing check | id={check_id} name={check_name} type={check_type} "
                f"sql={sql_query} expected={expected}"
            )
            cursor      = conn.execute(sql_query)
            result      = cursor.fetchall()
            description = cursor.description

            if check_type == "failed_rows":
                actual_value = len(result) if result else 0
                passed       = actual_value == 0
                failed_rows  = _rows_to_dicts(description, result, limit=3)

                if not passed:
                    error_msg = f"Found {actual_value} rows violating rule:\n"
                    for i, row in enumerate(failed_rows):
                        error_msg += f"  Row {i+1}: {row}\n"
                    if actual_value > len(failed_rows):
                        error_msg += f"  ... and {actual_value - len(failed_rows)} more rows"
                else:
                    error_msg = None
                logger.info(
                    f"[soda] failed_rows result | id={check_id} rows={actual_value} "
                    f"status={'PASS' if passed else 'FAIL'}"
                )
            else:
                actual_value = result[0][0] if result else None
                passed, error_msg = evaluate_check(actual_value, expected, check_type)
                logger.info(
                    f"[soda] check result | id={check_id} actual={actual_value} "
                    f"expected={expected} status={'PASS' if passed else 'FAIL'}"
                )

            if passed:
                passed_count += 1
            else:
                failed_count += 1

            results.append({
                "check_id":     check_id,
                "check_name":   check_name,
                "type":         check_type,
                "passed":       passed,
                "actual_value": make_serializable(actual_value),
                "expected":     expected,
                "error_message": error_msg,
                "failed_rows":  make_serializable(failed_rows) if check_type == "failed_rows" else [],
            })

        except Exception as e:
            failed_count += 1
            logger.error(
                f"[soda] check execution failed | id={check_id} name={check_name} "
                f"type={check_type} sql={sql_query} error={e}",
                exc_info=True,
            )
            results.append({
                "check_id":     check_id,
                "check_name":   check_name,
                "type":         check_type,
                "passed":       False,
                "actual_value": None,
                "expected":     expected,
                "error_message": f"Query failed: {str(e)}",
                "failed_rows":  [],
            })

    conn.close()

    return make_serializable({
        "audit_passed":  failed_count == 0,
        "total_checks":  len(checks),
        "passed_checks": passed_count,
        "failed_checks": failed_count,
        "checks":        results
    })


def evaluate_check(actual_value: Any, expected_str: str, check_type: str) -> Tuple[bool, str]:
    """Evaluate if actual value matches the expected condition."""

    if actual_value is None:
        return False, "Actual value is NULL"

    expected_raw = expected_str.strip()
    match = re.match(r"([<>=]+)\s*([\w\-\.: ]+)", expected_raw)
    if not match:
        return False, f"Could not parse expected: {expected_str}"

    operator, expected_value = match.groups()
    expected_value = expected_value.strip().lower()

    if expected_value in {"today", "current_date"}:
        expected_value = date.today().isoformat()

    if re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?$", str(expected_value)):
        actual_str        = actual_value.isoformat() if isinstance(actual_value, (date, datetime)) else str(actual_value)
        expected_str_norm = str(expected_value)
        if operator == ">":
            passed = actual_str > expected_str_norm
        elif operator == "<":
            passed = actual_str < expected_str_norm
        elif operator == ">=":
            passed = actual_str >= expected_str_norm
        elif operator == "<=":
            passed = actual_str <= expected_str_norm
        elif operator == "=":
            passed = actual_str == expected_str_norm
        elif operator == "!=":
            passed = actual_str != expected_str_norm
        else:
            return False, f"Unknown operator: {operator}"

        if not passed:
            return False, f"Expected {expected_str}, but actual is {actual_str}"
        return True, None

    try:
        expected_value = float(expected_value) if '.' in expected_value else int(expected_value)
        actual_value   = float(actual_value)
    except Exception:
        return False, f"Could not convert to number: actual={actual_value}, expected={expected_value}"

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


def run_soda_checks_from_yaml(
    db_file: str,
    yaml_path: str,
    table_name: str,
    bare_table: str = None
) -> Dict[str, Any]:
    """
    End-to-end: load YAML → parse checks → execute → return results.

    table_name : fully qualified name used in SQL (e.g. parag.orders)
    bare_table : bare name used in YAML key matching (e.g. orders)
                 If not provided, falls back to table_name.
    """
    yaml_content = load_soda_yaml(yaml_path)

    # YAML keys use bare table name: "checks for orders"
    # SQL queries use fully qualified name: parag.orders
    yaml_table = bare_table or table_name
    checks     = parse_soda_checks(yaml_content, yaml_table, full_table_name=table_name)

    if not checks:
        return {
            "audit_passed": False,
            "error": f"No checks found in {yaml_path} for table {yaml_table}",
            "total_checks": 0,
            "passed_checks": 0,
            "failed_checks": 0,
            "checks": []
        }

    # Execute SQL against fully qualified table
    results = execute_checks(db_file, table_name, checks)

    return results