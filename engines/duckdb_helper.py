"""
duckdb_helper.py — All metadata + sampling via DuckDB only
============================================================
No Polars. No pandas. No full file in memory.
DuckDB reads the data on disk, returns only what we need.

Memory/thread config is back to the fixed, manually-tuned settings
that gave the best confirmed results (16.56s disk-write / 20.82s
ingest on a 4.5GB file) — adaptive sizing was tried and reverted.

temp_directory stays pointed at /tmp — confirmed to be on the local
SSD, not network storage, so spilling there is fast.

Low-cardinality columns get value_counts (not just distinct values)
via DuckDB's histogram() aggregate, in a single combined query/scan.

Every ingested table gets 3 audit columns added in the same single
CREATE TABLE AS SELECT pass: system_date, system_active, file_path.

Multi-tenancy
-------------
Every function below takes an optional `schema_name`. When given, the
connection it opens runs `SET search_path = '<schema>'` (and, for
write connections, `CREATE SCHEMA IF NOT EXISTS <schema>` first) so
every unqualified table reference — read or write — resolves into
that user's own schema. This mirrors the `get_user_conn()` pattern in
app.py. `schema_name=None` preserves the old default-schema behavior,
so existing callers that don't pass it keep working.
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

SAMPLE_ROWS_SCHEMA = 10

LOW_CARDINALITY_THRESHOLD = 10
CSV_AUTODETECT_SAMPLE_SIZE = 500


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _connect(db_file: str, read_only: bool = False, schema_name: str = None):
    """
    Open a DuckDB connection, optionally scoped to a user's schema.

    When schema_name is given:
      - write connections first CREATE SCHEMA IF NOT EXISTS for it
      - SET search_path is issued so unqualified CREATE TABLE / SELECT /
        DESCRIBE statements in the caller resolve into that schema first

    schema_name=None keeps the previous behavior (default/main schema),
    so callers that haven't been updated yet don't break.
    """
    conn = duckdb.connect(db_file, read_only=read_only, config=DUCKDB_CONFIG)
    if schema_name:
        if not read_only:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema_name)}")
        conn.execute(f"SET search_path = '{schema_name}'")
    return conn


# ── Ingest ─────────────────────────────────────────────────────────────────────
def ingest_csv(csv_file: str, db_file: str, table: str, original_filename: str = None,
                schema_name: str = None) -> dict:
    """
    Ingest CSV into DuckDB, adding 3 audit/control columns to every table
    in the same single CREATE TABLE AS SELECT pass (no extra scan needed):

      - system_date   : today's date, set at ingest time
      - system_active : boolean, true for every row by default
      - file_path  : the local file path of the uploaded CSV

    When schema_name is given, the table is created inside that user's
    schema (created if it doesn't exist yet) rather than the default schema.
    """
    try:
        file_size_bytes = os.path.getsize(csv_file)
        conn = _connect(db_file, read_only=False, schema_name=schema_name)

        qtable = _quote_ident(table)
        conn.execute(f"DROP TABLE IF EXISTS {qtable}")

        today = date.today().isoformat()
        local_file_path = os.path.abspath(csv_file).replace("'", "''")

        conn.execute(f"""
            CREATE TABLE {qtable} AS
            SELECT
                *,
                DATE '{today}'  AS system_date,
                true            AS system_active,
                '{local_file_path}' AS file_path
            FROM read_csv_auto('{csv_file}', sample_size={CSV_AUTODETECT_SAMPLE_SIZE}, nullstr='')
        """)

        row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
        cols      = conn.execute(f"DESCRIBE {qtable}").fetchall()
        conn.close()

        return {
            "success":      True,
            "table":        table,
            "schema":       schema_name,
            "row_count":    row_count,
            "columns":      [{"name": c[0], "type": c[1]} for c in cols],
            "col_names":    [c[0] for c in cols],
            "file_size_mb": round(file_size_bytes / (1024 * 1024), 2),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def write_table_schema_snapshot(db_file: str, table: str, schema_name: str = None) -> dict:
    """
    Save the actual DuckDB schema for a table into a durable snapshot table.
    This uses the real database types, not AI-generated metadata.

    With schema_name set, table_schema_snapshot itself lives inside that
    user's schema (via search_path), so each user gets their own snapshot
    table rather than sharing one global table keyed only by table_name.
    """
    try:
        conn = _connect(db_file, read_only=False, schema_name=schema_name)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS table_schema_snapshot (
                id TEXT PRIMARY KEY,
                table_name TEXT,
                column_name TEXT,
                data_type TEXT,
                nullable BOOLEAN,
                created_at TEXT
            )
        """)

        conn.execute("DELETE FROM table_schema_snapshot WHERE table_name = ?", [table])
        rows = conn.execute(f"DESCRIBE {_quote_ident(table)}").fetchall()
        now = date.today().isoformat()
        for idx, row in enumerate(rows, start=1):
            conn.execute(
                """
                INSERT INTO table_schema_snapshot
                (id, table_name, column_name, data_type, nullable, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    f"{table}:{row[0]}",
                    table,
                    row[0],
                    row[1],
                    True,
                    now,
                ],
            )
        conn.close()
        return {"success": True, "rows_written": len(rows)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Preview (8 rows for UI table) ─────────────────────────────────────────────
def get_preview(db_file: str, table: str, n: int = 8, row_count: int = None,
                 schema_name: str = None) -> tuple:
    conn = _connect(db_file, read_only=True, schema_name=schema_name)
    try:
        qtable = _quote_ident(table)
        cols = [c[0] for c in conn.execute(f"DESCRIBE {qtable}").fetchall()]
        rows = conn.execute(f"SELECT * FROM {qtable} LIMIT {n}").fetchall()
        if row_count is None:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
        conn.close()
        return [dict(zip(cols, row)) for row in rows], cols, row_count
    except Exception:
        # Fallback: cast everything to VARCHAR so preview does not fail on
        # timezone-aware timestamps or other Python conversion issues.
        try:
            qtable = _quote_ident(table)
            cols = [c[0] for c in conn.execute(f"DESCRIBE {qtable}").fetchall()]
            safe_select = ", ".join([f'CAST("{c}" AS VARCHAR) AS "{c}"' for c in cols])
            rows = conn.execute(f"SELECT {safe_select} FROM {qtable} LIMIT {n}").fetchall()
            if row_count is None:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
            return [dict(zip(cols, row)) for row in rows], cols, row_count
        finally:
            conn.close()


# ── Full metadata via SUMMARIZE + single-pass value-count extraction ─────────
def get_metadata(db_file: str, table: str, schema_name: str = None) -> dict:
    """
    SUMMARIZE: one query for null/range stats, plus exact distinct counts.

    For low-cardinality columns, value counts (e.g. {"Yes": 1200,
    "No": 340}) are fetched using DuckDB's histogram() aggregate in
    ONE combined query across all qualifying columns — ONE full-table
    scan, not one scan per column.
    """
    conn = _connect(db_file, read_only=True, schema_name=schema_name)

    qtable = _quote_ident(table)
    row_count  = conn.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]
    describe_rows = conn.execute(f"DESCRIBE {qtable}").fetchall()
    total_cols = len(describe_rows)
    col_names = [c[0] for c in describe_rows]

    cur          = conn.execute(f"SUMMARIZE {qtable}")
    summary_cols = [d[0] for d in cur.description]
    summary_rows = cur.fetchall()

    exact_distinct_map = {}
    if col_names:
        distinct_select = ", ".join(
            f'COUNT(DISTINCT "{c}") AS "{c}__distinct"' for c in col_names
        )
        try:
            distinct_row = conn.execute(f"SELECT {distinct_select} FROM {qtable}").fetchone()
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
                hist = combined[i] or {}
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

        col_dict = {
            "name":          col_name,
            "dtype":         r["column_type"],
            "null_count":    null_count,
            "null_pct":      round(null_pct, 2),
            "unique_count":  unique_count,
            "min":           str(r.get("min", "") or ""),
            "max":           str(r.get("max", "") or ""),
            "mean":          str(r.get("avg", "") or ""),
        }
        if col_name in value_counts_map:
            col_dict["value_counts"] = value_counts_map[col_name]

        columns.append(col_dict)

    return {
        "total_rows":    row_count,
        "total_columns": total_cols,
        "columns":       columns,
    }


# ── Sample rows as list[dict] ──────────────────────────────────────────────────
def get_sample_rows(db_file: str, table: str, n_rows: int, row_count: int = None,
                     schema_name: str = None) -> list:
    conn = _connect(db_file, read_only=True, schema_name=schema_name)
    qtable = _quote_ident(table)  # was unquoted previously; quoting for consistency
                                   # with the rest of the module and to avoid
                                   # breaking on reserved-word/odd table names
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


# ── Combined metadata, shaped for prompt_builder.py ────────────────────────────
def get_full_metadata_for_ai(db_file: str, table: str, filename: str,
                              file_size_bytes: int,
                              n_sample: int = SAMPLE_ROWS_SCHEMA,
                              schema_name: str = None) -> dict:
    metadata    = get_metadata(db_file, table, schema_name=schema_name)
    sample_rows = get_sample_rows(
        db_file, table, n_sample,
        row_count=metadata["total_rows"], schema_name=schema_name,
    )
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
