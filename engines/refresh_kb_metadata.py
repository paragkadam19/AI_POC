"""
Refresh KB metadata from the DuckDB database with AI enrichment.

This script scans all non-KB tables in the active DuckDB file and ensures they
are represented in kb_metadata, then updates kb_state.json and semantic_layer.md.

Multi-tenancy
-------------
kb_metadata, kb_state.json, and semantic_layer.md now live per user schema
(see kb_manager.py). By default this script discovers every schema
registered in admin.users.schema_name and runs the full
stage -> apply -> finalize refresh for each one in turn. Pass
--schema-name to target just one schema instead of all of them.

Each schema also gets its own staging file
(storage_dir/kb/<schema_name>/kb_refresh_staging.json) so refreshes for
different users never overwrite each other's in-progress staging data.

admin.users itself stays a fixed system table, queried unscoped with the
admin. prefix — it is never part of the per-user schema scoping.
"""

import argparse
import json
import os
import sys
import time

os.environ.setdefault("DISABLE_LANGFUSE", "1")

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


def _staging_path(storage_dir: str, schema_name: str = None) -> str:
    if schema_name:
        return os.path.join(storage_dir, "kb", schema_name, "kb_refresh_staging.json")
    return os.path.join(storage_dir, "kb", "kb_refresh_staging.json")


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


def _refresh_schema(db_file: str, storage_dir: str, schema_name: str,
                     generate_fn, apply_fn, finalize_fn) -> dict:
    """Run the stage -> apply -> finalize flow for one user's schema."""
    staging_path = _staging_path(storage_dir, schema_name)
    t0 = time.time()

    stage_result = generate_fn(db_file, storage_dir, staging_path, schema_name=schema_name)
    if not stage_result.get("success"):
        return {
            "schema": schema_name, "success": False,
            "stage": stage_result, "elapsed_seconds": round(time.time() - t0, 2),
        }

    apply_result = apply_fn(db_file, storage_dir, staging_path, schema_name=schema_name)
    if not apply_result.get("success"):
        return {
            "schema": schema_name, "success": False,
            "stage": stage_result, "apply": apply_result,
            "elapsed_seconds": round(time.time() - t0, 2),
        }

    finalize_result = finalize_fn(db_file, storage_dir, schema_name=schema_name)
    return {
        "schema": schema_name,
        "success": bool(finalize_result.get("success")),
        "stage": stage_result,
        "apply": apply_result,
        "finalize": finalize_result,
        "elapsed_seconds": round(time.time() - t0, 2),
    }


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
        description="Refresh kb_metadata from all tables in data_resonance.duckdb, "
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
        help="Refresh only this DuckDB schema instead of every schema found in admin.users.",
    )
    args = parser.parse_args()

    from poc_package.engines.kb_manager import (
        apply_kb_refresh_staging,
        finalize_kb_refresh,
        generate_kb_refresh_staging,
    )

    if args.schema_name:
        schemas = [args.schema_name]
    else:
        schemas = _discover_schemas(args.db_file)
        if not schemas:
            result = {
                "success": True,
                "mode": "full_refresh",
                "schemas_processed": 0,
                "note": "No schemas found in admin.users — nothing to refresh.",
            }
            print(json.dumps(result, indent=2, default=str))
            return 0

    t0_all = time.time()
    by_schema = []
    for schema_name in schemas:
        schema_result = _refresh_schema(
            args.db_file, args.storage_dir, schema_name,
            generate_kb_refresh_staging, apply_kb_refresh_staging, finalize_kb_refresh,
        )
        by_schema.append(schema_result)

    result = {
        "success": all(r.get("success") for r in by_schema),
        "mode": "full_refresh",
        "schemas_processed": len(by_schema),
        "elapsed_seconds": round(time.time() - t0_all, 2),
        "by_schema": by_schema,
    }

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
