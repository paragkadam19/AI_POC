"""
app.py — Flask backend for Data Quality POC Console
=================================================================
Flow: Upload CSV → DuckDB ingest → Schema Discovery → SODA YAML →
      Data Quality → Schema Validation → Schema Changes

Human-in-the-loop:
- Tab 2: AI result shown for review/edit → user clicks Approve → saved
- Tab 3: AI YAML shown for review/edit → user clicks Approve → saved
- Tab 4: Runs real SODA checks directly against DuckDB (no AI call)
"""
import os, sys, json, re, time, shutil
from datetime import datetime

from logger_config import setup_logging, get_logger

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

setup_logging()
logger = get_logger(__name__)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR   = os.path.join(BASE_DIR, "storage")
ENGINES_DIR   = os.path.join(BASE_DIR, "engines")
OUTPUTS_DIR   = os.path.join(STORAGE_DIR, "outputs")
UPLOADS_DIR   = os.path.join(STORAGE_DIR, "uploads")
CONTRACTS_DIR = os.path.join(STORAGE_DIR, "contracts")
os.environ["STORAGE_DIR"] = STORAGE_DIR
for d in (STORAGE_DIR, OUTPUTS_DIR, UPLOADS_DIR, CONTRACTS_DIR):
    os.makedirs(d, exist_ok=True)

if load_dotenv:
    root_env = os.path.join(os.path.dirname(BASE_DIR), ".env")
    pkg_env  = os.path.join(BASE_DIR, ".env")
    loaded = False
    if os.path.exists(root_env):
        loaded = bool(load_dotenv(root_env, override=False)) or loaded
        logger.info(f"[env] loaded .env from {root_env}")
    if os.path.exists(pkg_env):
        loaded = bool(load_dotenv(pkg_env, override=False)) or loaded
        logger.info(f"[env] loaded .env from {pkg_env}")
    if not loaded:
        logger.info("[env] no .env file found in project root or poc_package/")
else:
    logger.info("[env] python-dotenv not installed; .env file will not be loaded automatically")

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, ENGINES_DIR)

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import mfg_poc7_soda_yaml as poc7
from bedrock_client import ask_json, ask
from prompt_builder import build_schema_discovery_prompt
from soda_executor import run_soda_checks_from_yaml
from duckdb_helper import (
    ingest_csv, get_preview, get_full_metadata_for_ai, get_sample_csv,
    SAMPLE_ROWS_SCHEMA,
)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DB_FILE = os.path.join(STORAGE_DIR, "ai_poc_dq.duckdb")
STATE   = {"dataset_id": None, "filename": None}


# ── helpers ───────────────────────────────────────────────────────────────────
def make_dataset_id(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", filename).strip("_").lower()
    return stem or "dataset"

_last_ts = {"value": None, "seq": 0}
def ts() -> str:
    base = datetime.now().strftime("%Y%m%d_%H%M%S")
    if base == _last_ts["value"]:
        _last_ts["seq"] += 1
    else:
        _last_ts["value"] = base
        _last_ts["seq"]   = 0
    return base if _last_ts["seq"] == 0 else f"{base}_{_last_ts['seq']:03d}"

def dataset_dir(dataset_id: str) -> str:
    d = os.path.join(OUTPUTS_DIR, dataset_id)
    os.makedirs(d, exist_ok=True)
    return d

def save_versioned(dataset_id: str, stage: str, data, ext="json") -> str:
    d              = dataset_dir(dataset_id)
    stamp          = ts()
    versioned_name = f"{stage}_{stamp}.{ext}"
    latest_name    = f"{stage}_latest.{ext}"
    text = json.dumps(data, indent=2) if ext == "json" else data
    for name in (versioned_name, latest_name):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
    return versioned_name

def list_versions(dataset_id: str, stage: str, ext="json") -> list:
    d       = dataset_dir(dataset_id)
    pattern = re.compile(rf"^{re.escape(stage)}_(\d{{8}}_\d{{6}})\.{ext}$")
    out     = []
    for name in os.listdir(d):
        m = pattern.match(name)
        if m:
            out.append({"file": name, "timestamp": m.group(1)})
    return sorted(out, key=lambda x: x["timestamp"])

def load_latest(dataset_id: str, stage: str, ext="json"):
    path = os.path.join(dataset_dir(dataset_id), f"{stage}_latest.{ext}")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f) if ext == "json" else f.read()

def current_csv_path():
    if not STATE["dataset_id"]:
        return None
    d = os.path.join(UPLOADS_DIR, STATE["dataset_id"])
    if not os.path.isdir(d):
        return None
    versions = sorted(os.listdir(d))
    return os.path.join(d, versions[-1]) if versions else None

def contract_path(dataset_id: str) -> str:
    return os.path.join(CONTRACTS_DIR, f"{dataset_id}_contract.json")

def table_name(dataset_id: str) -> str:
    return f"tbl_{dataset_id}"


def _schema_map(schema_profile: dict) -> dict:
    return {
        col.get("column"): col.get("data_type")
        for col in (schema_profile or {}).get("schema", [])
        if col.get("column")
    }


def compare_contract_to_schema(contract: dict, current_schema: dict) -> dict:
    contract = contract or {}
    current  = _schema_map(current_schema)

    contract_cols = set(contract.keys())
    current_cols  = set(current.keys())

    missing_columns = sorted(contract_cols - current_cols)
    extra_columns   = sorted(current_cols - contract_cols)

    type_mismatches = []
    null_issues = []
    for col in sorted(contract_cols & current_cols):
        if str(contract.get(col)).lower() != str(current.get(col)).lower():
            type_mismatches.append({
                "column": col,
                "expected_type": contract.get(col),
                "actual_type": current.get(col),
            })

    for col in (current_schema or {}).get("schema", []):
        if col.get("null_count", 0) > 0 and col.get("column") in contract_cols:
            null_issues.append({
                "column": col.get("column"),
                "null_count": col.get("null_count", 0),
                "recommendation": "Review nulls for contract-required column",
            })

    severity = "none"
    can_pipeline_proceed = True
    validation_passed = True

    if missing_columns:
        severity = "critical"
        can_pipeline_proceed = False
        validation_passed = False
    elif type_mismatches:
        severity = "high"
        can_pipeline_proceed = False
        validation_passed = False
    elif extra_columns:
        severity = "medium"

    summary = "Schema matches the contract."
    if missing_columns:
        summary = f"Missing contract columns: {', '.join(missing_columns)}."
    elif type_mismatches:
        summary = f"Type mismatches found for {len(type_mismatches)} column(s)."
    elif extra_columns:
        summary = f"Extra columns found: {', '.join(extra_columns)}."

    recommended_actions = []
    if missing_columns:
        recommended_actions.append("Add the missing contract columns or reject the upload.")
    if type_mismatches:
        recommended_actions.append("Fix the data types to match the locked contract.")
    if extra_columns:
        recommended_actions.append("Review whether extra columns should be added to the contract.")
    if null_issues:
        recommended_actions.append("Review null-heavy contract columns before proceeding.")

    return {
        "validation_passed": validation_passed,
        "severity": severity,
        "can_pipeline_proceed": can_pipeline_proceed,
        "summary": summary,
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "type_mismatches": type_mismatches,
        "null_issues": null_issues,
        "recommended_actions": recommended_actions,
    }


def compare_schema_snapshots(previous_schema: dict, current_schema: dict, previous_file: str = None) -> dict:
    previous_cols = previous_schema.get("schema", []) if previous_schema else []
    current_cols  = current_schema.get("schema", []) if current_schema else []

    prev_map = {c.get("column"): c.get("data_type") for c in previous_cols if c.get("column")}
    curr_map = {c.get("column"): c.get("data_type") for c in current_cols if c.get("column")}

    prev_names = list(prev_map.keys())
    curr_names = list(curr_map.keys())

    new_columns = sorted(set(curr_names) - set(prev_names))
    dropped_columns = sorted(set(prev_names) - set(curr_names))

    type_changes = []
    for col in sorted(set(prev_names) & set(curr_names)):
        if str(prev_map.get(col)).lower() != str(curr_map.get(col)).lower():
            type_changes.append({
                "column": col,
                "old_type": prev_map.get(col),
                "new_type": curr_map.get(col),
            })

    reordered = prev_names != curr_names and set(prev_names) == set(curr_names)

    possible_renames = []
    if len(previous_cols) == len(current_cols):
        for i, (prev_col, curr_col) in enumerate(zip(previous_cols, current_cols)):
            prev_name = prev_col.get("column")
            curr_name = curr_col.get("column")
            if prev_name != curr_name and prev_name not in curr_names and curr_name not in prev_names:
                confidence = "medium"
                if str(prev_col.get("data_type")).lower() == str(curr_col.get("data_type")).lower():
                    confidence = "high"
                possible_renames.append({
                    "position": i,
                    "old_name": prev_name,
                    "new_name": curr_name,
                    "confidence": confidence,
                })

    change_detected = bool(new_columns or dropped_columns or type_changes or reordered or possible_renames)
    summary = "No schema changes detected."
    if change_detected:
        parts = []
        if new_columns:
            parts.append(f"new columns: {', '.join(new_columns)}")
        if dropped_columns:
            parts.append(f"dropped columns: {', '.join(dropped_columns)}")
        if type_changes:
            parts.append(f"type changes in {len(type_changes)} column(s)")
        if reordered:
            parts.append("column order changed")
        if possible_renames:
            parts.append(f"possible renames: {len(possible_renames)}")
        summary = "; ".join(parts).capitalize() + "."

    recommended_actions = []
    if new_columns:
        recommended_actions.append("Review whether the new columns should be added to the downstream contract.")
    if dropped_columns:
        recommended_actions.append("Check whether the dropped columns were intentionally removed.")
    if type_changes:
        recommended_actions.append("Validate type changes before loading downstream systems.")
    if reordered:
        recommended_actions.append("Confirm the reorder is expected and does not affect positional consumers.")
    if possible_renames:
        recommended_actions.append("Review possible renames manually before approving schema drift.")

    return {
        "change_detected": change_detected,
        "summary": summary,
        "new_columns": new_columns,
        "dropped_columns": dropped_columns,
        "possible_renames": possible_renames,
        "type_changes": type_changes,
        "reordered": reordered,
        "recommended_actions": recommended_actions,
        "compared_against": previous_file,
        "is_first_run": False,
    }


# ── static UI ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
def status():
    ds = STATE["dataset_id"]
    return jsonify({
        "csv_ready":  bool(ds and current_csv_path()),
        "poc1_ready": bool(ds and load_latest(ds, "poc1") is not None),
        "csv_file":   STATE["filename"],
        "dataset_id": ds,
    })

@app.route("/api/datasets")
def list_datasets():
    out = []
    if os.path.isdir(OUTPUTS_DIR):
        for dataset_id in sorted(os.listdir(OUTPUTS_DIR)):
            out.append({
                "dataset_id":     dataset_id,
                "poc1_versions":  len(list_versions(dataset_id, "poc1")),
                "poc2_versions":  len(list_versions(dataset_id, "poc2")),
                "poc3a_versions": len(list_versions(dataset_id, "poc3a")),
                "poc3b_versions": len(list_versions(dataset_id, "poc3b")),
                "has_contract":   os.path.exists(contract_path(dataset_id)),
            })
    return jsonify(out)


# ── Tab 1: CSV Upload + DuckDB ingest ─────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    filename     = "pasted.csv"
    csv_path     = None
    dataset_id   = None
    content_type = request.content_type or ""

    if content_type.startswith("application/octet-stream"):
        filename   = request.args.get("filename", filename)
        dataset_id = make_dataset_id(filename)
        udir       = os.path.join(UPLOADS_DIR, dataset_id)
        os.makedirs(udir, exist_ok=True)
        csv_path   = os.path.join(udir, f"{ts()}.csv")
        t0 = time.time()
        with open(csv_path, "wb") as out:
            shutil.copyfileobj(request.stream, out, length=16 * 1024 * 1024)
        logger.info(f"upload streamed to disk in {time.time()-t0:.2f}s | file={filename}")

    elif "file" in request.files:
        f          = request.files["file"]
        filename   = f.filename or filename
        dataset_id = make_dataset_id(filename)
        udir       = os.path.join(UPLOADS_DIR, dataset_id)
        os.makedirs(udir, exist_ok=True)
        csv_path   = os.path.join(udir, f"{ts()}.csv")
        t0 = time.time()
        f.save(csv_path)
        logger.info(f"upload saved to disk (multipart) in {time.time()-t0:.2f}s | file={filename}")

    else:
        body = request.get_json(silent=True)
        if body and "csv_text" in body:
            text       = body["csv_text"]
            filename   = body.get("filename", filename)
            dataset_id = make_dataset_id(filename)
            udir       = os.path.join(UPLOADS_DIR, dataset_id)
            os.makedirs(udir, exist_ok=True)
            csv_path   = os.path.join(udir, f"{ts()}.csv")
            with open(csv_path, "w") as out:
                out.write(text)

    if not csv_path:
        return jsonify({"error": "No CSV provided"}), 400

    STATE["dataset_id"] = dataset_id
    STATE["filename"]   = filename

    try:
        table = table_name(dataset_id)
        t1    = time.time()
        ingest_result = ingest_csv(csv_path, DB_FILE, table, original_filename=filename)
        logger.info(f"DuckDB ingest completed in {time.time()-t1:.2f}s | table={table}")

        if not ingest_result.get("success"):
            logger.error(f"DB ingest reported failure for table={table}: {ingest_result.get('error')}")
            return jsonify({"error": ingest_result.get("error", "DB ingest failed")}), 500

        sample, columns, row_count = get_preview(
            DB_FILE, table, n=8, row_count=ingest_result["row_count"]
        )
        return jsonify({
            "dataset_id": dataset_id,
            "filename":   filename,
            "row_count":  row_count,
            "columns":    columns,
            "sample":     sample,
            "duckdb":     ingest_result,
        })
    except Exception as e:
        logger.error(f"Upload/ingest failed for dataset={dataset_id}: {e}", exc_info=True)
        return jsonify({"error": f"DB error: {str(e)}"}), 500


# ── Tab 2: Schema Discovery (POC 1) ───────────────────────────────────────────
# HUMAN IN THE LOOP: run_poc1 returns result for review only.
# Nothing is saved until user clicks Approve → /api/poc1/approve

@app.route("/api/poc1/run", methods=["POST"])
def run_poc1():
    ds       = STATE["dataset_id"]
    csv_path = current_csv_path()
    if not ds or not csv_path:
        return jsonify({"error": "Upload a CSV first"}), 400
    try:
        t_start = time.time()

        t0 = time.time()
        file_size_bytes = os.path.getsize(csv_path)
        tbl             = table_name(ds)
        logger.info(f"[poc1] STEP 1 - getsize: {time.time()-t0:.3f}s")

        t0       = time.time()
        metadata = get_full_metadata_for_ai(DB_FILE, tbl, STATE["filename"], file_size_bytes, SAMPLE_ROWS_SCHEMA)
        logger.info(f"[poc1] STEP 2 - get_full_metadata_for_ai (DuckDB): {time.time()-t0:.3f}s")

        t0                         = time.time()
        system_prompt, user_prompt = build_schema_discovery_prompt(metadata)
        prompt_len_chars           = len(system_prompt) + len(user_prompt)
        logger.info(f"[poc1] STEP 3 - build_schema_discovery_prompt: {time.time()-t0:.3f}s | prompt_chars={prompt_len_chars}")
        logger.info(f"[poc1] USER PROMPT:\n{user_prompt}")

        t0     = time.time()
        result = ask_json(user_prompt, system_prompt)
        logger.info(f"[poc1] STEP 4 - ask_json (Bedrock call): {time.time()-t0:.3f}s")

        # ── NOT saving here — user must review and approve first ─────────────
        result["_dataset_id"] = ds
        logger.info(f"[poc1] TOTAL route time (AI only, not saved yet): {time.time()-t_start:.3f}s")
        return jsonify(result)

    except Exception as e:
        logger.error(f"Schema discovery (poc1) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/poc1/approve", methods=["POST"])
def approve_poc1():
    """
    Human-in-the-loop approval for Tab 2.
    User reviews/edits the schema in the UI, then clicks Approve.
    Only then does the result get saved to disk and contract locked.
    """
    ds = STATE["dataset_id"]
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    try:
        result = request.get_json()
        if not result:
            return jsonify({"error": "No schema data received"}), 400

        # Save approved (possibly edited) schema
        versioned_name = save_versioned(ds, "poc1", result)
        logger.info(f"[poc1] schema approved and saved | dataset={ds} | file={versioned_name}")

        # Lock the contract from this approved schema (first approval only)
        cpath           = contract_path(ds)
        is_new_contract = not os.path.exists(cpath)
        if is_new_contract:
            contract = {col["column"]: col["data_type"] for col in result.get("schema", [])}
            with open(cpath, "w") as f:
                json.dump(contract, f, indent=2)
            logger.info(f"[poc1] contract locked from approved schema | dataset={ds}")

        result["_dataset_id"]       = ds
        result["_version_file"]     = versioned_name
        result["_contract_created"] = is_new_contract
        return jsonify({"success": True, "version_file": versioned_name, "contract_created": is_new_contract})
    except Exception as e:
        logger.error(f"Schema approval (poc1) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/poc1/versions")
def poc1_versions():
    ds = STATE["dataset_id"]
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    return jsonify(list_versions(ds, "poc1"))


# ── Tab 3: SODA YAML (POC 7) ──────────────────────────────────────────────────
# HUMAN IN THE LOOP: run_poc7 returns YAML for review only.
# Nothing is saved until user clicks Approve → /api/poc7/approve

@app.route("/api/poc7/run", methods=["POST"])
def run_poc7():
    ds = STATE["dataset_id"]
    if not ds:
        return jsonify({"error": "Upload a CSV first"}), 400

    # STRICT GATE: only approved schema (saved poc1_latest.json) is accepted
    # pending/unapproved AI output is never used here
    schema_profile = load_latest(ds, "poc1")
    if not schema_profile:
        return jsonify({
            "error": "Schema Discovery must be approved first. Run Tab 2 and click 'Approve & Save' before generating YAML."
        }), 400

    try:
        schema_json = json.dumps(schema_profile, indent=2)
        prompt = poc7.PROMPT.replace("{table}", table_name(ds)).replace(
            "{schema_profile}", schema_json
        )
        logger.info(f"[poc7] using approved poc1_latest.json | dataset={ds}")
        logger.info(f"[poc7] Prompt (first 500 chars):\n{prompt[:500]}")

        yaml_output = ask(prompt, poc7.SYSTEM).strip()
        if yaml_output.startswith("```"):
            yaml_output = "\n".join(yaml_output.split("\n")[1:])
        if yaml_output.endswith("```"):
            yaml_output = "\n".join(yaml_output.split("\n")[:-1])
        yaml_output = yaml_output.strip()

        # NOT saving here — user must approve in Tab 3 first
        check_count = sum(
            1 for line in yaml_output.split("\n")
            if line.strip().startswith("- ") and not line.strip().startswith("- value")
        )
        logger.info(f"[poc7] YAML generated (not saved yet) | checks={check_count}")
        return jsonify({"yaml": yaml_output, "check_count": check_count, "dataset_id": ds})

    except Exception as e:
        logger.error(f"DQ YAML generation (poc7) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/poc7/approve", methods=["POST"])
def approve_poc7():
    """
    Human-in-the-loop approval for Tab 3.
    User reviews/edits the YAML in the textarea, then clicks Approve.
    Only then does the YAML get saved to disk for Tab 4 to use.
    """
    ds = STATE["dataset_id"]
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    try:
        body = request.get_json()
        if not body or "yaml" not in body:
            return jsonify({"error": "No YAML data received"}), 400

        yaml_text = body["yaml"].strip()
        if not yaml_text:
            return jsonify({"error": "YAML content is empty"}), 400

        versioned_name = save_versioned(ds, "soda", yaml_text, ext="yaml")
        logger.info(f"[poc7] YAML approved and saved | dataset={ds} | file={versioned_name}")

        check_count = sum(
            1 for line in yaml_text.split("\n")
            if line.strip().startswith("- ") and not line.strip().startswith("- value")
        )
        return jsonify({"success": True, "version_file": versioned_name, "check_count": check_count})
    except Exception as e:
        logger.error(f"YAML approval (poc7) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 4: Data Quality (real SODA checks via DuckDB — no AI) ────────────────
@app.route("/api/poc2/run", methods=["POST"])
def run_poc2():
    ds = STATE["dataset_id"]
    if not ds or not current_csv_path():
        return jsonify({"error": "Upload a CSV first"}), 400

    yaml_path = os.path.join(dataset_dir(ds), "soda_latest.yaml")
    if not os.path.exists(yaml_path):
        return jsonify({"error": "Generate and approve Quality Checks YAML (Tab 3) first"}), 400

    try:
        tbl    = table_name(ds)
        result = run_soda_checks_from_yaml(DB_FILE, yaml_path, tbl)
        result["_dataset_id"] = ds
        save_versioned(ds, "poc2", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Data quality check run (poc2) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 5: Schema Validation (POC 3a) ────────────────────────────────────────
@app.route("/api/poc3a/run", methods=["POST"])
def run_poc3a():
    ds          = STATE["dataset_id"]
    poc1_schema = ds and load_latest(ds, "poc1")
    if not poc1_schema:
        return jsonify({"error": "Run Schema Discovery (Tab 2) first"}), 400
    try:
        cpath = contract_path(ds)
        if not os.path.exists(cpath):
            contract = {col["column"]: col["data_type"] for col in poc1_schema.get("schema", [])}
            with open(cpath, "w") as f:
                json.dump(contract, f, indent=2)
        with open(cpath) as f:
            contract = json.load(f)

        result                   = compare_contract_to_schema(contract, poc1_schema)
        result["_dataset_id"]     = ds
        result["_contract_file"]  = os.path.basename(cpath)
        save_versioned(ds, "poc3a", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Schema validation (poc3a) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 6: Schema Change Discovery (POC 3b) ──────────────────────────────────
@app.route("/api/poc3b/run", methods=["POST"])
def run_poc3b():
    ds             = STATE["dataset_id"]
    current_schema = ds and load_latest(ds, "poc1")
    if not current_schema:
        return jsonify({"error": "Run Schema Discovery (Tab 2) first"}), 400
    try:
        versions = list_versions(ds, "poc1")

        if len(versions) < 2:
            result = {
                "change_detected": False,
                "summary": "First profiled upload — no previous snapshot to compare against.",
                "new_columns": [], "dropped_columns": [], "possible_renames": [],
                "type_changes": [], "reordered": False, "recommended_actions": [],
                "is_first_run": True, "_dataset_id": ds,
            }
            save_versioned(ds, "poc3b", result)
            return jsonify(result)

        previous_file = versions[-2]["file"]
        with open(os.path.join(dataset_dir(ds), previous_file)) as f:
            previous_schema = json.load(f)

        previous_columns = [{"column": c["column"], "data_type": c["data_type"]}
                            for c in previous_schema.get("schema", [])]
        current_columns  = [{"column": c["column"], "data_type": c["data_type"]}
                            for c in current_schema.get("schema", [])]
        result = compare_schema_snapshots(previous_schema, current_schema, previous_file=previous_file)
        result["_dataset_id"] = ds

        save_versioned(ds, "poc3b", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Schema change detection (poc3b) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
