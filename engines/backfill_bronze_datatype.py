"""
Backfill kb_metadata.bronze_datatype from table_schema_snapshot.

This script fills NULL bronze_datatype values by matching table_name and
column_name against the actual DuckDB schema snapshot.
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
        description="Backfill kb_metadata.bronze_datatype from table_schema_snapshot."
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
    args = parser.parse_args()

    from poc_package.engines.duckdb_helper import DUCKDB_CONFIG
    from poc_package.engines.kb_manager import _connect

    conn = _connect(args.db_file)
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

        targets = conn.execute("""
            SELECT id, table_name, column_name, bronze_datatype
            FROM kb_metadata
            WHERE record_type = 'column'
        """).fetchall()

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
        result = {
            "success": True,
            "updated": updated,
            "skipped": skipped,
        }
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
