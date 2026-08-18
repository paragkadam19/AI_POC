"""
Backfill kb_metadata.bronze_datatype from table_schema_snapshot.

This script fills NULL bronze_datatype values by matching table_name and
column_name against the actual DuckDB schema snapshot.

Multi-tenancy
-------------
table_schema_snapshot and kb_metadata now live inside each user's own
DuckDB schema (see kb_manager.py / duckdb_helper.py) rather than in one
shared "main" schema. By default this script discovers every schema
registered in admin.users.schema_name and backfills each one in turn.
Pass --schema-name to target just one schema instead of all of them.

admin.users itself stays a fixed system table, queried unscoped with the
admin. prefix — it is never part of the per-user schema scoping.
"""

import argparse
import json
import os
import sys

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


def _default_db_path() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_dir, "storage", "data_resonance.duckdb")


def _default_storage_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_dir, "storage")


def _discover_schemas(db_file: str) -> list:
    """
    Return every distinct schema_name registered in admin.users.
    Returns [] (with a warning, not a crash) on a fresh install where
    admin.users doesn't exist yet or has no rows.
    """
    import duckdb
    conn = duckdb.connect(db_file, read_only=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT schema_name FROM admin.users "
            "WHERE schema_name IS NOT NULL AND schema_name != ''"
        ).fetchall()
        return sorted({str(r[0]) for r in rows if r and r[0]})
    except Exception as e:
        print(f"[warning] could not read admin.users (fresh install / no users yet?): {e}", file=sys.stderr)
        return []
    finally:
        conn.close()


def _backfill_schema(db_file: str, schema_name: str, connect_fn) -> dict:
    """Run the backfill inside one user's schema and return its result."""
    conn = connect_fn(db_file, schema_name=schema_name)
    try:
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

        snapshot_rows = conn.execute("""
            SELECT table_name, column_name, data_type
            FROM table_schema_snapshot
        """).fetchall()
        snapshot_map = {
            (str(t), str(c)): str(dt)
            for t, c, dt in snapshot_rows
            if t and c and dt
        }

        try:
            targets = conn.execute("""
                SELECT id, table_name, column_name, bronze_datatype
                FROM kb_metadata
                WHERE record_type = 'column'
            """).fetchall()
        except Exception:
            # This user has no kb_metadata table yet (never built a KB) —
            # nothing to backfill in this schema.
            return {
                "schema": schema_name, "success": True,
                "updated": 0, "skipped": 0,
                "note": "kb_metadata not present in this schema",
            }

        updated = 0
        skipped = 0
        for row_id, table_name, column_name, bronze_datatype in targets:
            if bronze_datatype not in (None, ""):
                skipped += 1
                continue
            new_type = snapshot_map.get((str(table_name), str(column_name)))
            if not new_type:
                skipped += 1
                continue
            conn.execute(
                """
                UPDATE kb_metadata
                SET bronze_datatype = ?,
                    metadata_json = json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.bronze_datatype',
                        ?
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [new_type, new_type, row_id],
            )
            updated += 1

        conn.commit()
        return {"schema": schema_name, "success": True, "updated": updated, "skipped": skipped}
    except Exception as e:
        return {"schema": schema_name, "success": False, "error": str(e), "updated": 0, "skipped": 0}
    finally:
        conn.close()


def main() -> int:
    engines_dir = os.path.abspath(os.path.dirname(__file__))
    package_dir = os.path.abspath(os.path.join(engines_dir, ".."))
    project_root = os.path.abspath(os.path.join(package_dir, ".."))
    for path in (package_dir, engines_dir, project_root):
        if path not in sys.path:
            sys.path.insert(0, path)

    if load_dotenv:
        root_env = os.path.join(project_root, ".env")
        pkg_env = os.path.join(package_dir, ".env")
        if os.path.exists(root_env):
            load_dotenv(root_env, override=True)
        if os.path.exists(pkg_env):
            load_dotenv(pkg_env, override=True)

    parser = argparse.ArgumentParser(
        description="Backfill kb_metadata.bronze_datatype from table_schema_snapshot, "
                    "across every user schema (or one, via --schema-name)."
    )
    parser.add_argument(
        "--db-file",
        default=_default_db_path(),
        help="Path to the DuckDB database file.",
    )
    parser.add_argument(
        "--storage-dir",
        default=_default_storage_dir(),
        help="Path to the app storage directory.",
    )
    parser.add_argument(
        "--schema-name",
        default=None,
        help="Backfill only this DuckDB schema instead of every schema found in admin.users.",
    )
    args = parser.parse_args()

    from poc_package.engines.duckdb_helper import DUCKDB_CONFIG
    from poc_package.engines.kb_manager import _connect

    if args.schema_name:
        schemas = [args.schema_name]
    else:
        schemas = _discover_schemas(args.db_file)
        if not schemas:
            result = {
                "success": True,
                "schemas_processed": 0,
                "total_updated": 0,
                "total_skipped": 0,
                "note": "No schemas found in admin.users — nothing to backfill.",
            }
            print(json.dumps(result, indent=2, default=str))
            return 0

    results = []
    total_updated = 0
    total_skipped = 0
    for schema_name in schemas:
        result = _backfill_schema(args.db_file, schema_name, _connect)
        results.append(result)
        total_updated += result.get("updated", 0)
        total_skipped += result.get("skipped", 0)

    summary = {
        "success": all(r.get("success") for r in results),
        "schemas_processed": len(results),
        "total_updated": total_updated,
        "total_skipped": total_skipped,
        "by_schema": results,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
