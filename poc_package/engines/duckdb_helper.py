"""
duckdb_helper.py — All metadata + sampling via DuckDB only
============================================================
No Polars. No pandas. No full file in memory.
DuckDB reads the data on disk, returns only what we need.

OPTIMIZED: distinct values for ALL low-cardinality columns are now
fetched in ONE combined query/scan instead of one query per column.
row_count is computed once and threaded through, instead of being
recomputed by every helper that needs it.
"""
import duckdb

DUCKDB_CONFIG = {
    "memory_limit":              "2GB",
    "max_memory":                "2GB",
    "temp_directory":            "/tmp",
    "max_temp_directory_size":   "10GB",
    "threads":                   6,
    "preserve_insertion_order":  False,
}

SAMPLE_ROWS_SCHEMA = 500
SAMPLE_ROWS_DQ     = 500

LOW_CARDINALITY_THRESHOLD = 100


def _connect(db_file: str, read_only: bool = False):
    return duckdb.connect(db_file, read_only=read_only, config=DUCKDB_CONFIG)


# ── Ingest ─────────────────────────────────────────────────────────────────────
def ingest_csv(csv_file: str, db_file: str, table: str) -> dict:
    try:
        conn = _connect(db_file)
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"""
            CREATE TABLE {table} AS
            SELECT * FROM read_csv_auto('{csv_file}', sample_size=10000)
        """)
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        cols      = conn.execute(f"DESCRIBE {table}").fetchall()
        conn.close()

        return {
            "success":   True,
            "table":     table,
            "row_count": row_count,
            "columns":   [{"name": c[0], "type": c[1]} for c in cols],
            "col_names": [c[0] for c in cols],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Preview (8 rows for UI table) ─────────────────────────────────────────────
def get_preview(db_file: str, table: str, n: int = 8, row_count: int = None) -> tuple:
    """
    8 rows for UI preview. row_count can be passed in (e.g. from ingest_csv's
    result) to skip a redundant COUNT(*) scan.
    """
    conn = _connect(db_file, read_only=True)
    cols = [c[0] for c in conn.execute(f"DESCRIBE {table}").fetchall()]
    rows = conn.execute(f"SELECT * FROM {table} LIMIT {n}").fetchall()
    if row_count is None:
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return [dict(zip(cols, row)) for row in rows], cols, row_count


# ── Full metadata via SUMMARIZE + single-pass low-cardinality extraction ──────
def get_metadata(db_file: str, table: str) -> dict:
    """
    SUMMARIZE: one query, full-dataset column stats, single pass.

    Low-cardinality distinct values used to be fetched with a NEW connection
    and a NEW full-table scan PER column. Now it's ONE combined query that
    fetches distinct values for every qualifying column in a single scan,
    using DuckDB's list(DISTINCT col) aggregate.
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

    distinct_map = {}
    if candidates:
        select_parts = ", ".join(
            f'list(DISTINCT "{c}") AS "{c}__vals"' for c in candidates
        )
        try:
            combined = conn.execute(f"SELECT {select_parts} FROM {table}").fetchone()
            for i, c in enumerate(candidates):
                vals = combined[i] or []
                distinct_map[c] = sorted(str(v) for v in vals)[:LOW_CARDINALITY_THRESHOLD]
        except Exception as e:
            print(f"  [warning] combined distinct-value fetch failed: {e}")

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
        if col_name in distinct_map:
            col_dict["distinct_values"] = distinct_map[col_name]

        columns.append(col_dict)

    return {
        "total_rows":    row_count,
        "total_columns": total_cols,
        "columns":       columns,
    }


# ── Sample rows as list[dict] ──────────────────────────────────────────────────
def get_sample_rows(db_file: str, table: str, n_rows: int, row_count: int = None) -> list:
    """row_count can be passed in to skip a redundant COUNT(*) scan."""
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


# ── Stratified sample as CSV string (kept for any caller that still wants it) ─
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
    # row_count already known from get_metadata — don't recompute it again
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