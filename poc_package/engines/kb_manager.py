"""
KB_MANAGER: Knowledge Base with Vector Semantic Search
=======================================================
Stores and retrieves tables/columns using:
1. kb_vectors table (Titan embeddings via DuckDB VSS HNSW)
2. Keyword fallback (cosine similarity over text)

If vectors fail to embed, keyword fallback activates silently.
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb

from logger_config import get_logger

logger = get_logger(__name__)

VECTOR_DIM       = 1024
_KB_STATE_FILE   = "kb_state.json"
_DB_FILE_REF: list = [None]
_KB_MANAGER_INSTANCE: Optional["KBManager"] = None


# ── lazy embed import ──────────────────────────────────────────────────────
def _embed(text: str) -> List[float]:
    """Call Bedrock Titan embeddings. Returns [] on failure (graceful degrade)."""
    try:
        from bedrock_client import embed_text
        vec = embed_text(text)
        if vec and len(vec) == VECTOR_DIM:
            logger.debug(f"[embed] success | {len(text)} chars → {len(vec)}-dim")
            return vec
        else:
            logger.warning(f"[embed] invalid result | got {len(vec)} dims, expected {VECTOR_DIM}")
            return []
    except Exception as e:
        logger.warning(f"[embed] Bedrock failed: {e}")
        return []


def _pad(vector: List[float], size: int = VECTOR_DIM) -> List[float]:
    """Pad or truncate vector to exact size."""
    vec = [float(v) for v in (vector or [])]
    if len(vec) < size:
        vec += [0.0] * (size - len(vec))
    return vec[:size]


def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not v1 or not v2:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    mag1 = sum(a * a for a in v1) ** 0.5
    mag2 = sum(b * b for b in v2) ** 0.5
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


# ============================================================================
# KBManager
# ============================================================================

class KBManager:
    """Manages KB tables inside ai_poc_dq.duckdb."""

    def __init__(self, db_file: str):
        self.db_file        = db_file
        self.catalog_cache  = None
        self.examples_cache = None
        self.glossary_cache = None
        self.joins_cache    = None
        self._vss_available = None

        self._ensure_kb_schema()
        logger.info(f"[KB] KBManager initialized (db={db_file})")

    # ── schema bootstrap ───────────────────────────────────────────────────

    def _ensure_kb_schema(self):
        """Create KB tables + VSS HNSW index if they don't exist."""
        try:
            conn = duckdb.connect(self.db_file)

            # Core KB tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_catalog (
                    table_name   TEXT PRIMARY KEY,
                    description  TEXT,
                    row_count    BIGINT,
                    column_count INTEGER,
                    created_at   TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_documents (
                    id               TEXT,
                    table_name       TEXT,
                    column_name      TEXT,
                    data_type        TEXT,
                    business_meaning TEXT,
                    nullable         BOOLEAN,
                    ordinal_position INTEGER,
                    created_at       TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_schema_fields (
                    id                TEXT PRIMARY KEY,
                    dataset_id        TEXT,
                    table_name        TEXT,
                    source_file_name  TEXT,
                    column_name       TEXT,
                    column_type       TEXT,
                    column_description TEXT,
                    validation_rule   TEXT,
                    null_count        BIGINT,
                    null_pct          DOUBLE,
                    sample_values     TEXT,
                    search_text       TEXT,
                    created_at        TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_examples (
                    id            TEXT PRIMARY KEY,
                    question      TEXT,
                    sql           TEXT,
                    category      TEXT,
                    quality_score DOUBLE,
                    created_at    TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_glossary (
                    id               TEXT,
                    table_name       TEXT,
                    column_reference TEXT,
                    definition       TEXT,
                    confidence       DOUBLE,
                    created_at       TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_joins (
                    table1     TEXT,
                    column1    TEXT,
                    table2     TEXT,
                    column2    TEXT,
                    confidence DOUBLE,
                    source     TEXT
                )
            """)
            
            # Vector table
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS kb_vectors (
                    id          TEXT PRIMARY KEY,
                    table_name  TEXT,
                    doc_type    TEXT,          -- 'table' | 'column'
                    text        TEXT,
                    embedding   FLOAT[{VECTOR_DIM}],
                    created_at  TEXT
                )
            """)

            # Try VSS HNSW index
            try:
                conn.execute("INSTALL vss; LOAD vss;")
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS kb_vectors_hnsw
                    ON kb_vectors USING HNSW (embedding)
                    WITH (metric = 'cosine')
                """)
                self._vss_available = True
                logger.info("[KB] VSS HNSW index enabled")
            except Exception as vss_err:
                self._vss_available = False
                logger.info(f"[KB] VSS unavailable, using keyword fallback: {vss_err}")

            conn.close()
            logger.info("[KB] Schema verified/created")
        except Exception as e:
            logger.error(f"[KB] Schema creation failed: {e}")

    def _vss_ok(self) -> bool:
        """Check if VSS is available."""
        if self._vss_available is None:
            try:
                conn = duckdb.connect(self.db_file, read_only=True)
                conn.execute("LOAD vss;")
                conn.close()
                self._vss_available = True
            except Exception:
                self._vss_available = False
        return bool(self._vss_available)

    # ── vector upsert ──────────────────────────────────────────────────────

    def _upsert_vector(self, conn, doc_id: str, table_name: str,
                       doc_type: str, text: str):
        """Embed text and store vector. Fails gracefully if embed fails."""
        vector = _embed(text)
        conn.execute("DELETE FROM kb_vectors WHERE id = ?", [doc_id])
        if vector:
            logger.debug(f"[KB] storing vector for {doc_id}")
        else:
            logger.debug(f"[KB] no vector produced for {doc_id}, storing zero vector")
        conn.execute(
            "INSERT INTO kb_vectors (id, table_name, doc_type, text, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [doc_id, table_name, doc_type, text, _pad(vector),
             datetime.now().isoformat()],
        )

    # ── vector retrieval (with keyword fallback) ──────────────────────────

    def retrieve_by_vector(self, question: str, top_k: int = 15) -> List[Dict[str, Any]]:
        """
        Try VSS first, fallback to keyword search if it fails.
        Returns list of {id, table_name, doc_type, text, score}.
        """
        # Try VSS
        if self._vss_ok():
            try:
                results = self._retrieve_vss(question, top_k)
                if results:
                    logger.info(f"[KB] VSS returned {len(results)} documents")
                    return results
                logger.debug("[KB] VSS returned 0 results, trying keyword fallback")
            except Exception as e:
                logger.warning(f"[KB] VSS query failed: {e}, falling back to keyword")

        # Fallback: keyword search
        logger.info("[KB] Using keyword similarity fallback")
        return self._retrieve_keyword(question, top_k)

    def _retrieve_vss(self, question: str, top_k: int) -> List[Dict[str, Any]]:
        """Vector search via DuckDB VSS HNSW."""
        q_vec = _embed(question)
        if not q_vec:
            logger.warning("[KB] Question embedding failed")
            return []

        conn = duckdb.connect(self.db_file, read_only=True)
        try:
            conn.execute("LOAD vss;")
            rows = conn.execute(f"""
                SELECT id, table_name, doc_type, text,
                       array_cosine_distance(embedding,
                           CAST(? AS FLOAT[{VECTOR_DIM}])) AS dist
                FROM kb_vectors
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
        """Keyword fallback: cosine similarity over text."""
        try:
            conn = duckdb.connect(self.db_file, read_only=True)
            rows = conn.execute(
                "SELECT id, table_name, doc_type, text FROM kb_vectors WHERE text IS NOT NULL LIMIT 500"
            ).fetchall()
            conn.close()

            if not rows:
                logger.warning("[KB] No documents in kb_vectors table")
                return []

            # Tokenize question
            q_tokens = set(question.lower().split())
            scores = []

            for doc_id, tname, dtype, text in rows:
                doc_tokens = set((text or "").lower().split())
                overlap = len(q_tokens & doc_tokens)
                if overlap > 0:
                    score = overlap / max(len(q_tokens), len(doc_tokens))
                    scores.append((score, doc_id, tname, dtype, text))

            scores.sort(reverse=True)
            result = [
                {"id": s[1], "table_name": s[2], "doc_type": s[3],
                 "text": s[4], "score": float(s[0])}
                for s in scores[:top_k]
            ]

            logger.info(f"[KB] Keyword fallback returned {len(result)} documents")
            return result

        except Exception as e:
            logger.error(f"[KB] Keyword fallback failed: {e}")
            return []

    # ── catalog ────────────────────────────────────────────────────────────

    def load_catalog(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self.catalog_cache and not force_refresh:
            return self.catalog_cache
        try:
            conn = duckdb.connect(self.db_file, read_only=True)
            rows = conn.execute(
                "SELECT table_name, description, row_count, column_count, created_at "
                "FROM kb_catalog ORDER BY table_name"
            ).fetchall()

            catalog = {}
            for tname, desc, rc, cc, cat in rows:
                col_rows = conn.execute(
                    "SELECT column_name, data_type, business_meaning, nullable "
                    "FROM kb_documents WHERE table_name = ? ORDER BY ordinal_position",
                    [tname],
                ).fetchall()

                catalog[tname] = {
                    "table_name": tname,
                    "description": desc or tname,
                    "row_count": rc,
                    "columns": [
                        {"column": cn, "data_type": dt,
                         "business_meaning": bm or "", "nullable": nl}
                        for cn, dt, bm, nl in col_rows
                    ],
                    "created_at": cat,
                }
            conn.close()
            self.catalog_cache = catalog
            logger.info(f"[KB] Catalog loaded: {len(catalog)} tables")
            return catalog
        except Exception as e:
            logger.error(f"[KB] load_catalog failed: {e}")
            return {}

    # ── examples ───────────────────────────────────────────────────────────

    def load_examples(self, min_quality: float = 0.80, limit: int = 5,
                      force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self.examples_cache and not force_refresh:
            return self.examples_cache[:limit]
        try:
            conn = duckdb.connect(self.db_file, read_only=True)
            rows = conn.execute(
                "SELECT question, sql, category, quality_score, created_at "
                "FROM kb_examples WHERE quality_score >= ? "
                "ORDER BY quality_score DESC, created_at DESC LIMIT ?",
                [min_quality, limit * 2],
            ).fetchall()
            conn.close()
            self.examples_cache = [
                {"question": q, "sql": s, "category": c,
                 "quality_score": qs, "created_at": ca}
                for q, s, c, qs, ca in rows
            ]
            return self.examples_cache[:limit]
        except Exception as e:
            logger.warning(f"[KB] load_examples failed: {e}")
            return []

    # ── glossary ───────────────────────────────────────────────────────────

    def load_glossary(self, tables: List[str] = None, limit: int = 20,
                      force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self.glossary_cache and not force_refresh and not tables:
            return self.glossary_cache[:limit]
        try:
            conn = duckdb.connect(self.db_file, read_only=True)
            if tables:
                ph = ",".join(["?"] * len(tables))
                rows = conn.execute(
                    f"SELECT column_reference, definition FROM kb_glossary "
                    f"WHERE table_name IN ({ph}) ORDER BY confidence DESC LIMIT ?",
                    tables + [limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT column_reference, definition FROM kb_glossary "
                    "ORDER BY confidence DESC LIMIT ?", [limit * 2]
                ).fetchall()
            conn.close()
            result = [{"column_reference": cr, "definition": d} for cr, d in rows]
            if not tables:
                self.glossary_cache = result
            return result[:limit]
        except Exception as e:
            logger.warning(f"[KB] load_glossary failed: {e}")
            return []

    # ── joins ──────────────────────────────────────────────────────────────

    def load_joins(self, force_refresh: bool = False) -> Dict[str, List[Tuple[str, str]]]:
        if self.joins_cache and not force_refresh:
            return self.joins_cache
        try:
            conn = duckdb.connect(self.db_file, read_only=True)
            rows = conn.execute(
                "SELECT table1, column1, table2, column2 FROM kb_joins"
            ).fetchall()
            conn.close()
            joins: Dict[str, List] = defaultdict(list)
            for t1, c1, t2, c2 in rows:
                joins[t1].append((t2, f"{c1}->{c2}"))
            self.joins_cache = dict(joins)
            return self.joins_cache
        except Exception as e:
            logger.warning(f"[KB] load_joins failed: {e}")
            return {}

    # ── register table ─────────────────────────────────────────────────────

    def register_table(self, table_name: str, schema_profile: dict) -> bool:
        """Write approved schema to all KB tables + vectors."""
        try:
            conn = duckdb.connect(self.db_file)
            dataset_id = schema_profile.get("_dataset_id") or schema_profile.get("dataset_id") or table_name
            source_file_name = (
                schema_profile.get("filename")
                or schema_profile.get("source_file_name")
                or schema_profile.get("file_name")
                or ""
            )

            # kb_catalog
            conn.execute("DELETE FROM kb_catalog WHERE table_name = ?", [table_name])
            conn.execute(
                "INSERT INTO kb_catalog VALUES (?, ?, ?, ?, ?)",
                [table_name,
                 schema_profile.get("table_name") or table_name,
                 schema_profile.get("total_rows") or 0,
                 schema_profile.get("total_columns") or
                 len(schema_profile.get("schema", [])),
                 datetime.now().isoformat()],
            )

            # Clear old
            conn.execute("DELETE FROM kb_documents WHERE table_name = ?", [table_name])
            conn.execute("DELETE FROM kb_glossary  WHERE table_name = ?", [table_name])
            conn.execute("DELETE FROM kb_vectors   WHERE table_name = ?", [table_name])
            conn.execute("DELETE FROM kb_schema_fields WHERE table_name = ?", [table_name])

            # Process columns
            schema_cols = schema_profile.get("schema", [])
            for pos, col in enumerate(schema_cols):
                cname = col.get("column")
                if not cname:
                    continue

                # kb_documents
                conn.execute(
                    "INSERT INTO kb_documents "
                    "(id, table_name, column_name, data_type, business_meaning, "
                    " nullable, ordinal_position, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [f"{table_name}:{cname}", table_name, cname,
                     col.get("data_type") or "TEXT",
                     col.get("business_meaning") or "",
                     col.get("nullable", True), pos,
                     datetime.now().isoformat()],
                )

                sample_values = col.get("sample_values") or col.get("categorical_values") or []
                sample_values_text = " | ".join(str(v) for v in sample_values[:5] if v is not None)
                column_description = (col.get("business_meaning") or col.get("description") or col.get("column_description") or "").strip()
                validation_rule = (col.get("validation_rule") or "").strip()
                search_text = " | ".join(filter(None, [
                    table_name,
                    dataset_id,
                    source_file_name,
                    cname,
                    col.get("data_type") or "",
                    column_description,
                    validation_rule,
                    sample_values_text,
                ]))

                conn.execute(
                    "INSERT OR REPLACE INTO kb_schema_fields "
                    "(id, dataset_id, table_name, source_file_name, column_name, column_type, "
                    " column_description, validation_rule, null_count, null_pct, sample_values, "
                    " search_text, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [f"{table_name}:{cname}",
                     dataset_id,
                     table_name,
                     source_file_name,
                     cname,
                     col.get("data_type") or "TEXT",
                     column_description,
                     validation_rule,
                     int(col.get("null_count", 0) or 0),
                     float(col.get("null_pct", 0) or 0),
                     sample_values_text,
                     search_text,
                     datetime.now().isoformat()],
                )

                # kb_glossary
                meaning = column_description
                if meaning:
                    conn.execute(
                        "INSERT INTO kb_glossary "
                        "(id, table_name, column_reference, definition, "
                        " confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [f"{table_name}:{cname}:meaning", table_name,
                         f"{table_name}.{cname}", meaning, 0.9,
                         datetime.now().isoformat()],
                    )

                # kb_vectors — rich column text
                col_text = " | ".join(filter(None, [
                    table_name, cname,
                    col.get("data_type") or "",
                    column_description,
                    validation_rule,
                    " ".join(str(v) for v in sample_values[:3]),
                ]))
                self._upsert_vector(conn, f"{table_name}:{cname}",
                                    table_name, "column", col_text)
                self._upsert_vector(conn, f"{table_name}:{cname}:field",
                                    table_name, "field", search_text)

            # Table-level vector
            table_text = " | ".join(filter(None, [
                table_name,
                schema_profile.get("table_name") or table_name,
                " ".join(col.get("column") or "" for col in schema_cols),
                " ".join(col.get("business_meaning") or "" for col in schema_cols),
            ]))
            self._upsert_vector(conn, f"{table_name}:__table__",
                                table_name, "table", table_text)

            conn.close()
            self.catalog_cache = None
            logger.info(f"[KB] Registered {table_name}: "
                        f"{len(schema_cols)} columns + {len(schema_cols)*2+1} vectors")
            return True
        except Exception as e:
            logger.error(f"[KB] register_table {table_name} failed: {e}")
            return False

    # ── save example ───────────────────────────────────────────────────────

    def save_example(self, question: str, sql: str,
                     category: str = "general",
                     quality_score: float = 0.85) -> bool:
        if not question or not sql:
            return False
        try:
            ex_id = (f"{abs(hash(question)) % 10**8}_"
                     f"{datetime.now().strftime('%Y%m%d%H%M%S')}")
            conn = duckdb.connect(self.db_file)
            conn.execute("DELETE FROM kb_examples WHERE id = ?", [ex_id])
            conn.execute(
                "INSERT INTO kb_examples VALUES (?, ?, ?, ?, ?, ?)",
                [ex_id, question, sql, category,
                 max(0.0, min(1.0, float(quality_score))),
                 datetime.now().isoformat()],
            )
            conn.close()
            self.examples_cache = None
            return True
        except Exception as e:
            logger.error(f"[KB] save_example failed: {e}")
            return False

    # ── stats ──────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        try:
            catalog  = self.load_catalog()
            examples = self.load_examples(limit=1000)
            glossary = self.load_glossary(limit=1000)
            joins    = self.load_joins()
            try:
                conn      = duckdb.connect(self.db_file, read_only=True)
                vec_count = conn.execute(
                    "SELECT COUNT(*) FROM kb_vectors"
                ).fetchone()[0]
                vec_null_count = conn.execute(
                    "SELECT COUNT(*) FROM kb_vectors WHERE embedding IS NULL"
                ).fetchone()[0]
                conn.close()
            except Exception:
                vec_count = vec_null_count = 0

            return {
                "num_tables":             len(catalog),
                "num_columns":            sum(len(t.get("columns", []))
                                              for t in catalog.values()),
                "num_examples":           len(examples),
                "num_glossary_entries":   len(glossary),
                "num_join_relationships": sum(len(v) for v in joins.values()),
                "num_vectors_total":      vec_count,
                "num_vectors_with_embeddings": vec_count - vec_null_count,
                "vss_enabled":            self._vss_ok(),
                "status":                 "✅ READY",
            }
        except Exception as e:
            return {"status": "❌ ERROR", "error": str(e)}

    # ── cache ──────────────────────────────────────────────────────────────

    def clear_cache(self):
        self.catalog_cache = None
        self.examples_cache = None
        self.glossary_cache = None
        self.joins_cache = None
        logger.info("[KB] Caches cleared")


# ============================================================================
# FUNCTION-BASED API (called from app.py)
# ============================================================================

def _kb_state_path(storage_dir: str) -> str:
    d = os.path.join(storage_dir, "kb")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _KB_STATE_FILE)


def _save_kb_state(storage_dir: str, meta: dict):
    meta["last_refresh_epoch"] = datetime.utcnow().timestamp()
    meta["last_refresh_iso"] = datetime.utcnow().isoformat()
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
        return col in ("id",) or col.endswith("_id") or col.endswith("_key")

    left_cols = set(filter(is_key, col_names(catalog[primary_table])))
    edges = []
    for other, meta in catalog.items():
        if other == primary_table:
            continue
        shared = left_cols & set(filter(is_key, col_names(meta)))
        for col in sorted(shared):
            for t1, t2 in [(primary_table, other), (other, primary_table)]:
                edges.append({"table1": t1, "column1": col,
                               "table2": t2, "column2": col,
                               "confidence": 0.7, "source": "inferred"})
    return edges


def persist_kb(storage_dir: str, dataset_id: str, schema_profile: dict,
               table_name: Optional[str] = None) -> dict:
    kb = get_kb_manager()
    canonical = table_name or schema_profile.get("table_name") or dataset_id
    ok = kb.register_table(canonical, schema_profile)

    join_edges = 0
    try:
        catalog = kb.load_catalog(force_refresh=True)
        edges = _infer_join_edges(catalog, canonical)
        if edges:
            conn = duckdb.connect(kb.db_file)
            conn.execute("DELETE FROM kb_joins WHERE table1=? OR table2=?",
                         [canonical, canonical])
            for e in edges:
                conn.execute("INSERT INTO kb_joins VALUES (?,?,?,?,?,?)",
                             [e["table1"], e["column1"], e["table2"],
                              e["column2"], e["confidence"], e["source"]])
            conn.close()
            kb.joins_cache = None
            join_edges = len(edges)
    except Exception as ex:
        logger.warning(f"[kb] join inference failed: {ex}")

    _save_kb_state(storage_dir, {
        "last_dataset_id": dataset_id,
        "tables": list(kb.load_catalog().keys()),
        "join_edges": join_edges,
    })
    return {
        "success": ok,
        "documents_written": len(schema_profile.get("schema", [])) + 1,
        "join_edges": join_edges,
    }


def refresh_kb_from_duckdb(db_file: str, storage_dir: str) -> dict:
    kb = get_kb_manager()
    _DB_FILE_REF[0] = db_file
    try:
        conn_ro = duckdb.connect(db_file, read_only=True)
        rows = conn_ro.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
              AND table_name NOT LIKE 'kb_%'
            ORDER BY table_name
        """).fetchall()
        conn_ro.close()
    except Exception as e:
        return {"success": False, "error": str(e)}

    refreshed, docs_written = [], 0
    for (tname,) in rows:
        try:
            conn_ro = duckdb.connect(db_file, read_only=True)
            describe = conn_ro.execute(f'DESCRIBE "{tname}"').fetchall()
            row_count = conn_ro.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
            conn_ro.close()
            profile = {
                "table_name": tname,
                "total_rows": int(row_count),
                "total_columns": len(describe),
                "schema": [
                    {"column": c[0], "data_type": c[1],
                     "business_meaning": "", "nullable": True}
                    for c in describe
                ],
            }
            if kb.register_table(tname, profile):
                refreshed.append(tname)
                docs_written += len(describe) + 1
        except Exception as e:
            logger.warning(f"[kb] refresh skipped {tname}: {e}")

    join_edges = 0
    try:
        catalog = kb.load_catalog(force_refresh=True)
        conn = duckdb.connect(db_file)
        conn.execute("DELETE FROM kb_joins")
        processed = set()
        all_edges = []
        for tname in refreshed:
            for e in _infer_join_edges(catalog, tname):
                pair = tuple(sorted([e["table1"], e["table2"], e["column1"]]))
                if pair not in processed:
                    processed.add(pair)
                    all_edges.append(e)
        for e in all_edges:
            conn.execute("INSERT INTO kb_joins VALUES (?,?,?,?,?,?)",
                         [e["table1"], e["column1"], e["table2"],
                          e["column2"], e["confidence"], e["source"]])
        conn.close()
        kb.joins_cache = None
        join_edges = len(all_edges)
    except Exception as ex:
        logger.warning(f"[kb] join rebuild failed: {ex}")

    _save_kb_state(storage_dir, {
        "tables": refreshed,
        "join_edges": join_edges,
        "refresh_mode": "full_duckdb_scan",
    })
    return {
        "success": True,
        "tables_indexed": len(refreshed),
        "documents_written": docs_written,
        "join_edges": join_edges,
        "tables": refreshed,
    }


def upsert_example_pair(storage_dir: str, dataset_id: str, question: str,
                        sql: str, tables: list, tags: list = None) -> dict:
    kb = get_kb_manager()
    cat = "join" if len(tables) > 1 else "single_table"
    ok = kb.save_example(question, sql, category=cat, quality_score=0.85)
    return {"success": ok}


def upsert_join_edges(storage_dir: str, edges: list) -> dict:
    kb = get_kb_manager()
    if not edges:
        return {"success": True, "edges_written": 0}
    try:
        conn = duckdb.connect(kb.db_file)
        inserted = 0
        for e in edges:
            lt = e.get("left_table") or e.get("table1")
            lc = e.get("left_column") or e.get("column1") or ""
            rt = e.get("right_table") or e.get("table2")
            rc = e.get("right_column") or e.get("column2") or ""
            src = str(e.get("source") or "tab8")
            if not lt or not rt:
                continue
            conn.execute(
                "DELETE FROM kb_joins "
                "WHERE table1=? AND column1=? AND table2=? AND source=?",
                [lt, lc, rt, src],
            )
            conn.execute("INSERT INTO kb_joins VALUES (?,?,?,?,?,?)",
                         [lt, lc, rt, rc,
                          float(e.get("confidence") or 0.5), src])
            inserted += 1
        conn.close()
        kb.joins_cache = None
        return {"success": True, "edges_written": inserted}
    except Exception as e:
        logger.error(f"[kb] upsert_join_edges failed: {e}")
        return {"success": False, "error": str(e)}


# ============================================================================
# SINGLETON
# ============================================================================

def get_kb_manager(db_file: str = None) -> KBManager:
    global _KB_MANAGER_INSTANCE
    if db_file:
        _DB_FILE_REF[0] = db_file
    if _KB_MANAGER_INSTANCE is None:
        target = db_file or _DB_FILE_REF[0]
        if not target:
            raise RuntimeError("kb_manager: db_file required on first call")
        _KB_MANAGER_INSTANCE = KBManager(target)
    return _KB_MANAGER_INSTANCE


def init_kb_manager(db_file: str) -> KBManager:
    global _KB_MANAGER_INSTANCE
    _DB_FILE_REF[0] = db_file
    _KB_MANAGER_INSTANCE = KBManager(db_file)
    try:
        _KB_MANAGER_INSTANCE.load_catalog()
        _KB_MANAGER_INSTANCE.load_examples(limit=10)
        _KB_MANAGER_INSTANCE.load_glossary(limit=50)
        _KB_MANAGER_INSTANCE.load_joins()
        stats = _KB_MANAGER_INSTANCE.get_stats()
        logger.info(f"[KB] init complete: {stats}")
    except Exception as e:
        logger.warning(f"[KB] pre-warm issue: {e}")
    return _KB_MANAGER_INSTANCE
