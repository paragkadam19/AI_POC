"""
app.py — Flask backend for Data Quality POC Console
=================================================================
Flow: Upload CSV → DuckDB ingest → Schema Discovery → SODA YAML →
      Data Quality → Schema Validation → Schema Changes

Tab 4 (Data Quality) now runs real SODA checks directly against
DuckDB via soda_executor.py — no Bedrock calls, no CSV sampling
sent over the network. 100% of Tab 4's AI cost is gone.
"""
import os, sys, json, re, time, shutil
from datetime import datetime

from logger_config import setup_logging, get_logger

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

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, ENGINES_DIR)

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import mfg_poc7_soda_yaml as poc7
import mfg_poc3a_drift    as poc3a
import mfg_poc3b_drift    as poc3b
from bedrock_client import ask_json, ask
from duckdb_helper import ingest_csv, get_preview, get_full_metadata_for_ai, get_sample_csv
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

    # ── Preferred path: raw streamed body ────────────────────────────────────
    # The browser sends the file as the raw request body (no multipart
    # envelope). Flask's form-parser never touches it, so the bytes hit
    # disk exactly once instead of twice.
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

    # ── Fallback: classic multipart form upload ──────────────────────────────
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

    # ── Fallback: pasted CSV text as JSON ─────────────────────────────────────
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
@app.route("/api/poc1/run", methods=["POST"])
def run_poc1():
    ds       = STATE["dataset_id"]
    csv_path = current_csv_path()
    if not ds or not csv_path:
        return jsonify({"error": "Upload a CSV first"}), 400
    try:
        t_start = time.time()

        # ── Step 1: get file size ────────────────────────────────────────────
        t0 = time.time()
        file_size_bytes = os.path.getsize(csv_path)
        tbl             = table_name(ds)
        logger.info(f"[poc1] STEP 1 - getsize: {time.time()-t0:.3f}s")

        # ── Step 2: fetch metadata from DuckDB (SUMMARIZE + histogram + sample) ─
        t0 = time.time()
        metadata = get_full_metadata_for_ai(DB_FILE, tbl, STATE["filename"], file_size_bytes, SAMPLE_ROWS_SCHEMA)
        logger.info(f"[poc1] STEP 2 - get_full_metadata_for_ai (DuckDB): {time.time()-t0:.3f}s")

        # ── Step 3: build the prompt strings ─────────────────────────────────
        t0 = time.time()
        system_prompt, user_prompt = build_schema_discovery_prompt(metadata)
        prompt_len_chars = len(system_prompt) + len(user_prompt)
        logger.info(f"[poc1] STEP 3 - build_schema_discovery_prompt: {time.time()-t0:.3f}s | prompt_chars={prompt_len_chars}")
        logger.info(f"[poc1] USER PROMPT:\n{user_prompt}")  
        # ── Step 4: the actual Bedrock call (network + model generation) ────
        t0 = time.time()
        result = ask_json(user_prompt, system_prompt)
        logger.info(f"[poc1] STEP 4 - ask_json (Bedrock call, incl. JSON parse): {time.time()-t0:.3f}s")

        # ── Step 5: save result to disk ───────────────────────────────────────
        t0 = time.time()
        versioned_name = save_versioned(ds, "poc1", result)
        logger.info(f"[poc1] STEP 5 - save_versioned: {time.time()-t0:.3f}s")

        # ── Step 6: contract creation (first run only) ───────────────────────
        t0 = time.time()
        cpath           = contract_path(ds)
        is_new_contract = not os.path.exists(cpath)
        if is_new_contract:
            contract = {col["column"]: col["data_type"] for col in result.get("schema", [])}
            with open(cpath, "w") as f:
                json.dump(contract, f, indent=2)
        logger.info(f"[poc1] STEP 6 - contract write: {time.time()-t0:.3f}s | new_contract={is_new_contract}")

        result["_dataset_id"]       = ds
        result["_version_file"]     = versioned_name
        result["_contract_created"] = is_new_contract

        logger.info(f"[poc1] TOTAL route time: {time.time()-t_start:.3f}s")
        return jsonify(result)
    except Exception as e:
        logger.error(f"Schema discovery (poc1) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    

@app.route("/api/poc1/versions")
def poc1_versions():
    ds = STATE["dataset_id"]
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    return jsonify(list_versions(ds, "poc1"))


# ── Tab 3: SODA YAML (POC 7) ──────────────────────────────────────────────────
@app.route("/api/poc7/run", methods=["POST"])
def run_poc7():
    ds             = STATE["dataset_id"]
    schema_profile = ds and load_latest(ds, "poc1")
    if not schema_profile:
        return jsonify({"error": "Run Schema Discovery (Tab 2) first"}), 400
    try:
        # FIX: Use safe string replacement instead of .format()
        schema_json = json.dumps(schema_profile, indent=2)
        prompt = poc7.PROMPT.replace("{table}", table_name(ds)).replace(
            "{schema_profile}", schema_json
        )
        yaml_output = ask(prompt, poc7.SYSTEM).strip()
        if yaml_output.startswith("```"):
            yaml_output = "\n".join(yaml_output.split("\n")[1:])
        if yaml_output.endswith("```"):
            yaml_output = "\n".join(yaml_output.split("\n")[:-1])
        yaml_output = yaml_output.strip()

        save_versioned(ds, "soda", yaml_output, ext="yaml")
        check_count = sum(
            1 for line in yaml_output.split("\n")
            if line.strip().startswith("- ") and not line.strip().startswith("- value")
        )
        return jsonify({"yaml": yaml_output, "check_count": check_count, "dataset_id": ds})
    except Exception as e:
        logger.error(f"DQ YAML generation (poc7) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 4: Data Quality (now runs real SODA checks via DuckDB — no AI) ───────
@app.route("/api/poc2/run", methods=["POST"])
def run_poc2():
    ds = STATE["dataset_id"]
    if not ds or not current_csv_path():
        return jsonify({"error": "Upload a CSV first"}), 400

    yaml_path = os.path.join(dataset_dir(ds), "soda_latest.yaml")
    if not os.path.exists(yaml_path):
        return jsonify({"error": "Generate the Quality Checks YAML (Tab 3) first"}), 400

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

        prompt = poc3a.PROMPT.format(
            contract    = json.dumps(contract, indent=2),
            poc1_schema = json.dumps(poc1_schema, indent=2)
        )
        result                   = ask_json(prompt, poc3a.SYSTEM)
        result["_dataset_id"]    = ds
        result["_contract_file"] = os.path.basename(cpath)
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

        prompt = poc3b.PROMPT.format(
            previous_columns = json.dumps(previous_columns, indent=2),
            current_columns  = json.dumps(current_columns, indent=2)
        )
        result                     = ask_json(prompt, poc3b.SYSTEM)
        result["is_first_run"]     = False
        result["compared_against"] = previous_file
        result["_dataset_id"]      = ds

        save_versioned(ds, "poc3b", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Schema change detection (poc3b) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500