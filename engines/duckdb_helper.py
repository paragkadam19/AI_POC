"""
duckdb_helper.py — All metadata + sampling via DuckDB only
============================================================
Multi-tenancy: all functions accept an optional `schema` parameter.
A shared _connect() helper sets search_path on every connection so
unqualified table references resolve into the correct user schema.
"""
import os
import duckdb
from datetime import date

DUCKDB_CONFIG = {
    "memory_limit":              "3GB",
    "max_memory":                "3GB",
    "temp_directory":            "/tmp",
    "max_temp_directory_size":   "20GB",
    "threads":                   4,
    "preserve_insertion_order":  False,
    "hnsw_enable_experimental_persistence": True,
}

SAMPLE_ROWS_SCHEMA         = 10
LOW_CARDINALITY_THRESHOLD  = 10
CSV_AUTODETECT_SAMPLE_SIZE = 500


def _connect(db_file: str, read_only: bool = False, schema: str = None):
    """
    Open a DuckDB connection and optionally set search_path.
    All code in this project should use this helper so that
    unqualified table names resolve into the correct user schema.
    """
    conn = duckdb.connect(db_file, read_only=read_only, config=DUCKDB_CONFIG)
    if schema:
        try:
            conn.execute(f"SET search_path = '{schema}'")
        except Exception:
            pass
    return conn


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _qualified(schema: str, table: str) -> str:
    """Return schema.quoted_table or just quoted_table if no schema."""
    if schema:
        return f"{schema}.{_quote_ident(table)}"
    return _quote_ident(table)


# ── Ingest ─────────────────────────────────────────────────────────────────────
def ingest_csv(csv_file: str, db_file: str, table: str,
               original_filename: str = None, schema: str = None) -> dict:
    """
    Ingest CSV into DuckDB under {schema}.{table}.
    Adds 3 audit columns: system_date, system_active, file_path.
    """
    try:
        file_size_bytes = os.path.getsize(csv_file)
        conn = _connect(db_file, schema=schema)

        if schema:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

        qtable = _qualified(schema, table)
        conn.execute(f"DROP TABLE IF EXISTS {qtable}")

        today           = date.today().isoformat()
        local_file_path = os.path.abspath(csv_file).replace("'", "''")

        conn.execute(f"""
            CREATE TABLE {qtable} AS
            SELECT
                *,
                DATE '{today}'      AS system_date,
                true                AS system_active,
                '{local_file_path}' AS file_path
            FROM read_csv_auto('{csv_file}', sample_size={CSV_AUTODETECT_SAMPLE_SIZE}, nullstr='')
        """)

        row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
        cols      = conn.execute(f"DESCRIBE {qtable}").fetchall()
        conn.close()

        return {
            "success":      True,
            "table":        table,
            "row_count":    row_count,
            "columns":      [{"name": c[0], "type": c[1]} for c in cols],
            "col_names":    [c[0] for c in cols],
            "file_size_mb": round(file_size_bytes / (1024 * 1024), 2),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def write_table_schema_snapshot(db_file: str, table: str, schema: str = None) -> dict:
    """
    Save actual DuckDB schema for a table into {schema}.table_schema_snapshot.
    """
    try:
        conn = _connect(db_file, schema=schema)

        if schema:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

        snapshot_table = f"{schema}.table_schema_snapshot" if schema else "table_schema_snapshot"

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {snapshot_table} (
                id          TEXT PRIMARY KEY,
                table_name  TEXT,
                column_name TEXT,
                data_type   TEXT,
                nullable    BOOLEAN,
                created_at  TEXT
            )
        """)

        qtable = _qualified(schema, table)
        conn.execute(f"DELETE FROM {snapshot_table} WHERE table_name = ?", [table])
        rows = conn.execute(f"DESCRIBE {qtable}").fetchall()
        now  = date.today().isoformat()
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO {snapshot_table}
                (id, table_name, column_name, data_type, nullable, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [f"{table}:{row[0]}", table, row[0], row[1], True, now],
            )
        conn.close()
        return {"success": True, "rows_written": len(rows)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Preview ────────────────────────────────────────────────────────────────────
def get_preview(db_file: str, table: str, n: int = 8,
                row_count: int = None, schema: str = None) -> tuple:
    conn   = _connect(db_file, read_only=True, schema=schema)
    qtable = _qualified(schema, table)
    try:
        cols = [c[0] for c in conn.execute(f"DESCRIBE {qtable}").fetchall()]
        rows = conn.execute(f"SELECT * FROM {qtable} LIMIT {n}").fetchall()
        if row_count is None:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
        conn.close()
        return [dict(zip(cols, row)) for row in rows], cols, row_count
    except Exception:
        try:
            cols        = [c[0] for c in conn.execute(f"DESCRIBE {qtable}").fetchall()]
            safe_select = ", ".join([f'CAST("{c}" AS VARCHAR) AS "{c}"' for c in cols])
            rows        = conn.execute(f"SELECT {safe_select} FROM {qtable} LIMIT {n}").fetchall()
            if row_count is None:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
            return [dict(zip(cols, row)) for row in rows], cols, row_count
        finally:
            conn.close()


# ── Full metadata ──────────────────────────────────────────────────────────────
def get_metadata(db_file: str, table: str, schema: str = None) -> dict:
    conn   = _connect(db_file, read_only=True, schema=schema)
    qtable = _qualified(schema, table)

    row_count     = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
    describe_rows = conn.execute(f"DESCRIBE {qtable}").fetchall()
    total_cols    = len(describe_rows)
    col_names     = [c[0] for c in describe_rows]

    cur          = conn.execute(f"SUMMARIZE {qtable}")
    summary_cols = [d[0] for d in cur.description]
    summary_rows = cur.fetchall()

    exact_distinct_map = {}
    if col_names:
        distinct_select = ", ".join(
            f'COUNT(DISTINCT "{c}") AS "{c}__distinct"' for c in col_names
        )
        try:
            distinct_row       = conn.execute(f"SELECT {distinct_select} FROM {qtable}").fetchone()
            exact_distinct_map = {
                c: int(distinct_row[i]) if distinct_row[i] is not None else 0
                for i, c in enumerate(col_names)
            }
        except Exception as e:
            print(f"  [warning] exact distinct count query failed: {e}")

    parsed     = []
    candidates = []
    for row in summary_rows:
        r            = dict(zip(summary_cols, row))
        col_name     = r["column_name"]
        unique_count = exact_distinct_map.get(col_name, r.get("approx_unique"))
        parsed.append((r, unique_count, col_name))
        if unique_count and unique_count < LOW_CARDINALITY_THRESHOLD and unique_count < row_count:
            candidates.append(col_name)

    value_counts_map = {}
    if candidates:
        select_parts = ", ".join(
            f'histogram("{c}") AS "{c}__hist"' for c in candidates
        )
        try:
            combined = conn.execute(f"SELECT {select_parts} FROM {qtable}").fetchone()
            for i, c in enumerate(candidates):
                hist         = combined[i] or {}
                sorted_items = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)
                value_counts_map[c] = {
                    str(val): int(cnt) for val, cnt in sorted_items[:LOW_CARDINALITY_THRESHOLD]
                }
        except Exception as e:
            print(f"  [warning] combined histogram fetch failed: {e}")

    conn.close()

    columns = []
    for r, unique_count, col_name in parsed:
        null_pct   = float(r.get("null_percentage") or 0)
        null_count = int(round(null_pct / 100 * row_count))
        col_dict   = {
            "name":         col_name,
            "dtype":        r["column_type"],
            "null_count":   null_count,
            "null_pct":     round(null_pct, 2),
            "unique_count": unique_count,
            "min":          str(r.get("min", "") or ""),
            "max":          str(r.get("max", "") or ""),
            "mean":         str(r.get("avg", "") or ""),
        }
        if col_name in value_counts_map:
            col_dict["value_counts"] = value_counts_map[col_name]
        columns.append(col_dict)

    return {
        "total_rows":    row_count,
        "total_columns": total_cols,
        "columns":       columns,
    }


# ── Sample rows ────────────────────────────────────────────────────────────────
def get_sample_rows(db_file: str, table: str, n_rows: int,
                    row_count: int = None, schema: str = None) -> list:
    conn   = _connect(db_file, read_only=True, schema=schema)
    qtable = _qualified(schema, table)
    if row_count is None:
        row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
    if row_count <= n_rows:
        cur = conn.execute(f"SELECT * FROM {qtable}")
    else:
        cur = conn.execute(f"SELECT * FROM {qtable} USING SAMPLE {n_rows} ROWS")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


# ── Combined metadata for AI ───────────────────────────────────────────────────
def get_full_metadata_for_ai(db_file: str, table: str, filename: str,
                              file_size_bytes: int,
                              n_sample: int = SAMPLE_ROWS_SCHEMA,
                              schema: str = None) -> dict:
    metadata    = get_metadata(db_file, table, schema=schema)
    sample_rows = get_sample_rows(db_file, table, n_sample,
                                  row_count=metadata["total_rows"], schema=schema)
    schema_dict = {c["name"]: c["dtype"] for c in metadata["columns"]}

    return {
        "filename":        filename,
        "file_size_bytes": file_size_bytes,
        "file_size_kb":    round(file_size_bytes / 1024, 2),
        "file_size_mb":    round(file_size_bytes / (1024 * 1024), 2),
        "total_rows":      metadata["total_rows"],
        "total_columns":   metadata["total_columns"],
        "schema":          schema_dict,
        "columns":         metadata["columns"],
        "sample_rows":     sample_rows,
        "sample_size":     n_sample,
    }