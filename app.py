"""
app.py — Flask backend for Data Quality POC Console
=================================================================
Flow: 01 Signal → 02 Tune → 03 Compose → 04 Soundcheck → 05 Resonance → 06 Pitch Drift → 07 Retune

Human-in-the-loop:
- Tab 2: AI result shown for review/edit → user clicks Approve → saved
- Tab 3: AI YAML shown for review/edit → user clicks Approve → saved
- Tab 4: Runs real SODA checks directly against DuckDB (no AI call)
- Tab 8: NL→SQL via KB-aware retrieval + Claude

Multi-tenancy
-------------
Every logged-in user is mapped, via admin.users.schema_name, to their own
DuckDB schema. All per-user tables (CSV loads, KB tables, snapshots, etc.)
are created inside that schema by opening connections through
`get_user_conn()`, which issues `SET search_path = '<schema>'` so unqualified
CREATE TABLE / SELECT statements resolve into the user's schema automatically.

The `admin` schema (admin.users, admin.login_activity) is a fixed system
schema and is always referenced explicitly with the `admin.` prefix — it is
NOT part of the per-user scoping and must never collide with a user schema
(see RESERVED_SCHEMA_NAMES below).

NOTE: ingest_csv / get_full_metadata_for_ai / write_table_schema_snapshot /
run_soda_checks_from_yaml / persist_kb / refresh_kb_from_duckdb /
upsert_example_pair / upsert_join_edges / poc8.question_to_sql now receive a
`schema_name` argument from this file. The implementations of those
functions (in duckdb_helper.py, kb_manager.py, soda_executor.py,
mfg_poc8_nl_sql.py) need to actually use it — e.g. by opening their DuckDB
connection through the same `SET search_path` pattern, or by qualifying
table names with it — for isolation to be complete end-to-end. The fallback
stubs below accept the extra kwarg harmlessly if those modules aren't
updated yet.
"""
import os, sys, json, re, time, shutil
from datetime import datetime
import yaml
import duckdb
from flask import session, redirect, url_for
import hashlib
import functools

from logger_config import setup_logging, get_logger

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

setup_logging()
logger = get_logger(__name__)
logger.info(f"[boot] python={sys.executable}")

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
        loaded = bool(load_dotenv(root_env, override=True)) or loaded
        logger.info(f"[env] loaded .env from {root_env}")
    if os.path.exists(pkg_env):
        loaded = bool(load_dotenv(pkg_env, override=True)) or loaded
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
import mfg_poc8_nl_sql as poc8
from bedrock_client import ask_json, ask
from prompt_builder import build_schema_discovery_prompt
from soda_executor import run_soda_checks_from_yaml, sanitize_soda_yaml_text
# Class-based KB API (new KBManager — may live in engines/kb_manager.py)
from kb_manager import init_kb_manager, get_kb_manager

# Function-based KB API (original file-store helpers).
# Try to import; fall back to no-op stubs if the new kb_manager doesn't have them.
# **kwargs on every stub so a `schema_name=...` call from this file never breaks
# things even before kb_manager.py has been updated to use it.
try:
    from kb_manager import (
        persist_kb, refresh_kb_from_duckdb, should_refresh_kb,
        upsert_example_pair, upsert_join_edges,
    )
except ImportError:
    def persist_kb(storage_dir, dataset_id, schema_profile, table_name=None, **kwargs):
        return {"documents_written": 0, "join_edges": 0}

    def refresh_kb_from_duckdb(db_file, storage_dir, **kwargs):
        return {"tables_indexed": 0, "join_edges": 0}

    def should_refresh_kb(storage_dir):
        return False

    def upsert_example_pair(storage_dir, dataset_id, question, sql, tables, tags=None, **kwargs):
        return {"success": False}

    def upsert_join_edges(storage_dir, edges, **kwargs):
        return {"success": False}
from duckdb_helper import (
    ingest_csv, get_preview, get_full_metadata_for_ai,
    SAMPLE_ROWS_SCHEMA, write_table_schema_snapshot, DUCKDB_CONFIG,
)

app = Flask(__name__, static_folder="static", static_url_path="")
# Sessions carry schema_name, which every DuckDB connection trusts to route
# a user to their own data — a hardcoded, source-committed secret_key would
# let anyone who's seen this file forge a session cookie for any user. Set
# FLASK_SECRET_KEY in your .env for a stable key across restarts; without
# it, a random key is generated at startup (safe, but invalidates existing
# sessions on every restart).
_flask_secret = os.getenv("FLASK_SECRET_KEY")
if not _flask_secret:
    import secrets as _secrets
    _flask_secret = _secrets.token_hex(32)
    logger.warning(
        "[security] FLASK_SECRET_KEY not set — using a random key generated "
        "at startup. Sessions will invalidate on every restart. Set "
        "FLASK_SECRET_KEY in your .env for stable sessions across restarts."
    )
app.secret_key = _flask_secret
CORS(app)

DB_FILE = os.path.join(STORAGE_DIR, "data_resonance.duckdb")
UPLOAD_COPY_CHUNK_SIZE = 32 * 1024 * 1024
#POC1_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
POC1_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Per-session cache for the (possibly large) poc1 metadata blob. Keyed by
# session_id so it doesn't leak between users the way a single global dict
# (the old STATE["poc1_metadata"]) did. dataset_id / filename live directly
# in the Flask session (see helpers below) since they're small.
POC1_METADATA_CACHE: dict = {}


# ── KB Manager initialisation ─────────────────────────────────────────────────
KB_MANAGER = None


# ── multi-tenant schema helpers ────────────────────────────────────────────────
# "admin" is reserved so a user literally named "admin" can't collide with the
# fixed system schema that holds admin.users / admin.login_activity.
RESERVED_SCHEMA_NAMES = {"admin", "main", "information_schema", "pg_catalog", "system", "temp"}


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sanitize_schema_name(user_id: str) -> str:
    """Derive a safe DuckDB schema name from a user_id (alnum/underscore only)."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", user_id or "").strip("_").lower()
    if not name:
        name = "user"
    if not re.match(r"^[a-zA-Z_]", name):
        name = f"u_{name}"
    if name in RESERVED_SCHEMA_NAMES:
        name = f"u_{name}"
    return name


def _ensure_schema(conn, schema_name: str) -> None:
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema_name)}")


def _get_user_schema(conn, user_id: str) -> str:
    """Look up the schema mapped to a given user_id in admin.users."""
    row = conn.execute(
        "SELECT schema_name FROM admin.users WHERE user_id = ?",
        [user_id]
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"No schema mapping found for user: {user_id}")
    return row[0]


def _current_schema_name() -> str:
    """Schema name for the logged-in user, resolved once at login and cached in session."""
    schema_name = session.get("schema_name")
    if not schema_name:
        raise RuntimeError("No schema_name in session — user is not logged in")
    return schema_name


def get_user_conn(read_only: bool = False):
    """
    Open a DuckDB connection scoped to the logged-in user's schema.
    Every unqualified CREATE TABLE / SELECT / etc. issued on this connection
    resolves against that schema first via SET search_path — callers don't
    need to hand-qualify table names with schema.table.

    Does NOT apply to admin.users / admin.login_activity, which stay
    hardcoded with the admin. prefix wherever they're queried.
    """
    schema_name = _current_schema_name()
    conn = duckdb.connect(DB_FILE, read_only=read_only, config=DUCKDB_CONFIG)
    if not read_only:
        _ensure_schema(conn, schema_name)
    conn.execute(f"SET search_path = '{schema_name}'")
    return conn


def _kb_manager_for_session():
    """
    The KBManager scoped to the logged-in user's schema, created lazily on
    first use. The module-level KB_MANAGER global below is only the
    boot-time default (no-schema/"main") manager — routes must NOT read it
    directly once a user is logged in, or they'll see/modify the wrong
    (or an empty) catalog.
    """
    return get_kb_manager(DB_FILE, schema_name=_current_schema_name())


def init_auth_db():
    """Create admin schema, users and login_activity tables in DuckDB."""
    try:
        conn = duckdb.connect(DB_FILE, read_only=False, config=DUCKDB_CONFIG)
        conn.execute("CREATE SCHEMA IF NOT EXISTS admin")

        # users table — id is auto increment PK
        # schema_name maps each user to their own DuckDB schema for tenant isolation.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin.users (
                id           INTEGER PRIMARY KEY,
                user_id      VARCHAR UNIQUE NOT NULL,
                password     VARCHAR NOT NULL,
                full_name    VARCHAR NOT NULL,
                schema_name  VARCHAR,
                is_active    BOOLEAN DEFAULT TRUE
            )
        """)

        # Migration for installs created before schema_name existed.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info('admin.users')").fetchall()}
        if "schema_name" not in existing_cols:
            conn.execute("ALTER TABLE admin.users ADD COLUMN schema_name VARCHAR")
            logger.info("[auth] migrated admin.users: added schema_name column")

        # login_activity table — user_ref_id is FK to admin.users.id
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin.login_activity (
                id           INTEGER PRIMARY KEY,
                user_ref_id  INTEGER NOT NULL REFERENCES admin.users(id),
                login_time   TIMESTAMP,
                logout_time  TIMESTAMP,
                session_id   VARCHAR
            )
        """)

        # Seed default admin user if not exists
        existing = conn.execute("SELECT COUNT(*) FROM admin.users WHERE user_id = 'admin'").fetchone()[0]
        if existing == 0:
            default_schema = _sanitize_schema_name("admin")
            conn.execute("""
                INSERT INTO admin.users (id, user_id, password, full_name, schema_name, is_active)
                VALUES (1, 'admin', 'admin123', 'Administrator', ?, TRUE)
            """, [default_schema])
            _ensure_schema(conn, default_schema)
            #logger.info("[auth] default admin user created — user_id=admin, password=admin123")

        # Backfill schema_name for any existing rows that predate this column
        # (or were inserted manually without one).
        missing = conn.execute(
            "SELECT id, user_id FROM admin.users WHERE schema_name IS NULL OR schema_name = ''"
        ).fetchall()
        for uid, user_id in missing:
            derived = _sanitize_schema_name(user_id)
            conn.execute("UPDATE admin.users SET schema_name = ? WHERE id = ?", [derived, uid])
            _ensure_schema(conn, derived)
            logger.info(f"[auth] backfilled schema_name for user={user_id} -> {derived}")

        conn.close()
        logger.info("[auth] auth DB initialized")
    except Exception as e:
        logger.warning(f"[auth] auth DB init failed (non-critical): {e}")


def init_kb_manager_system():
    """Initialize KB Manager at app startup and wire into poc8."""
    global KB_MANAGER
    try:
        KB_MANAGER = init_kb_manager(DB_FILE)
        poc8.set_kb_manager(KB_MANAGER)
        stats = KB_MANAGER.get_stats()
        logger.info(f"[app] KB Manager initialized: {stats}")
    except Exception as e:
        logger.warning(f"[app] KB Manager init failed (non-critical): {e}")

init_kb_manager_system()

init_auth_db()

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id") or not session.get("schema_name"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect("/")
    return send_from_directory("static", "login.html")


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body     = request.get_json(silent=True) or {}
    user_id  = (body.get("user_id") or "").strip()
    password = (body.get("password") or "").strip()
 
    if not user_id or not password:
        return jsonify({"success": False, "error": "User ID and Password are required."}), 400
 
    try:
        conn = duckdb.connect(DB_FILE, read_only=False, config=DUCKDB_CONFIG)
        row  = conn.execute(
            "SELECT id, user_id, password, full_name, schema_name, is_active FROM admin.users WHERE user_id = ?",
            [user_id]
        ).fetchone()
 
        if not row:
            conn.close()
            return jsonify({"success": False, "error": "User ID not found."}), 401
 
        db_id, db_user_id, db_password, full_name, schema_name, is_active = row
 
        if not is_active:
            conn.close()
            return jsonify({"success": False, "error": "Your account is inactive. Contact admin."}), 403
 
        if password != db_password:
            conn.close()
            return jsonify({"success": False, "error": "Incorrect password."}), 401

        # Safety net: if this row predates the schema_name column and somehow
        # wasn't caught by the init_auth_db() backfill, derive and persist it now.
        if not schema_name:
            schema_name = _sanitize_schema_name(db_user_id)
            conn.execute("UPDATE admin.users SET schema_name = ? WHERE id = ?", [schema_name, db_id])
            logger.info(f"[auth] schema_name assigned at login for user={db_user_id} -> {schema_name}")

        _ensure_schema(conn, schema_name)

        # Generate session ID
        import uuid
        from datetime import datetime as dt
        session_id = str(uuid.uuid4())
 
        # Get next login_activity id
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM admin.login_activity").fetchone()[0]
        next_id = max_id + 1
 
        # Insert login activity row
        conn.execute("""
            INSERT INTO admin.login_activity (id, user_ref_id, login_time, logout_time, session_id)
            VALUES (?, ?, ?, NULL, ?)
        """, [next_id, db_id, dt.now(), session_id])
        conn.close()
 
        # Save to session
        session["user_id"]          = db_user_id
        session["full_name"]        = full_name
        session["schema_name"]      = schema_name   # NEW — every table-creation call reads this
        session["user_ref_id"]      = db_id
        session["session_id"]       = session_id
        session["activity_row_id"]  = next_id
        # Fresh login = fresh working dataset, so an old session's active
        # dataset never leaks into a new one.
        session["dataset_id"] = None
        session["filename"]   = None
 
        logger.info(f"[auth] login success | user={db_user_id} | schema={schema_name} | activity_id={next_id}")
        return jsonify({"success": True, "full_name": full_name})
 
    except Exception as e:
        logger.error(f"[auth] login failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Server error."}), 500


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    user          = session.get("user_id", "unknown")
    activity_id   = session.get("activity_row_id")
    session_id    = session.get("session_id")
 
    # Update logout_time in login_activity
    if activity_id:
        try:
            from datetime import datetime as dt
            conn = duckdb.connect(DB_FILE, read_only=False, config=DUCKDB_CONFIG)
            conn.execute("""
                UPDATE admin.login_activity
                SET logout_time = ?
                WHERE id = ?
            """, [dt.now(), activity_id])
            conn.close()
            logger.info(f"[auth] logout recorded | user={user} | activity_id={activity_id}")
        except Exception as e:
            logger.warning(f"[auth] logout time update failed: {e}")

    if session_id:
        POC1_METADATA_CACHE.pop(session_id, None)

    session.clear()
    return jsonify({"success": True})

 
@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    if not session.get("user_id"):
        return jsonify({"logged_in": False}), 401
    return jsonify({
        "logged_in":   True,
        "user_id":     session.get("user_id"),
        "full_name":   session.get("full_name"),
        "schema_name": session.get("schema_name"),
    })



# ── helpers ───────────────────────────────────────────────────────────────────
def make_dataset_id(filename: str) -> str:
    base = os.path.splitext(filename or "")[0]
    stem = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").lower()
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
    # Scoped by schema so two users uploading same-named files never collide
    # on disk (mirrors the DuckDB-level schema isolation).
    schema_name = _current_schema_name()
    d = os.path.join(OUTPUTS_DIR, schema_name, dataset_id)
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

def _parse_join_label(label: str) -> tuple:
    text = (label or "").strip()
    if "->" in text:
        left, right = text.split("->", 1)
        return left.strip(), right.strip()
    if "<-" in text:
        right, left = text.split("<-", 1)
        return left.strip(), right.strip()
    return text, text

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
    dataset_id = session.get("dataset_id")
    if not dataset_id:
        return None
    schema_name = _current_schema_name()
    d = os.path.join(UPLOADS_DIR, schema_name, dataset_id)
    if not os.path.isdir(d):
        return None
    versions = sorted(os.listdir(d))
    return os.path.join(d, versions[-1]) if versions else None

def contract_path(dataset_id: str) -> str:
    schema_name = _current_schema_name()
    d = os.path.join(CONTRACTS_DIR, schema_name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{dataset_id}_contract.json")

def table_name(dataset_id: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", dataset_id or "").strip("_")
    if not name:
        name = "dataset"
    if not re.match(r"^[A-Za-z_]", name):
        name = f"t_{name}"
    return name


AUDIT_COLUMN_NAMES = {"system_date", "system_active", "file_path"}


def _is_audit_column_name(name: str) -> bool:
    return str(name or "").strip().lower() in AUDIT_COLUMN_NAMES


def _filter_audit_fields(schema_profile: dict) -> dict:
    if not isinstance(schema_profile, dict):
        return schema_profile
    cleaned = json.loads(json.dumps(schema_profile))
    for key in ("critical_data", "quality_concerns", "recommended_indexes"):
        value = cleaned.get(key)
        if not value:
            continue
        if isinstance(value, list):
            filtered = []
            for item in value:
                if isinstance(item, dict) and _is_audit_column_name(item.get("column")):
                    continue
                if isinstance(item, str) and any(audit in item.lower() for audit in AUDIT_COLUMN_NAMES):
                    continue
                filtered.append(item)
            cleaned[key] = filtered
    return cleaned


def strip_audit_columns(schema_profile: dict) -> dict:
    if not isinstance(schema_profile, dict):
        return schema_profile
    cleaned = json.loads(json.dumps(schema_profile))
    schema = cleaned.get("schema")
    if isinstance(schema, list):
        cleaned["schema"] = [
            col for col in schema
            if str(col.get("column", "")).strip().lower() not in AUDIT_COLUMN_NAMES
        ]
        cleaned["total_columns"] = len(cleaned["schema"])
    return cleaned


def enrich_with_bronze_datatypes(schema_profile: dict, table: str) -> dict:
    if not isinstance(schema_profile, dict):
        return schema_profile
    try:
        # Scoped to the logged-in user's schema via SET search_path, so this
        # reads table_schema_snapshot rows for THIS user's tables only.
        conn = get_user_conn(read_only=True)
        rows = conn.execute(
            """
            SELECT column_name, data_type
            FROM table_schema_snapshot
            WHERE table_name = ?
            """,
            [table],
        ).fetchall()
        conn.close()
        bronze_map = {str(row[0]): str(row[1]) for row in rows if row and row[0]}
        logger.info(f"[poc1] bronze datatypes loaded | table={table} | cols={len(bronze_map)}")
    except Exception as e:
        logger.warning(f"[poc1] bronze datatype lookup failed | table={table} | error={e}")
        return schema_profile

    enriched = json.loads(json.dumps(schema_profile))
    for col in enriched.get("schema", []) or []:
        name = col.get("column")
        bronze = bronze_map.get(name)
        if bronze:
            col["bronze_datatype"] = bronze
            col["actual_data_type"] = bronze
            if not col.get("data_type"):
                col["data_type"] = bronze
    return enriched


def remove_audit_checks_from_yaml(yaml_text: str) -> str:
    if not yaml_text:
        return yaml_text

    lines = yaml_text.splitlines()
    out = []
    skip = False
    current_indent = 0

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        lower = stripped.lower()
        mentions_audit = any(name in lower for name in AUDIT_COLUMN_NAMES)

        if stripped.startswith("- ") and mentions_audit:
            skip = True
            current_indent = indent
            continue

        if skip:
            if stripped.startswith("- ") and indent <= current_indent:
                skip = False
            elif stripped and indent > current_indent:
                continue
            else:
                skip = False

        if not skip:
            out.append(line)

    return "\n".join(out)

def maybe_refresh_kb() -> None:
    schema_name = _current_schema_name()
    if should_refresh_kb(STORAGE_DIR, schema_name=schema_name):
        try:
            result = refresh_kb_from_duckdb(DB_FILE, STORAGE_DIR, schema_name=schema_name)
            logger.info(
                f"[kb] auto refresh triggered | tables={result.get('tables_indexed', 0)} | joins={result.get('join_edges', 0)}"
            )
            # Re-wire KB Manager after refresh so poc8 sees the updated catalog
            _kb_manager_for_session().clear_cache()
        except Exception as e:
            logger.warning(f"[kb] auto refresh skipped due to error: {e}")

def _schema_map(schema_profile: dict) -> dict:
    return {
        col.get("column"): col.get("bronze_datatype") or col.get("data_type")
        for col in (schema_profile or {}).get("schema", [])
        if col.get("column")
    }


# ── contract / schema comparison helpers ─────────────────────────────────────
def compare_contract_to_schema(contract: dict, current_schema: dict) -> dict:
    contract = contract or {}
    current  = _schema_map(current_schema)

    contract_cols = set(contract.keys())
    current_cols  = set(current.keys())

    missing_columns = sorted(contract_cols - current_cols)
    extra_columns   = sorted(current_cols - contract_cols)

    type_mismatches, null_issues = [], []
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
    validation_passed    = True
    if missing_columns:
        severity = "critical"; can_pipeline_proceed = False; validation_passed = False
    elif type_mismatches:
        severity = "high";     can_pipeline_proceed = False; validation_passed = False
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
    if missing_columns:  recommended_actions.append("Add the missing contract columns or reject the upload.")
    if type_mismatches:  recommended_actions.append("Fix the data types to match the locked contract.")
    if extra_columns:    recommended_actions.append("Review whether extra columns should be added to the contract.")
    if null_issues:      recommended_actions.append("Review null-heavy contract columns before proceeding.")

    return {
        "validation_passed": validation_passed, "severity": severity,
        "can_pipeline_proceed": can_pipeline_proceed, "summary": summary,
        "missing_columns": missing_columns, "extra_columns": extra_columns,
        "type_mismatches": type_mismatches, "null_issues": null_issues,
        "recommended_actions": recommended_actions,
    }


def compare_schema_snapshots(previous_schema: dict, current_schema: dict, previous_file: str = None) -> dict:
    previous_cols = previous_schema.get("schema", []) if previous_schema else []
    current_cols  = current_schema.get("schema", []) if current_schema else []

    prev_map = {c.get("column"): c.get("data_type") for c in previous_cols if c.get("column")}
    curr_map = {c.get("column"): c.get("data_type") for c in current_cols if c.get("column")}

    prev_names = list(prev_map.keys())
    curr_names = list(curr_map.keys())

    new_columns     = sorted(set(curr_names) - set(prev_names))
    dropped_columns = sorted(set(prev_names) - set(curr_names))

    type_changes = []
    for col in sorted(set(prev_names) & set(curr_names)):
        if str(prev_map.get(col)).lower() != str(curr_map.get(col)).lower():
            type_changes.append({"column": col, "old_type": prev_map.get(col), "new_type": curr_map.get(col)})

    reordered = prev_names != curr_names and set(prev_names) == set(curr_names)

    possible_renames = []
    if len(previous_cols) == len(current_cols):
        for i, (prev_col, curr_col) in enumerate(zip(previous_cols, current_cols)):
            pname, cname = prev_col.get("column"), curr_col.get("column")
            if pname != cname and pname not in curr_names and cname not in prev_names:
                confidence = "high" if str(prev_col.get("data_type")).lower() == str(curr_col.get("data_type")).lower() else "medium"
                possible_renames.append({"position": i, "old_name": pname, "new_name": cname, "confidence": confidence})

    change_detected = bool(new_columns or dropped_columns or type_changes or reordered or possible_renames)
    summary = "No schema changes detected."
    if change_detected:
        parts = []
        if new_columns:       parts.append(f"new columns: {', '.join(new_columns)}")
        if dropped_columns:   parts.append(f"dropped columns: {', '.join(dropped_columns)}")
        if type_changes:      parts.append(f"type changes in {len(type_changes)} column(s)")
        if reordered:         parts.append("column order changed")
        if possible_renames:  parts.append(f"possible renames: {len(possible_renames)}")
        summary = "; ".join(parts).capitalize() + "."

    recommended_actions = []
    if new_columns:      recommended_actions.append("Review whether the new columns should be added to the downstream contract.")
    if dropped_columns:  recommended_actions.append("Check whether the dropped columns were intentionally removed.")
    if type_changes:     recommended_actions.append("Validate type changes before loading downstream systems.")
    if reordered:        recommended_actions.append("Confirm the reorder is expected and does not affect positional consumers.")
    if possible_renames: recommended_actions.append("Review possible renames manually before approving schema drift.")

    return {
        "change_detected": change_detected, "summary": summary,
        "new_columns": new_columns, "dropped_columns": dropped_columns,
        "possible_renames": possible_renames, "type_changes": type_changes,
        "reordered": reordered, "recommended_actions": recommended_actions,
        "compared_against": previous_file, "is_first_run": False,
    }


# ── static UI ─────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
@login_required
def status():
    ds = session.get("dataset_id")
    return jsonify({
        "csv_ready":  bool(ds and current_csv_path()),
        "poc1_ready": bool(ds and load_latest(ds, "poc1") is not None),
        "csv_file":   session.get("filename"),
        "dataset_id": ds,
    })

@app.route("/api/datasets")
@login_required
def list_datasets():
    out = []
    schema_name = _current_schema_name()
    schema_outputs_dir = os.path.join(OUTPUTS_DIR, schema_name)
    if os.path.isdir(schema_outputs_dir):
        for dataset_id in sorted(os.listdir(schema_outputs_dir)):
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
@login_required
def upload():
    filename     = "pasted.csv"
    csv_path     = None
    dataset_id   = None
    content_type = request.content_type or ""
    schema_name  = _current_schema_name()

    if content_type.startswith("application/octet-stream"):
        filename   = request.args.get("filename", filename)
        dataset_id = make_dataset_id(filename)
        udir       = os.path.join(UPLOADS_DIR, schema_name, dataset_id)
        os.makedirs(udir, exist_ok=True)
        csv_path   = os.path.join(udir, f"{ts()}.csv")
        t0 = time.time()
        with open(csv_path, "wb") as out:
            shutil.copyfileobj(request.stream, out, length=UPLOAD_COPY_CHUNK_SIZE)
        logger.info(f"upload streamed to disk in {time.time()-t0:.2f}s | file={filename}")

    elif "file" in request.files:
        f          = request.files["file"]
        filename   = f.filename or filename
        dataset_id = make_dataset_id(filename)
        udir       = os.path.join(UPLOADS_DIR, schema_name, dataset_id)
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
            udir       = os.path.join(UPLOADS_DIR, schema_name, dataset_id)
            os.makedirs(udir, exist_ok=True)
            csv_path   = os.path.join(udir, f"{ts()}.csv")
            with open(csv_path, "w") as out:
                out.write(text)

    if not csv_path:
        return jsonify({"error": "No CSV provided"}), 400

    session["dataset_id"] = dataset_id
    session["filename"]   = filename

    try:
        table = table_name(dataset_id)
        t1    = time.time()
        ingest_result = ingest_csv(
            csv_path, DB_FILE, table,
            original_filename=filename, schema_name=schema_name,
        )
        logger.info(f"DuckDB ingest completed in {time.time()-t1:.2f}s | schema={schema_name} | table={table}")

        if not ingest_result.get("success"):
            return jsonify({"error": ingest_result.get("error", "DB ingest failed")}), 500

        snapshot_result = write_table_schema_snapshot(DB_FILE, table, schema_name=schema_name)
        if not snapshot_result.get("success"):
            logger.warning(f"[schema] snapshot write failed: {snapshot_result.get('error')}")

        sample, columns, row_count = get_preview(
            DB_FILE, table, n=8, row_count=ingest_result["row_count"], schema_name=schema_name,
        )
        return jsonify({
            "dataset_id": dataset_id, "filename": filename,
            "row_count": row_count, "columns": columns,
            "sample": sample, "duckdb": ingest_result,
            "schema_snapshot": snapshot_result,
        })
    except Exception as e:
        logger.error(f"Upload/ingest failed for dataset={dataset_id}: {e}", exc_info=True)
        return jsonify({"error": f"DB error: {str(e)}"}), 500


# ── Tab 2: Tune - Schema Intelligence  ───────────────────────────────────────────
@app.route("/api/poc1/run", methods=["POST"])
@login_required
def run_poc1():
    ds       = session.get("dataset_id")
    csv_path = current_csv_path()
    if not ds or not csv_path:
        return jsonify({"error": "Upload a CSV first"}), 400
    try:
        t_start         = time.time()
        file_size_bytes = os.path.getsize(csv_path)
        tbl             = table_name(ds)
        schema_name     = _current_schema_name()
        logger.info(f"[poc1] STEP 1 - getsize done")

        metadata = get_full_metadata_for_ai(
            DB_FILE, tbl, session.get("filename"), file_size_bytes, SAMPLE_ROWS_SCHEMA,
            schema_name=schema_name,
        )
        logger.info(f"[poc1] STEP 2 - metadata fetched")
        POC1_METADATA_CACHE[session.get("session_id")] = metadata

        system_prompt, user_prompt = build_schema_discovery_prompt(metadata)
        logger.info(f"[poc1] STEP 3 - prompt built | chars={len(system_prompt)+len(user_prompt)}")

        t_ai = time.time()
        result = ask_json(user_prompt, system_prompt)
        logger.info(f"[poc1] STEP 4 - AI call done in {time.time()-t_ai:.3f}s | total={time.time()-t_start:.3f}s")

        result = _filter_audit_fields(result)
        logger.info(
            "[poc1] bronze datatypes loaded from snapshot | sample=%s",
            [(col.get("column"), col.get("data_type")) for col in (result.get("schema", []) or [])[:5]],
        )
        result["_dataset_id"] = ds
        return jsonify(result)
    except Exception as e:
        logger.error(f"Tune - Schema Intelligence, failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/poc1/approve", methods=["POST"])
@login_required
def approve_poc1():
    ds = session.get("dataset_id")
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    try:
        t_approve   = time.time()
        schema_name = _current_schema_name()
        result = request.get_json()
        if not result:
            return jsonify({"error": "No schema data received"}), 400

        t_bronze = time.time()
        result = enrich_with_bronze_datatypes(result, table_name(ds))
        logger.info(
            "[poc1] bronze_datatype in approve payload | sample=%s",
            [
                (col.get("column"), col.get("bronze_datatype"), col.get("data_type"))
                for col in (result.get("schema", []) or [])[:5]
            ],
        )
        logger.info(f"[poc1] bronze enrichment done in {time.time()-t_bronze:.3f}s")

        t_save = time.time()
        versioned_name = save_versioned(ds, "poc1", result)
        logger.info(f"[poc1] schema approved and saved | dataset={ds} | file={versioned_name}")

        cpath           = contract_path(ds)
        is_new_contract = not os.path.exists(cpath)
        if is_new_contract:
            contract = {
                col["column"]: col.get("bronze_datatype") or col["data_type"]
                for col in result.get("schema", [])
            }
            with open(cpath, "w") as f:
                json.dump(contract, f, indent=2)
            logger.info(f"[poc1] contract locked | dataset={ds}")

        # Persist to KB (function-based API for file storage)
        try:
            t_kb = time.time()
            kb_result = persist_kb(
                STORAGE_DIR, ds, result,
                table_name=table_name(ds), schema_name=schema_name,
            )
            logger.info(
                f"[kb] KB file store updated | db={DB_FILE} | schema={schema_name} | table={table_name(ds)} | "
                f"docs={kb_result.get('documents_written', 0)} | success={kb_result.get('success')}"
            )
            logger.info(f"[kb] persist_kb elapsed={time.time()-t_kb:.3f}s")
        except Exception as e:
            logger.warning(f"[kb] persist_kb failed (non-critical): {e}")

        # Invalidate KB Manager cache so next query sees the new table
        try:
            t_cache = time.time()
            _kb_manager_for_session().clear_cache()
            logger.info("[kb] KB Manager cache cleared after schema approval")
            logger.info(f"[kb] cache clear elapsed={time.time()-t_cache:.3f}s")
        except Exception as e:
            logger.warning(f"[kb] cache clear failed (non-critical): {e}")

        if os.getenv("KB_REFRESH_ON_APPROVAL", "0").lower() in {"1", "true", "yes"}:
            t_refresh = time.time()
            maybe_refresh_kb()
            logger.info(f"[kb] maybe_refresh_kb elapsed={time.time()-t_refresh:.3f}s")

        logger.info(f"[poc1] approve total elapsed={time.time()-t_approve:.3f}s")

        return jsonify({
            "success": True,
            "version_file": versioned_name,
            "contract_created": is_new_contract,
        })
    except Exception as e:
        logger.error(f"Schema approval (poc1) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/poc1/versions")
@login_required
def poc1_versions():
    ds = session.get("dataset_id")
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    return jsonify(list_versions(ds, "poc1"))


# ── Tab 3: SODA YAML (POC 7) ──────────────────────────────────────────────────
@app.route("/api/poc7/run", methods=["POST"])
@login_required
def run_poc7():
    ds = session.get("dataset_id")
    if not ds:
        return jsonify({"error": "Upload a CSV first"}), 400

    schema_profile = load_latest(ds, "poc1")
    if not schema_profile:
        return jsonify({"error": "Tune - Schema Intelligence must be approved first (Tab 2 → Approve & Save)."}), 400

    try:
        schema_profile = enrich_with_bronze_datatypes(schema_profile, table_name(ds))
        schema_profile = strip_audit_columns(schema_profile)
        schema_json = json.dumps(schema_profile, indent=2)
        prompt = poc7.PROMPT.replace("{table}", table_name(ds)).replace("{schema_profile}", schema_json)
        logger.info(f"[poc7] using approved poc1_latest.json | dataset={ds}")

        yaml_output = ask(prompt, poc7.SYSTEM).strip()
        if yaml_output.startswith("```"):
            yaml_output = "\n".join(yaml_output.split("\n")[1:])
        if yaml_output.endswith("```"):
            yaml_output = "\n".join(yaml_output.split("\n")[:-1])
        yaml_output = yaml_output.strip()
        yaml_output = remove_audit_checks_from_yaml(yaml_output)

        check_count = sum(
            1 for line in yaml_output.split("\n")
            if line.strip().startswith("- ") and not line.strip().startswith("- value")
        )
        logger.info(f"[poc7] YAML generated | checks={check_count}")
        return jsonify({"yaml": yaml_output, "check_count": check_count, "dataset_id": ds})
    except Exception as e:
        logger.error(f"DQ YAML generation (poc7) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/poc7/approve", methods=["POST"])
@login_required
def approve_poc7():
    ds = session.get("dataset_id")
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    try:
        body = request.get_json()
        if not body or "yaml" not in body:
            return jsonify({"error": "No YAML data received"}), 400

        yaml_text = body["yaml"].strip()
        if not yaml_text:
            return jsonify({"error": "YAML content is empty"}), 400

        repaired_yaml = sanitize_soda_yaml_text(yaml_text)
        try:
            yaml.safe_load(repaired_yaml)
        except Exception as e:
            logger.warning(f"[poc7] YAML still invalid after sanitize; saving original: {e}")
            repaired_yaml = yaml_text

        versioned_name = save_versioned(ds, "soda", repaired_yaml, ext="yaml")
        logger.info(f"[poc7] YAML approved and saved | dataset={ds} | file={versioned_name}")

        check_count = sum(
            1 for line in yaml_text.split("\n")
            if line.strip().startswith("- ") and not line.strip().startswith("- value")
        )
        return jsonify({"success": True, "version_file": versioned_name, "check_count": check_count})
    except Exception as e:
        logger.error(f"YAML approval (poc7) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 4: Data Quality (real SODA checks via DuckDB) ────────────────────────
@app.route("/api/poc2/run", methods=["POST"])
@login_required
def run_poc2():
    ds = session.get("dataset_id")
    if not ds or not current_csv_path():
        return jsonify({"error": "Upload a CSV first"}), 400

    yaml_path = os.path.join(dataset_dir(ds), "soda_latest.yaml")
    if not os.path.exists(yaml_path):
        return jsonify({"error": "Generate and approve Quality Checks YAML (Tab 3) first"}), 400

    try:
        tbl         = table_name(ds)
        schema_name = _current_schema_name()
        result = run_soda_checks_from_yaml(DB_FILE, yaml_path, tbl, schema_name=schema_name)
        result["_dataset_id"] = ds
        save_versioned(ds, "poc2", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Data quality check run (poc2) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 5: Schema Validation (POC 3a) ────────────────────────────────────────
@app.route("/api/poc3a/run", methods=["POST"])
@login_required
def run_poc3a():
    ds          = session.get("dataset_id")
    poc1_schema = ds and load_latest(ds, "poc1")
    if not poc1_schema:
        return jsonify({"error": "Play Tune (Tab 2) first"}), 400
    try:
        poc1_schema = enrich_with_bronze_datatypes(poc1_schema, table_name(ds))
        cpath = contract_path(ds)
        if not os.path.exists(cpath):
            contract = {
                col["column"]: col.get("bronze_datatype") or col["data_type"]
                for col in poc1_schema.get("schema", [])
            }
            with open(cpath, "w") as f:
                json.dump(contract, f, indent=2)
        with open(cpath) as f:
            contract = json.load(f)

        result                  = compare_contract_to_schema(contract, poc1_schema)
        result["_dataset_id"]   = ds
        result["_contract_file"] = os.path.basename(cpath)
        save_versioned(ds, "poc3a", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Schema validation (poc3a) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 6: Schema Change Discovery (POC 3b) ──────────────────────────────────
@app.route("/api/poc3b/run", methods=["POST"])
@login_required
def run_poc3b():
    ds             = session.get("dataset_id")
    current_schema = ds and load_latest(ds, "poc1")
    if not current_schema:
        return jsonify({"error": "Play Tune (Tab 2) first"}), 400
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

        result = compare_schema_snapshots(previous_schema, current_schema, previous_file=previous_file)
        result["_dataset_id"] = ds
        save_versioned(ds, "poc3b", result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Schema change detection (poc3b) failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Tab 7 / Tab 8: NL → SQL Query Builder (POC 8) ────────────────────────────
@app.route("/api/poc8/query", methods=["POST"])
@login_required
def run_poc8_query():
    ds          = session.get("dataset_id")
    schema_name = _current_schema_name()
    body     = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Please enter a question"}), 400

    try:
        try:
            _kb_manager_for_session().load_catalog(force_refresh=False)
        except Exception as e:
            logger.warning(f"[poc8] KB catalog warm-up skipped: {e}")
        result = poc8.question_to_sql(
            db_file=DB_FILE,
            ds=ds,
            storage_dir=STORAGE_DIR,
            question=question,
            ask_json_fn=ask_json,
            schema_name=schema_name,
        )

        if result.get("ok") and result.get("sql"):
            # Save successful query as an example pair
            try:
                if result.get("tables"):
                    upsert_example_pair(
                        STORAGE_DIR, ds or "kb", question,
                        result.get("sql", ""),
                        result.get("tables", []) or [],
                        tags=["poc8", "tab8", "auto_saved"],
                        schema_name=schema_name,
                    )
            except Exception as e:
                logger.warning(f"[poc8] example pair save skipped: {e}")

            # Save join edges discovered during query
            try:
                join_edges = result.get("join_paths", []) or []
                if join_edges:
                    normalized = []
                    for edge in join_edges:
                        left_col, right_col = _parse_join_label(str(edge.get("join_column") or ""))
                        normalized.append({
                            "left_table":  edge.get("left"),
                            "left_column": left_col,
                            "right_table": edge.get("right"),
                            "right_column": right_col,
                            "confidence": float(edge.get("confidence") or 0.5),
                            "reason":  "from_tab8_query",
                            "source":  "tab8",
                        })
                    upsert_join_edges(STORAGE_DIR, normalized, schema_name=schema_name)
            except Exception as e:
                logger.warning(f"[poc8] join edge save skipped: {e}")

        result["_dataset_id"] = ds or "kb"
        return jsonify(result)
    except Exception as e:
        logger.error(f"NL→SQL query generation failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── KB routes ─────────────────────────────────────────────────────────────────

@app.route("/api/kb/status", methods=["GET"])
@login_required
def kb_status():
    """Get KB Manager status and statistics."""
    try:
        stats = _kb_manager_for_session().get_stats()
        return jsonify({"success": True, "status": "✅ KB Ready", "stats": stats})
    except Exception as e:
        logger.error(f"KB status check failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/kb/stats", methods=["GET"])
@login_required
def kb_stats():
    """Get KB statistics (alias for /api/kb/status)."""
    return kb_status()


@app.route("/api/kb/catalog", methods=["GET"])
@login_required
def kb_catalog_route():
    """Get the full catalog of registered tables."""
    try:
        catalog = _kb_manager_for_session().load_catalog(force_refresh=True)
        return jsonify({
            "success": True,
            "tables": list(catalog.keys()),
            "count": len(catalog),
            "catalog": catalog,
        })
    except Exception as e:
        logger.error(f"[kb] catalog fetch failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/kb/join-graph", methods=["GET"])
@login_required
def kb_join_graph():
    """Get the join graph."""
    try:
        joins = _kb_manager_for_session().load_joins(force_refresh=True)
        edges = []
        for src, targets in joins.items():
            for tgt, col in targets:
                edges.append({"from": src, "to": tgt, "column": col})
        return jsonify({"success": True, "edges": edges, "count": len(edges)})
    except Exception as e:
        logger.error(f"[kb] join graph fetch failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/kb/refresh", methods=["POST"])
@login_required
def refresh_kb_route():
    """Force a full KB refresh from DuckDB."""
    ds = session.get("dataset_id")
    try:
        schema_name = _current_schema_name()
        result = refresh_kb_from_duckdb(DB_FILE, STORAGE_DIR, schema_name=schema_name)
        get_kb_manager(DB_FILE, schema_name=schema_name).clear_cache()
        result["_dataset_id"] = ds
        return jsonify(result)
    except Exception as e:
        logger.error(f"KB refresh failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/kb/build", methods=["POST"])
@login_required
def kb_build():
    """
    Register the current dataset's approved schema into the KB.
    Called automatically after Tab 2 approval (also callable manually).
    """
    ds = session.get("dataset_id")
    if not ds:
        return jsonify({"error": "No active dataset"}), 400
    try:
        schema_name = _current_schema_name()
        poc1_schema = load_latest(ds, "poc1")
        if not poc1_schema:
            return jsonify({"error": "No approved schema found — complete Tab 2 first"}), 400

        result = persist_kb(
            STORAGE_DIR, ds, poc1_schema,
            table_name=table_name(ds), schema_name=schema_name,
        )
        get_kb_manager(DB_FILE, schema_name=schema_name).clear_cache()
        logger.info(f"[kb] manual KB build done | dataset={ds} | docs={result.get('documents_written', 0)}")
        return jsonify({"success": True, **result})
    except Exception as e:
        logger.error(f"[kb] build failed for dataset={ds}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
