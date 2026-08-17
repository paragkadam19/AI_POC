"""
KB_MANAGER: Knowledge Base with Vector Semantic Search
=======================================================
Updated for multi-tenancy: KBManager now accepts a `user_schema`
parameter. All kb_metadata tables are
created inside the user's own schema (e.g. parag.kb_metadata).
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb

from logger_config import get_logger
from duckdb_helper import SAMPLE_ROWS_SCHEMA, DUCKDB_CONFIG, get_full_metadata_for_ai
from prompt_builder import build_schema_discovery_prompt
from bedrock_client import ask_json

logger = get_logger(__name__)

VECTOR_DIM           = 1024
_KB_STATE_FILE       = "kb_state.json"
_SEMANTIC_LAYER_FILE = "semantic_layer.md"
_DB_FILE_REF: list   = [None]
_KB_MANAGER_INSTANCE: Optional["KBManager"] = None


def _connect(db_file: str, read_only: bool = False, schema: str = None):
    from duckdb_helper import _connect as _dh_connect
    return _dh_connect(db_file, read_only=read_only, schema=schema)


def _embed(text: str) -> List[float]:
    try:
        from bedrock_client import embed_text
        vec = embed_text(text)
        if vec and len(vec) == VECTOR_DIM:
            return vec
        return []
    except Exception as e:
        logger.warning(f"[embed] Bedrock failed: {e}")
        return []


def _pad(vector: List[float], size: int = VECTOR_DIM) -> List[float]:
    vec = [float(v) for v in (vector or [])]
    if len(vec) < size:
        vec += [0.0] * (size - len(vec))
    return vec[:size]


def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
    if not v1 or not v2:
        return 0.0
    dot  = sum(a * b for a, b in zip(v1, v2))
    mag1 = sum(a * a for a in v1) ** 0.5
    mag2 = sum(b * b for b in v2) ** 0.5
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


def _normalize_join_name(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"(_id|_code|_key|_nm|_name)$", "", name)
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name


def _parse_sample_values(raw: str) -> List[str]:
    if not raw:
        return []
    parts = str(raw)
    for sep in (" • ", " · ", " | "):
        if sep in parts:
            return [v.strip() for v in parts.split(sep) if v and str(v).strip()]
    return [parts.strip()] if parts.strip() else []



def _is_key_like_column(col_name: str, column_type: str = "") -> bool:
    name  = (col_name or "").strip().lower()
    ctype = (column_type or "").strip().lower()
    if not name:
        return False
    if name in {"system_date", "system_active", "file_path", "sys_date", "sys_active"}:
        return False
    if name in {"id", "code", "key"}:
        return True
    if name.endswith(("_id", "_code", "_key")):
        return True
    if any(tok in name for tok in ("id", "code", "key", "customer", "product", "region", "country", "type", "status")):
        return True
    return ctype in {"varchar", "text", "uuid", "bigint", "integer", "int", "ubigint"}


def _detect_join_candidates_from_kb(conn, kb_table: str) -> List[dict]:
    rows = conn.execute(f"""
        SELECT table_name, column_name, metadata_json
        FROM {kb_table}
        WHERE record_type = 'column'
        ORDER BY table_name, column_name
    """).fetchall()

    by_table: Dict[str, List[dict]] = defaultdict(list)
    for table_name, column_name, metadata_json in rows:
        meta = json.loads(metadata_json or "{}")
        by_table[str(table_name)].append({
            "table_name":   str(table_name),
            "column_name":  str(column_name or ""),
            "column_type":  str(meta.get("column_type") or ""),
            "sample_values": meta.get("sample_values") or [],
        })

    candidates = []
    tables = sorted(by_table.keys())
    for i, left_table in enumerate(tables):
        left_cols = by_table[left_table]
        for right_table in tables[i + 1:]:
            right_cols = by_table[right_table]
            for lc in left_cols:
                if not _is_key_like_column(lc["column_name"], lc["column_type"]):
                    continue
                left_name_norm = _normalize_join_name(lc["column_name"])
                left_samples   = set(v.lower() for v in lc["sample_values"] if v)
                for rc in right_cols:
                    if not _is_key_like_column(rc["column_name"], rc["column_type"]):
                        continue
                    right_name_norm  = _normalize_join_name(rc["column_name"])
                    name_match       = bool(left_name_norm and left_name_norm == right_name_norm)
                    exact_name_match = lc["column_name"].strip().lower() == rc["column_name"].strip().lower()
                    right_samples    = set(v.lower() for v in rc["sample_values"] if v)
                    shared           = left_samples & right_samples
                    union            = left_samples | right_samples
                    overlap          = (len(shared) / len(union)) if union else 0.0
                    if not (name_match or overlap >= 0.15):
                        continue
                    confidence = round(min(0.95, max(0.55, overlap if overlap > 0 else 0.55)), 3)
                    for t1, c1, t2, c2 in [(left_table, lc["column_name"], right_table, rc["column_name"]),
                                            (right_table, rc["column_name"], left_table, lc["column_name"])]:
                        candidates.append({
                            "table1": t1, "column1": c1, "table2": t2, "column2": c2,
                            "confidence": confidence, "source": "kb_profile",
                            "exact_name_match": exact_name_match,
                            "normalized_name_match": name_match,
                            "sample_value_overlap": round(overlap, 3),
                            "shared_values": sorted(list(shared))[:5],
                        })

    dedup = {}
    for edge in candidates:
        key  = (edge["table1"], edge["column1"], edge["table2"], edge["column2"], edge["source"])
        prev = dedup.get(key)
        if not prev or float(edge["confidence"]) > float(prev["confidence"]):
            dedup[key] = edge
    return sorted(dedup.values(),
                  key=lambda e: (e.get("exact_name_match", False), e.get("sample_value_overlap", 0.0), e.get("confidence", 0.0)),
                  reverse=True)


def _load_real_table_samples(conn, tables: List[str], user_schema: str = None, sample_limit: int = 200) -> Dict[str, Dict[str, dict]]:
    if not tables:
        return {}
    result: Dict[str, Dict[str, dict]] = {}
    try:
        for table_name in tables:
            qtable = f"{user_schema}.{table_name}" if user_schema else f'"{table_name}"'
            try:
                cols = conn.execute(f'DESCRIBE {qtable}').fetchall()
            except Exception:
                continue
            table_map: Dict[str, dict] = {}
            for col_name, col_type, *_ in cols:
                if not _is_key_like_column(col_name, col_type):
                    continue
                try:
                    rows = conn.execute(
                        f'SELECT DISTINCT CAST("{col_name}" AS VARCHAR) AS v '
                        f'FROM {qtable} WHERE "{col_name}" IS NOT NULL LIMIT {sample_limit}'
                    ).fetchall()
                    values = [str(r[0]).strip() for r in rows if r and r[0] is not None and str(r[0]).strip()]
                except Exception:
                    values = []
                table_map[col_name] = {"column_type": col_type, "sample_values": values}
            if table_map:
                result[table_name] = table_map
        return result
    finally:
        conn.close()


def _detect_join_candidates_from_real_data(db_file: str, tables: List[str], user_schema: str = None) -> List[dict]:
    sampled = _load_real_table_samples(_connect(db_file), tables, user_schema=user_schema)
    if not sampled:
        return []
    candidates = []
    table_names = sorted(sampled.keys())
    for i, left_table in enumerate(table_names):
        left_cols = sampled[left_table]
        for right_table in table_names[i + 1:]:
            right_cols = sampled[right_table]
            for left_col, left_meta in left_cols.items():
                left_norm   = _normalize_join_name(left_col)
                left_values = set(v.lower() for v in left_meta.get("sample_values", []) if v)
                for right_col, right_meta in right_cols.items():
                    right_norm       = _normalize_join_name(right_col)
                    name_match       = bool(left_norm and left_norm == right_norm)
                    exact_name_match = left_col.strip().lower() == right_col.strip().lower()
                    right_values     = set(v.lower() for v in right_meta.get("sample_values", []) if v)
                    if not left_values or not right_values:
                        continue
                    shared    = left_values & right_values
                    union     = left_values | right_values
                    overlap   = (len(shared) / len(union)) if union else 0.0
                    if not (name_match or overlap >= 0.15):
                        continue
                    confidence = round(min(0.98, max(0.6, overlap if overlap > 0 else 0.6)), 3)
                    for t1, c1, t2, c2 in [(left_table, left_col, right_table, right_col),
                                            (right_table, right_col, left_table, left_col)]:
                        candidates.append({
                            "table1": t1, "column1": c1, "table2": t2, "column2": c2,
                            "confidence": confidence, "source": "real_data",
                            "exact_name_match": exact_name_match,
                            "normalized_name_match": name_match,
                            "sample_value_overlap": round(overlap, 3),
                            "shared_values": sorted(list(shared))[:5],
                        })
    dedup = {}
    for edge in candidates:
        key  = (edge["table1"], edge["column1"], edge["table2"], edge["column2"], edge["source"])
        prev = dedup.get(key)
        if not prev or float(edge["confidence"]) > float(prev["confidence"]):
            dedup[key] = edge
    return sorted(dedup.values(),
                  key=lambda e: (e.get("exact_name_match", False), e.get("sample_value_overlap", 0.0), e.get("confidence", 0.0)),
                  reverse=True)


def _refresh_joins_from_real_data(db_file: str, conn, tables: List[str], kb_table: str, user_schema: str = None) -> int:
    candidates = _detect_join_candidates_from_real_data(db_file, tables, user_schema=user_schema)
    if not candidates:
        return 0
    conn.execute(f"DELETE FROM {kb_table} WHERE record_type = 'join' AND json_extract_string(metadata_json, '$.source') = 'real_data'")
    for e in candidates:
        _upsert_metadata_record(conn, kb_table, {
            "id": f"join:real_data:{e['table1']}:{e['column1']}:{e['table2']}:{e['column2']}",
            "record_type": "join",
            "table_name": e["table1"], "related_table": e["table2"],
            "column_name": e["column1"],
            "content_text": f"{e['table1']}.{e['column1']} -> {e['table2']}.{e['column2']}",
            "metadata_json": e, "confidence": float(e["confidence"]),
        })
    return len(candidates)


def _infer_table_grain(columns: List[dict]) -> str:
    col_names = [str(c.get("column_name") or "").lower() for c in columns]
    if any("date" in n or "time" in n for n in col_names):
        return "Likely one row per entity per time period"
    if any(n.endswith(("_id", "_key")) or n == "id" for n in col_names):
        return "Likely one row per business entity"
    return "Grain not explicitly confirmed; likely one row per record"


def _infer_primary_key_candidate(columns: List[dict]) -> str:
    col_names = [str(c.get("column_name") or c.get("column") or "").lower() for c in columns]
    for name in col_names:
        if name == "id" or name.endswith("_id") or name.endswith("_key"):
            return name
    return "No obvious PK candidate"


def _infer_temporal_notes(columns: List[dict]) -> List[str]:
    notes     = []
    col_names = [str(c.get("column_name") or c.get("column") or "").lower() for c in columns]
    if any("effective" in n for n in col_names):
        notes.append("Contains effective-date style columns; prefer latest row by effective date when deduplicating.")
    if any(n in {"system_date", "created_at", "updated_at"} or "date" in n or "time" in n for n in col_names):
        notes.append("Contains temporal columns; use date filters for as-of queries when relevant.")
    if not notes:
        notes.append("No strong temporal pattern detected.")
    return notes


def build_semantic_layer_markdown(catalog: Dict[str, Any], joins: Dict[str, List[Tuple[str, str]]]) -> str:
    audit_cols = {"system_date", "system_active", "file_path", "sys_date", "sys_active"}
    lines = ["## 1. Table Registry"]
    for table_name in sorted(catalog.keys()):
        meta = catalog[table_name]
        cols = meta.get("columns", [])
        lines += [
            f"### {table_name}",
            f"- Description: {meta.get('description') or table_name}",
            f"- Row count: {meta.get('row_count', 0)}",
            f"- Column count: {len(cols)}",
            f"- Grain: {_infer_table_grain(cols)}",
            f"- PK candidate: {_infer_primary_key_candidate(cols)}",
            "- Columns:",
        ]
        for col in cols:
            cname = col.get("column", "")
            if cname.lower() in audit_cols:
                continue
            parts = [f"{cname} ({col.get('data_type', '')})"]
            if col.get("column_description"):
                parts.append(f"description: {col['column_description']}")
            if col.get("business_meaning"):
                parts.append(f"meaning: {col['business_meaning']}")
            ex = ", ".join(str(v) for v in (col.get("sample_values") or [])[:1] if v is not None)
            if ex:
                parts.append(f"example: {ex}")
            parts.append(f"nullable: {col.get('nullable', True)}")
            lines.append("  - " + "; ".join(parts))
        for note in _infer_temporal_notes(cols):
            lines.append(f"  - {note}")
    lines += ["", "## 2. Join Registry"]
    if not joins:
        lines.append("- No join relationships detected yet.")
    else:
        seen = set()
        for src, targets in sorted(joins.items()):
            for tgt, join_col in targets:
                if str(join_col).lower() in audit_cols:
                    continue
                key = (src, tgt, join_col)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"- {src} -> {tgt} on {join_col}")
    lines += ["", "## 3. Temporal Matching Rules"]
    temporal_rules = []
    for table_name in sorted(catalog.keys()):
        cols      = catalog[table_name].get("columns", [])
        col_names = [str(c.get("column") or "").lower() for c in cols]
        if any("effective" in n for n in col_names):
            temporal_rules.append(f"- {table_name}: prefer the row with the latest effective date.")
        elif any(n in {"system_date", "created_at", "updated_at"} or "date" in n or "time" in n for n in col_names):
            temporal_rules.append(f"- {table_name}: use temporal columns for date-filtered queries.")
    lines += temporal_rules if temporal_rules else ["- No explicit temporal matching rules detected."]
    return "\n".join(lines).strip() + "\n"


def _merge_semantic_layer_markdown(existing: str, catalog: Dict[str, Any],
                                    joins: Dict[str, List[Tuple[str, str]]],
                                    tables: Optional[List[str]] = None) -> str:
    if not existing.strip():
        return build_semantic_layer_markdown(catalog, joins)
    tables        = tables or list(catalog.keys())
    existing_lower = existing.lower()
    append_sections = []
    for table_name in sorted(set(tables)):
        if f"### {table_name}".lower() in existing_lower:
            continue
        meta = catalog.get(table_name)
        if not meta:
            continue
        cols  = meta.get("columns", [])
        lines = [f"### {table_name}",
                 f"- Description: {meta.get('description') or table_name}",
                 f"- Row count: {meta.get('row_count', 0)}",
                 "- Columns:"]
        for col in cols:
            lines.append(f"  - {col.get('column', '')} ({col.get('data_type', '')})")
        append_sections.append("\n".join(lines))
    if not append_sections:
        return existing
    return existing.rstrip() + "\n\n## 1b. Incremental Updates\n" + "\n\n".join(append_sections) + "\n"


def _merge_kb_state(storage_dir: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    state = load_kb_state(storage_dir)
    state.update(updates or {})
    prev_tables = state.get("tables") or []
    new_tables  = updates.get("tables") or []
    state["tables"] = sorted(set(prev_tables) | set(new_tables))
    for key in ("enriched_tables", "skipped_tables"):
        if key in updates:
            state[key] = sorted(set((state.get(key) or []) + (updates.get(key) or [])))
    return state


def _save_semantic_layer(storage_dir: str, markdown: str):
    d = os.path.join(storage_dir, "kb")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, _SEMANTIC_LAYER_FILE), "w") as f:
        f.write(markdown)


def load_semantic_layer(storage_dir: str) -> str:
    path = os.path.join(storage_dir, "kb", _SEMANTIC_LAYER_FILE)
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def load_kb_state(storage_dir: str) -> dict:
    path = _kb_state_path(storage_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _upsert_metadata_record(conn, kb_table: str, record: dict):
    """Upsert a record into the specified kb_table."""
    record_id                 = record["id"]
    record_type               = record["record_type"]
    table_name                = record.get("table_name")
    related_table             = record.get("related_table")
    column_name               = record.get("column_name")
    column_description        = record.get("column_description")
    detailed_business_meaning = record.get("detailed_business_meaning")
    column_example            = record.get("column_example")
    metadata_obj              = record.get("metadata_json") or {}
    bronze_datatype           = record.get("bronze_datatype") or metadata_obj.get("bronze_datatype")
    content_text              = record.get("content_text")
    metadata_json             = json.dumps(metadata_obj, default=str)
    confidence                = record.get("confidence")
    category                  = record.get("category")
    question                  = record.get("question")
    sql                       = record.get("sql")
    now                       = datetime.now().isoformat()
    embedding                 = record.get("embedding")

    conn.execute(f"DELETE FROM {kb_table} WHERE id = ?", [record_id])
    conn.execute(
        f"""
        INSERT INTO {kb_table}
        (id, record_type, table_name, related_table, column_name, column_description,
         detailed_business_meaning, column_example, bronze_datatype, content_text, metadata_json,
         confidence, category, question, sql, created_at, updated_at, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [record_id, record_type, table_name, related_table, column_name,
         column_description, detailed_business_meaning, column_example,
         bronze_datatype, content_text, metadata_json, confidence,
         category, question, sql, now, now, embedding],
    )


def _refresh_joins_from_kb(conn, kb_table: str) -> int:
    candidates = _detect_join_candidates_from_kb(conn, kb_table)
    if not candidates:
        return 0
    conn.execute(f"DELETE FROM {kb_table} WHERE record_type = 'join' AND json_extract_string(metadata_json, '$.source') = 'kb_profile'")
    for e in candidates:
        _upsert_metadata_record(conn, kb_table, {
            "id": f"join:kb_profile:{e['table1']}:{e['column1']}:{e['table2']}:{e['column2']}",
            "record_type": "join",
            "table_name": e["table1"], "related_table": e["table2"],
            "column_name": e["column1"],
            "content_text": f"{e['table1']}.{e['column1']} -> {e['table2']}.{e['column2']}",
            "metadata_json": e, "confidence": float(e["confidence"]),
        })
    return len(candidates)


# ============================================================================
# KBManager — per-user instance
# ============================================================================

class KBManager:
    """Manages KB tables inside data_resonance.duckdb, scoped to user_schema."""

    def __init__(self, db_file: str, user_schema: str = None):
        self.db_file        = db_file
        self.user_schema    = user_schema  # e.g. "parag"
        self.kb_table       = f"{user_schema}.kb_metadata" if user_schema else "kb_metadata"
        self.catalog_cache  = None
        self.examples_cache = None
        self.glossary_cache = None
        self.joins_cache    = None
        self._vss_available = None

        self._ensure_kb_schema()
        logger.info(f"[KB] KBManager initialized (db={db_file}, schema={user_schema})")

    def _ensure_kb_schema(self):
        try:
            conn = duckdb.connect(self.db_file, config=DUCKDB_CONFIG)

            # Ensure user schema exists
            if self.user_schema:
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.user_schema}")

            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.kb_table} (
                    id              TEXT PRIMARY KEY,
                    record_type     TEXT,
                    table_name      TEXT,
                    related_table   TEXT,
                    column_name     TEXT,
                    column_description TEXT,
                    detailed_business_meaning TEXT,
                    column_example  TEXT,
                    bronze_datatype TEXT,
                    content_text    TEXT,
                    metadata_json   TEXT,
                    confidence      DOUBLE,
                    category        TEXT,
                    question        TEXT,
                    sql             TEXT,
                    created_at      TEXT,
                    updated_at      TEXT,
                    embedding       FLOAT[{VECTOR_DIM}]
                )
            """)

            try:
                conn.execute(f"ALTER TABLE {self.kb_table} ADD COLUMN IF NOT EXISTS bronze_datatype TEXT")
            except Exception:
                pass

            try:
                conn.execute("INSTALL vss; LOAD vss;")
                conn.execute("SET hnsw_enable_experimental_persistence = true;")
                index_name = f"kb_metadata_hnsw_{self.user_schema or 'main'}"
                conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS {index_name}
                    ON {self.kb_table} USING HNSW (embedding)
                    WITH (metric = 'cosine')
                """)
                self._vss_available = True
                logger.info(f"[KB] VSS HNSW index enabled for schema={self.user_schema}")
            except Exception as vss_err:
                self._vss_available = False
                logger.info(f"[KB] VSS unavailable: {vss_err}")

            conn.close()
            logger.info(f"[KB] Schema verified/created for user={self.user_schema}")
        except Exception as e:
            logger.error(f"[KB] Schema creation failed: {e}")

    def _vss_ok(self) -> bool:
        if self._vss_available is None:
            try:
                conn = _connect(self.db_file, read_only=True)
                conn.execute("LOAD vss;")
                conn.close()
                self._vss_available = True
            except Exception:
                self._vss_available = False
        return bool(self._vss_available)

    def _upsert_vector(self, conn, doc_id: str, table_name: str, doc_type: str, text: str):
        vector_id = doc_id if str(doc_id).startswith("vector:") else f"vector:{doc_id}"
        vector    = _embed(text)
        conn.execute(f"DELETE FROM {self.kb_table} WHERE id = ?", [vector_id])
        conn.execute(
            f"INSERT INTO {self.kb_table} "
            f"(id, record_type, table_name, related_table, column_name, content_text, metadata_json, confidence, category, question, sql, created_at, updated_at, embedding) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [vector_id, "vector", table_name, None, doc_type, text, None, None, None, None, None,
             datetime.now().isoformat(), datetime.now().isoformat(), _pad(vector)],
        )

    def retrieve_by_vector(self, question: str, top_k: int = 15) -> List[Dict[str, Any]]:
        vss_results     = []
        keyword_results = []

        if self._vss_ok():
            try:
                vss_results = self._retrieve_vss(question, top_k)
            except Exception as e:
                logger.warning(f"[KB] VSS query failed: {e}")

        try:
            keyword_results = self._retrieve_keyword(question, top_k)
        except Exception as e:
            logger.warning(f"[KB] keyword query failed: {e}")

        if not vss_results and not keyword_results:
            return []

        blended: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for doc in vss_results:
            key = (str(doc.get("table_name") or ""), str(doc.get("text") or ""))
            if not key[0]:
                continue
            blended[key] = dict(doc)
            blended[key]["score"]          = float(doc.get("score", 0.0) or 0.0) * 0.7
            blended[key]["_vss_score"]     = float(doc.get("score", 0.0) or 0.0)
            blended[key]["_keyword_score"] = 0.0

        for doc in keyword_results:
            key   = (str(doc.get("table_name") or ""), str(doc.get("text") or ""))
            score = float(doc.get("score", 0.0) or 0.0) * 0.3
            if not key[0]:
                continue
            if key in blended:
                blended[key]["score"]          = float(blended[key].get("score", 0.0) or 0.0) + score
                blended[key]["_keyword_score"] = float(doc.get("score", 0.0) or 0.0)
            else:
                blended[key] = dict(doc)
                blended[key]["score"]          = score
                blended[key]["_vss_score"]     = 0.0
                blended[key]["_keyword_score"] = float(doc.get("score", 0.0) or 0.0)

        results = sorted(blended.values(), key=lambda d: float(d.get("score", 0.0) or 0.0), reverse=True)
        return results[:top_k]

    def _retrieve_vss(self, question: str, top_k: int) -> List[Dict[str, Any]]:
        q_vec = _embed(question)
        if not q_vec:
            return []
        conn = _connect(self.db_file, read_only=True)
        try:
            conn.execute("LOAD vss;")
            rows = conn.execute(f"""
                SELECT id, table_name, record_type, content_text,
                       array_cosine_distance(embedding, CAST(? AS FLOAT[{VECTOR_DIM}])) AS dist
                FROM {self.kb_table}
                WHERE embedding IS NOT NULL
                ORDER BY dist
                LIMIT ?
            """, [_pad(q_vec), top_k]).fetchall()
            conn.close()
            return [
                {"id": r[0], "table_name": r[1], "doc_type": r[2],
                 "text": r[3], "score": max(0.0, 1.0 - float(r[4]))}
                for r in rows if r
            ]
        except Exception as e:
            logger.error(f"[KB] VSS query failed: {e}")
            return []

    def _retrieve_keyword(self, question: str, top_k: int) -> List[Dict[str, Any]]:
        try:
            conn  = _connect(self.db_file, read_only=True)
            rows  = conn.execute(
                f"SELECT id, table_name, record_type, content_text FROM {self.kb_table} WHERE content_text IS NOT NULL LIMIT 500"
            ).fetchall()
            conn.close()
            if not rows:
                return []
            q_tokens = set(question.lower().split())
            scores   = []
            for doc_id, tname, dtype, text in rows:
                doc_tokens = set((text or "").lower().split())
                overlap    = len(q_tokens & doc_tokens)
                if overlap > 0:
                    score = overlap / max(len(q_tokens), len(doc_tokens))
                    scores.append((score, doc_id, tname, dtype, text))
            scores.sort(reverse=True)
            return [
                {"id": s[1], "table_name": s[2], "doc_type": s[3], "text": s[4], "score": float(s[0])}
                for s in scores[:top_k]
            ]
        except Exception as e:
            logger.error(f"[KB] Keyword fallback failed: {e}")
            return []

    def load_catalog(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self.catalog_cache and not force_refresh:
            return self.catalog_cache
        try:
            conn = _connect(self.db_file, read_only=True)
            rows = conn.execute(f"""
                SELECT table_name, content_text, metadata_json, created_at
                FROM {self.kb_table}
                WHERE record_type = 'table'
                ORDER BY table_name
            """).fetchall()
            catalog = {}
            for tname, content_text, meta_json, cat in rows:
                meta     = json.loads(meta_json or "{}")
                col_rows = conn.execute(f"""
                    SELECT column_name, metadata_json
                    FROM {self.kb_table}
                    WHERE record_type = 'column' AND table_name = ?
                    ORDER BY created_at
                """, [tname]).fetchall()
                catalog[tname] = {
                    "table_name":  tname,
                    "description": meta.get("description") or content_text or tname,
                    "row_count":   meta.get("row_count", 0),
                    "columns": [
                        {
                            "column":           cn,
                            "data_type":        (json.loads(mj or "{}").get("bronze_datatype", "") or json.loads(mj or "{}").get("column_type", "") or json.loads(mj or "{}").get("data_type", "")) if mj else "",
                            "bronze_datatype":  json.loads(mj or "{}").get("bronze_datatype", "") if mj else "",
                            "business_meaning": json.loads(mj or "{}").get("business_meaning", "") if mj else "",
                            "nullable":         json.loads(mj or "{}").get("nullable", True) if mj else True,
                            "sample_values":    json.loads(mj or "{}").get("sample_values", []) if mj else [],
                        }
                        for cn, mj in col_rows
                    ],
                    "created_at": cat,
                }
            conn.close()
            self.catalog_cache = catalog
            logger.info(f"[KB] Catalog loaded: {len(catalog)} tables (schema={self.user_schema})")
            return catalog
        except Exception as e:
            logger.error(f"[KB] load_catalog failed: {e}")
            return {}

    def load_examples(self, min_quality: float = 0.80, limit: int = 5,
                      force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self.examples_cache and not force_refresh:
            return self.examples_cache[:limit]
        try:
            conn = _connect(self.db_file, read_only=True)
            rows = conn.execute(f"""
                SELECT question, sql, category, confidence, created_at
                FROM {self.kb_table}
                WHERE record_type = 'example' AND confidence >= ?
                ORDER BY confidence DESC, created_at DESC
                LIMIT ?
            """, [min_quality, limit * 2]).fetchall()
            conn.close()
            self.examples_cache = [
                {"question": q, "sql": s, "category": c, "quality_score": qs, "created_at": ca}
                for q, s, c, qs, ca in rows
            ]
            return self.examples_cache[:limit]
        except Exception as e:
            logger.warning(f"[KB] load_examples failed: {e}")
            return []

    def load_glossary(self, tables: List[str] = None, limit: int = 20,
                      force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self.glossary_cache and not force_refresh and not tables:
            return self.glossary_cache[:limit]
        try:
            conn = _connect(self.db_file, read_only=True)
            if tables:
                ph   = ",".join(["?"] * len(tables))
                rows = conn.execute(f"""
                    SELECT column_name, content_text
                    FROM {self.kb_table}
                    WHERE record_type = 'glossary' AND table_name IN ({ph})
                    ORDER BY confidence DESC LIMIT ?
                """, tables + [limit]).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT column_name, content_text
                    FROM {self.kb_table}
                    WHERE record_type = 'glossary'
                    ORDER BY confidence DESC LIMIT ?
                """, [limit * 2]).fetchall()
            conn.close()
            result = [{"column_reference": cr, "definition": d} for cr, d in rows]
            if not tables:
                self.glossary_cache = result
            return result[:limit]
        except Exception as e:
            logger.warning(f"[KB] load_glossary failed: {e}")
            return []

    def load_joins(self, force_refresh: bool = False) -> Dict[str, List[Tuple[str, str]]]:
        if self.joins_cache and not force_refresh:
            return self.joins_cache
        try:
            audit_cols = {"system_date", "system_active", "file_path", "sys_date", "sys_active"}
            conn       = _connect(self.db_file, read_only=True)
            rows       = conn.execute(f"""
                SELECT table_name, column_name, related_table, content_text, metadata_json
                FROM {self.kb_table}
                WHERE record_type = 'join'
            """).fetchall()
            conn.close()
            joins: Dict[str, List] = defaultdict(list)
            for t1, c1, t2, c2, mj in rows:
                if str(c2 or "").lower() in audit_cols:
                    continue
                meta = json.loads(mj or "{}")
                joins[t1].append((t2, c2 or meta.get("content_text") or f"{c1}->{t2}"))
            self.joins_cache = dict(joins)
            return self.joins_cache
        except Exception as e:
            logger.warning(f"[KB] load_joins failed: {e}")
            return {}

    def register_table(self, table_name: str, schema_profile: dict) -> bool:
        try:
            self._ensure_kb_schema()
            conn = _connect(self.db_file)

            snapshot_bronze_map = {}
            raw_bronze_map      = {}
            try:
                # Avoid double schema prefix if table_name already has schema
                if self.user_schema and not table_name.startswith(f"{self.user_schema}."):
                    qtable = f"{self.user_schema}.{table_name}"
                else:
                    qtable = table_name
                raw_rows  = conn.execute(f'DESCRIBE {qtable}').fetchall()
                raw_bronze_map = {str(r[0]): str(r[1]) for r in raw_rows if r and r[0]}
            except Exception as e:
                logger.warning(f"[KB] raw bronze map lookup failed for {table_name}: {e}")

            dataset_id       = schema_profile.get("_dataset_id") or schema_profile.get("dataset_id") or table_name
            source_file_name = schema_profile.get("filename") or schema_profile.get("source_file_name") or ""

            conn.execute(f"DELETE FROM {self.kb_table} WHERE table_name = ? AND record_type IN ('table', 'column', 'glossary', 'vector', 'join', 'semantic_layer')", [table_name])

            schema_cols = schema_profile.get("schema", [])
            for pos, col in enumerate(schema_cols):
                cname = col.get("column")
                if not cname:
                    continue
                sample_values             = col.get("sample_values") or col.get("categorical_values") or []
                sample_values_text        = " • ".join(str(v) for v in sample_values[:5] if v is not None)
                column_description        = (col.get("column_description") or col.get("description") or "").strip()
                detailed_business_meaning = (col.get("business_meaning") or "").strip()
                validation_rule           = (col.get("validation_rule") or "").strip()
                column_example            = (sample_values_text or (sample_values[0] if sample_values else "") or "").strip()
                bronze_datatype           = (
                    raw_bronze_map.get(cname)
                    or col.get("data_type")
                    or "TEXT"
                )
                search_text = " • ".join(filter(None, [
                    table_name, dataset_id, source_file_name, cname,
                    col.get("data_type") or "", column_description,
                    detailed_business_meaning, validation_rule, column_example, sample_values_text,
                ]))
                _upsert_metadata_record(conn, self.kb_table, {
                    "id": f"{table_name}:{cname}",
                    "record_type": "column",
                    "table_name": table_name,
                    "column_name": cname,
                    "column_description": column_description,
                    "detailed_business_meaning": detailed_business_meaning,
                    "column_example": column_example,
                    "bronze_datatype": bronze_datatype,
                    "content_text": search_text,
                    "metadata_json": {
                        "dataset_id": dataset_id, "source_file_name": source_file_name,
                        "column_type": col.get("data_type") or "TEXT",
                        "bronze_datatype": bronze_datatype,
                        "column_description": column_description,
                        "detailed_business_meaning": detailed_business_meaning,
                        "validation_rule": validation_rule,
                        "null_count": int(col.get("null_count", 0) or 0),
                        "null_pct": float(col.get("null_pct", 0) or 0),
                        "sample_values": sample_values,
                        "ordinal_position": pos,
                        "business_meaning": detailed_business_meaning or column_description,
                        "column_example": column_example,
                        "nullable": col.get("nullable", True),
                        "search_text": search_text,
                    },
                })
                if column_description:
                    _upsert_metadata_record(conn, self.kb_table, {
                        "id": f"{table_name}:{cname}:meaning",
                        "record_type": "glossary",
                        "table_name": table_name,
                        "column_name": f"{table_name}.{cname}",
                        "column_description": column_description,
                        "detailed_business_meaning": detailed_business_meaning,
                        "column_example": column_example,
                        "content_text": column_description,
                        "metadata_json": {"definition": column_description, "confidence": 0.9},
                        "confidence": 0.9,
                    })

            # Table-level vector + record
            table_text = " | ".join(filter(None, [
                table_name,
                " ".join(col.get("column") or "" for col in schema_cols),
                " ".join(col.get("business_meaning") or col.get("column_description") or "" for col in schema_cols),
            ]))
            self._upsert_vector(conn, f"{table_name}:__table__", table_name, "table", table_text)
            _upsert_metadata_record(conn, self.kb_table, {
                "id": f"{table_name}:__table__",
                "record_type": "table",
                "table_name": table_name,
                "content_text": schema_profile.get("table_name") or table_name,
                "metadata_json": {
                    "description": schema_profile.get("table_name") or table_name,
                    "row_count": schema_profile.get("total_rows") or 0,
                    "column_count": schema_profile.get("total_columns") or len(schema_cols),
                    "dataset_id": dataset_id, "source_file_name": source_file_name,
                },
            })

            kb_join_edges    = _refresh_joins_from_kb(conn, self.kb_table)
            real_data_edges  = 0
            try:
                schema_prefix = f"{self.user_schema}." if self.user_schema else ""
                all_tables    = conn.execute(f"""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = '{self.user_schema or 'main'}'
                      AND table_type = 'BASE TABLE'
                      AND table_name NOT LIKE 'kb_%'
                            ORDER BY table_name
                """).fetchall()
                real_data_edges = _refresh_joins_from_real_data(
                    self.db_file, conn,
                    [t[0] for t in all_tables],
                    self.kb_table,
                    user_schema=self.user_schema,
                )
            except Exception as e:
                logger.warning(f"[KB] real-data join refresh skipped: {e}")

            conn.close()
            self.catalog_cache = None
            logger.info(f"[KB] Registered {table_name} in schema={self.user_schema}: {len(schema_cols)} cols | kb_joins={kb_join_edges} | real_joins={real_data_edges}")
            return True
        except Exception as e:
            logger.error(f"[KB] register_table {table_name} failed: {e}")
            return False

    def save_example(self, question: str, sql: str,
                     category: str = "general", quality_score: float = 0.85) -> bool:
        if not question or not sql:
            return False
        try:
            conn  = _connect(self.db_file)
            ex_id = f"example:{abs(hash((question, sql))) % 10**10}"
            _upsert_metadata_record(conn, self.kb_table, {
                "id": ex_id, "record_type": "example",
                "content_text": question,
                "metadata_json": {"question": question, "sql": sql, "category": category, "quality_score": max(0.0, min(1.0, float(quality_score)))},
                "confidence": max(0.0, min(1.0, float(quality_score))),
                "category": category, "question": question, "sql": sql,
            })
            conn.close()
            self.examples_cache = None
            return True
        except Exception as e:
            logger.error(f"[KB] save_example failed: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        try:
            catalog  = self.load_catalog()
            examples = self.load_examples(limit=1000)
            glossary = self.load_glossary(limit=1000)
            joins    = self.load_joins()
            try:
                conn           = _connect(self.db_file, read_only=True)
                vec_count      = conn.execute(f"SELECT COUNT(*) FROM {self.kb_table}").fetchone()[0]
                vec_null_count = conn.execute(f"SELECT COUNT(*) FROM {self.kb_table} WHERE embedding IS NULL").fetchone()[0]
                conn.close()
            except Exception:
                vec_count = vec_null_count = 0
            return {
                "num_tables":             len(catalog),
                "num_columns":            sum(len(t.get("columns", [])) for t in catalog.values()),
                "num_examples":           len(examples),
                "num_glossary_entries":   len(glossary),
                "num_join_relationships": sum(len(v) for v in joins.values()),
                "num_vectors_total":      vec_count,
                "num_vectors_with_embeddings": vec_count - vec_null_count,
                "vss_enabled":            self._vss_ok(),
                "user_schema":            self.user_schema,
                "status":                 "OK",
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    def clear_cache(self):
        self.catalog_cache  = None
        self.examples_cache = None
        self.glossary_cache = None
        self.joins_cache    = None
        logger.info(f"[KB] Caches cleared for schema={self.user_schema}")


# ============================================================================
# FUNCTION-BASED API
# ============================================================================

def _kb_state_path(storage_dir: str) -> str:
    d = os.path.join(storage_dir, "kb")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _KB_STATE_FILE)


def _save_kb_state(storage_dir: str, meta: dict):
    meta["last_refresh_epoch"] = datetime.utcnow().timestamp()
    meta["last_refresh_iso"]   = datetime.utcnow().isoformat()
    with open(_kb_state_path(storage_dir), "w") as f:
        json.dump(meta, f, indent=2, default=str)


def should_refresh_kb(storage_dir: str) -> bool:
    interval = int(os.getenv("KB_REFRESH_INTERVAL_SECONDS", "0") or "0")
    if interval <= 0:
        return False
    path = _kb_state_path(storage_dir)
    if not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            last = float(json.load(f).get("last_refresh_epoch", 0))
        return (datetime.utcnow().timestamp() - last) >= interval
    except Exception:
        return True


def _infer_join_edges(catalog: dict, primary_table: str) -> list:
    if primary_table not in catalog:
        return []
    def col_names(meta):
        return [(c.get("column") or "").lower() for c in meta.get("columns", [])]
    def is_key(col):
        if col in {"system_date", "system_active", "file_path"}:
            return False
        return col in ("id",) or col.endswith("_id") or col.endswith("_key")
    left_cols = set(filter(is_key, col_names(catalog[primary_table])))
    edges = []
    for other, meta in catalog.items():
        if other == primary_table:
            continue
        shared = left_cols & set(filter(is_key, col_names(meta)))
        for col in sorted(shared):
            for t1, t2 in [(primary_table, other), (other, primary_table)]:
                edges.append({"table1": t1, "column1": col, "table2": t2, "column2": col, "confidence": 0.7, "source": "inferred"})
    return edges


def persist_kb(storage_dir: str, dataset_id: str, schema_profile: dict,
               table_name: Optional[str] = None, kb_manager: "KBManager" = None) -> dict:
    kb        = kb_manager or get_kb_manager()
    canonical = table_name or schema_profile.get("table_name") or dataset_id
    ok        = kb.register_table(canonical, schema_profile)

    try:
        catalog     = kb.load_catalog(force_refresh=True)
        existing_md = load_semantic_layer(storage_dir)
        semantic_md = _merge_semantic_layer_markdown(existing_md, catalog, kb.load_joins(force_refresh=True), [canonical])
        _save_semantic_layer(storage_dir, semantic_md)
    except Exception as ex:
        logger.warning(f"[kb] semantic layer build failed: {ex}")

    state = _merge_kb_state(storage_dir, {
        "last_dataset_id": dataset_id,
        "tables": list(kb.load_catalog().keys()),
    })
    _save_kb_state(storage_dir, state)
    return {
        "success": ok,
        "documents_written": len(schema_profile.get("schema", [])) + 1,
        "join_edges": 0,
    }


def refresh_kb_from_duckdb(db_file: str, storage_dir: str,
                            kb_manager: "KBManager" = None) -> dict:
    kb = kb_manager or get_kb_manager(db_file)
    _DB_FILE_REF[0] = db_file
    user_schema = kb.user_schema

    try:
        conn_ro = _connect(db_file, read_only=True)
        rows    = conn_ro.execute(f"""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = '{user_schema or 'main'}' AND table_type = 'BASE TABLE'
              AND table_name NOT LIKE 'kb_%'
            ORDER BY table_name
        """).fetchall()
        conn_ro.close()
    except Exception as e:
        return {"success": False, "error": str(e)}

    refreshed, docs_written = [], 0
    for (tname,) in rows:
        try:
            conn_ro   = _connect(db_file, read_only=True)
            qtable    = f"{user_schema}.{tname}" if user_schema else f'"{tname}"'
            describe  = conn_ro.execute(f'DESCRIBE {qtable}').fetchall()
            row_count = conn_ro.execute(f'SELECT COUNT(*) FROM {qtable}').fetchone()[0]
            conn_ro.close()
            profile = {
                "table_name": tname, "total_rows": int(row_count), "total_columns": len(describe),
                "schema": [{"column": c[0], "data_type": c[1], "business_meaning": "", "nullable": True} for c in describe],
            }
            if kb.register_table(tname, profile):
                refreshed.append(tname)
                docs_written += len(describe) + 1
        except Exception as e:
            logger.warning(f"[kb] refresh skipped {tname}: {e}")

    try:
        semantic_md = build_semantic_layer_markdown(kb.load_catalog(force_refresh=True), kb.load_joins(force_refresh=True))
        _save_semantic_layer(storage_dir, semantic_md)
    except Exception as ex:
        logger.warning(f"[kb] semantic layer rebuild failed: {ex}")

    _save_kb_state(storage_dir, {"tables": refreshed, "refresh_mode": "full_duckdb_scan"})
    return {"success": True, "tables_indexed": len(refreshed), "documents_written": docs_written, "join_edges": 0, "tables": refreshed}


def upsert_example_pair(storage_dir: str, dataset_id: str, question: str,
                        sql: str, tables: list, tags: list = None,
                        kb_manager: "KBManager" = None) -> dict:
    kb  = kb_manager or get_kb_manager()
    cat = "join" if len(tables) > 1 else "single_table"
    ok  = kb.save_example(question, sql, category=cat, quality_score=0.85)
    return {"success": ok}


def upsert_join_edges(storage_dir: str, edges: list,
                      kb_manager: "KBManager" = None) -> dict:
    kb = kb_manager or get_kb_manager()
    if not edges:
        return {"success": True, "edges_written": 0}
    try:
        conn     = _connect(kb.db_file)
        inserted = 0
        for e in edges:
            lt  = e.get("left_table") or e.get("table1")
            lc  = e.get("left_column") or e.get("column1") or ""
            rt  = e.get("right_table") or e.get("table2")
            rc  = e.get("right_column") or e.get("column2") or ""
            src = str(e.get("source") or "tab8")
            if not lt or not rt:
                continue
            _upsert_metadata_record(conn, kb.kb_table, {
                "id": f"join:{src}:{lt}:{lc}:{rt}:{rc}",
                "record_type": "join",
                "table_name": lt, "related_table": rt, "column_name": lc,
                "content_text": f"{lt}.{lc} -> {rt}.{rc}",
                "metadata_json": {**e, "source": src},
                "confidence": float(e.get("confidence") or 0.5),
            })
            inserted += 1
        conn.close()
        kb.joins_cache = None
        return {"success": True, "edges_written": inserted}
    except Exception as e:
        logger.error(f"[kb] upsert_join_edges failed: {e}")
        return {"success": False, "error": str(e)}


# ============================================================================
# SINGLETON (global fallback — used when no per-user KB is needed)
# ============================================================================

def get_kb_manager(db_file: str = None, user_schema: str = None) -> KBManager:
    global _KB_MANAGER_INSTANCE
    if db_file:
        _DB_FILE_REF[0] = db_file
    if _KB_MANAGER_INSTANCE is None:
        target = db_file or _DB_FILE_REF[0]
        if not target:
            raise RuntimeError("kb_manager: db_file required on first call")
        _KB_MANAGER_INSTANCE = KBManager(target, user_schema=user_schema)
    return _KB_MANAGER_INSTANCE


def init_kb_manager(db_file: str, user_schema: str = None) -> KBManager:
    """Create a new KBManager — used per-user from app.py _KB_REGISTRY."""
    _DB_FILE_REF[0] = db_file
    kb = KBManager(db_file, user_schema=user_schema)
    try:
        kb.load_catalog()
        kb.load_examples(limit=10)
        kb.load_glossary(limit=50)
        kb.load_joins()
        stats = kb.get_stats()
        logger.info(f"[KB] init complete for schema={user_schema}: {stats}")
    except Exception as e:
        logger.warning(f"[KB] pre-warm issue: {e}")
    return kb