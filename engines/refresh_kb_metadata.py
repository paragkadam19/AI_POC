"""
Refresh KB metadata from the DuckDB database with AI enrichment.

This script scans all non-KB tables in the active DuckDB file and ensures they
are represented in kb_metadata, then updates kb_state.json and semantic_layer.md.
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
    return os.path.join(base_dir, "storage", "ai_poc_dq.duckdb")


def _default_storage_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_dir, "storage")


def _staging_path(storage_dir: str) -> str:
    return os.path.join(storage_dir, "kb", "kb_refresh_staging.json")


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
        description="Refresh kb_metadata from all tables in ai_poc_dq.duckdb."
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

    from poc_package.engines.kb_manager import (
        apply_kb_refresh_staging,
        finalize_kb_refresh,
        generate_kb_refresh_staging,
    )
    staging_path = _staging_path(args.storage_dir)
    t0 = time.time()
    stage_result = generate_kb_refresh_staging(args.db_file, args.storage_dir, staging_path)
    if not stage_result.get("success"):
        print(json.dumps(stage_result, indent=2, default=str))
        return 1

    apply_result = apply_kb_refresh_staging(args.db_file, args.storage_dir, staging_path)
    if not apply_result.get("success"):
        print(json.dumps(apply_result, indent=2, default=str))
        return 1

    finalize_result = finalize_kb_refresh(args.db_file, args.storage_dir)
    finalize_result["elapsed_seconds"] = round(time.time() - t0, 2)
    result = {
        "success": bool(finalize_result.get("success")),
        "mode": "full_refresh",
        "stage": stage_result,
        "apply": apply_result,
        "finalize": finalize_result,
    }

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
