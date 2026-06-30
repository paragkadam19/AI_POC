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
CREATE TABLE AS SELECT pass: created_date, is_active, file_name.
"""
import os
import duckdb
from datetime import date

DUCKDB_CONFIG = {
    "memory_limit":              "2GB",
    "max_memory":                "2GB",
    "temp_directory":            "/tmp",
    "max_temp_directory_size":   "10GB",
    "threads":                   4,
    "preserve_insertion_order":  False,
}

SAMPLE_ROWS_SCHEMA = 30

LOW_CARDINALITY_THRESHOLD = 20


def _connect(db_file: str, read_only: bool = False):
    return duckdb.connect(db_file, read_only=read_only, config=DUCKDB_CONFIG)


# ── Ingest ─────────────────────────────────────────────────────────────────────
def ingest_csv(csv_file: str, db_file: str, table: str, original_filename: str = None) -> dict:
    """
    Ingest CSV into DuckDB, adding 3 audit/control columns to every table
    in the same single CREATE TABLE AS SELECT pass (no extra scan needed):

      - created_date : today's date, set at ingest time
      - is_active    : boolean, true for every row by default
      - file_name    : the original uploaded filename
    """
    try:
        file_size_bytes = os.path.getsize(csv_file)
        conn = duckdb.connect(db_file, config=DUCKDB_CONFIG)

        conn.execute(f"DROP TABLE IF EXISTS {table}")

        today     = date.today().isoformat()
        file_name = (original_filename or os.path.basename(csv_file)).replace("'", "''")

        conn.execute(f"""
            CREATE TABLE {table} AS
            SELECT
                *,
                DATE '{today}'  AS created_date,
                true            AS is_active,
                '{file_name}'   AS file_name
            FROM read_csv_auto('{csv_file}', sample_size=10000, nullstr='')
        """)

        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        cols      = conn.execute(f"DESCRIBE {table}").fetchall()
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


# ── Preview (8 rows for UI table) ─────────────────────────────────────────────
def get_preview(db_file: str, table: str, n: int = 8, row_count: int = None) -> tuple:
    conn = _connect(db_file, read_only=True)
    cols = [c[0] for c in conn.execute(f"DESCRIBE {table}").fetchall()]
    rows = conn.execute(f"SELECT * FROM {table} LIMIT {n}").fetchall()
    if row_count is None:
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return [dict(zip(cols, row)) for row in rows], cols, row_count


# ── Full metadata via SUMMARIZE + single-pass value-count extraction ─────────
def get_metadata(db_file: str, table: str) -> dict:
    """
    SUMMARIZE: one query, full-dataset column stats, single pass.

    For low-cardinality columns, value counts (e.g. {"Yes": 1200,
    "No": 340}) are fetched using DuckDB's histogram() aggregate in
    ONE combined query across all qualifying columns — ONE full-table
    scan, not one scan per column.
    """
    conn = _connect(db_file, read_only=True)

    row_count  = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    total_cols = len(conn.execute(f"DESCRIBE {table}").fetchall())

    cur          = conn.execute(f"SUMMARIZE {table}")
    summary_cols = [d[0] for d in cur.description]
    summary_rows = cur.fetchall()

    parsed     = []
    candidates = []
    for row in summary_rows:
        r            = dict(zip(summary_cols, row))
        unique_count = r.get("approx_unique")
        col_name     = r["column_name"]
        parsed.append((r, unique_count, col_name))
        if unique_count and unique_count < LOW_CARDINALITY_THRESHOLD and unique_count < row_count:
            candidates.append(col_name)

    value_counts_map = {}
    if candidates:
        select_parts = ", ".join(
            f'histogram("{c}") AS "{c}__hist"' for c in candidates
        )
        try:
            combined = conn.execute(f"SELECT {select_parts} FROM {table}").fetchone()
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
def get_sample_rows(db_file: str, table: str, n_rows: int, row_count: int = None) -> list:
    conn = _connect(db_file, read_only=True)
    if row_count is None:
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    if row_count <= n_rows:
        cur = conn.execute(f"SELECT * FROM {table}")
    else:
        cur = conn.execute(f"SELECT * FROM {table} USING SAMPLE {n_rows} ROWS")

    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


# ── Stratified sample as CSV string ───────────────────────────────────────────
def get_sample_csv(db_file: str, table: str, n_rows: int, row_count: int = None) -> str:
    conn = _connect(db_file, read_only=True)
    if row_count is None:
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    if row_count <= n_rows:
        cur = conn.execute(f"SELECT * FROM {table}")
    else:
        cur = conn.execute(f"SELECT * FROM {table} USING SAMPLE {n_rows} ROWS")

    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()

    lines = [",".join(str(c) for c in cols)]
    for row in rows:
        lines.append(",".join(
            "" if v is None else str(v).replace(",", ";")
            for v in row
        ))
    return "\n".join(lines)


# ── Combined metadata, shaped for prompt_builder.py ────────────────────────────
def get_full_metadata_for_ai(db_file: str, table: str, filename: str,
                              file_size_bytes: int,
                              n_sample: int = SAMPLE_ROWS_SCHEMA) -> dict:
    metadata    = get_metadata(db_file, table)
    sample_rows = get_sample_rows(db_file, table, n_sample, row_count=metadata["total_rows"])
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