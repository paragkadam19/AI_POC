"""
KB_MANAGER: Knowledge Base Manager
===================================

Fully dynamic KB management system - NO hardcoding
Handles:
1. Loading KB from DuckDB tables
2. Discovering relationships
3. Managing examples
4. Inferring parameters
5. Providing context for Tab 8
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import duckdb
from logger_config import get_logger

logger = get_logger(__name__)

# ============================================================================
# KB_MANAGER: Main KB Management Class
# ============================================================================

class KBManager:
    """
    Manages Knowledge Base from DuckDB tables.

    Loads and caches:
    - kb_catalog (table metadata)
    - kb_documents (column metadata)
    - kb_examples (validated queries)
    - kb_glossary (business definitions)
    - kb_joins (relationships)
    """

    def __init__(self, db_file: str):
        """Initialize KB Manager and ensure schema exists."""
        self.db_file = db_file
        self.catalog_cache = None
        self.examples_cache = None
        self.glossary_cache = None
        self.joins_cache = None
        self.documents_cache = None

        # Create KB tables if they don't exist yet
        self._ensure_kb_schema()

        logger.info(f"[KB] KBManager initialized (db={db_file})")

    # ────────────────────────────────────────────────────────────────────
    # SCHEMA BOOTSTRAP
    # ────────────────────────────────────────────────────────────────────

    def _ensure_kb_schema(self):
        """
        Create KB tables in the main DuckDB file if they don't exist.
        Called once at startup so load_catalog / load_joins etc. never
        fail with 'Table does not exist'.
        """
        try:
            conn = duckdb.connect(self.db_file)
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
            conn.close()
            logger.info("[KB] KB schema verified/created in main DuckDB")
        except Exception as e:
            logger.warning(f"[KB] Schema creation failed (non-critical): {e}")

    # ────────────────────────────────────────────────────────────────────
    # CATALOG LOADING
    # ────────────────────────────────────────────────────────────────────

    def load_catalog(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Load complete catalog from KB tables.

        Queries:
        - kb_catalog (table metadata)
        - kb_documents (column metadata)

        Args:
            force_refresh: Ignore cache and reload from DB

        Returns:
            Dict mapping table_name → table metadata
        """
        if self.catalog_cache and not force_refresh:
            logger.debug(f"[KB] Using cached catalog ({len(self.catalog_cache)} tables)")
            return self.catalog_cache

        try:
            conn = duckdb.connect(self.db_file, read_only=True)

            table_rows = conn.execute("""
                SELECT
                    table_name,
                    description,
                    row_count,
                    column_count,
                    created_at
                FROM kb_catalog
                ORDER BY table_name
            """).fetchall()

            if not table_rows:
                logger.debug("[KB] kb_catalog is empty")
                conn.close()
                self.catalog_cache = {}
                return {}

            catalog = {}

            for table_name, description, row_count, col_count, created_at in table_rows:
                try:
                    col_rows = conn.execute("""
                        SELECT
                            column_name,
                            data_type,
                            business_meaning,
                            nullable
                        FROM kb_documents
                        WHERE table_name = ?
                        ORDER BY ordinal_position
                    """, [table_name]).fetchall()

                    columns = []
                    for col_name, data_type, business_meaning, nullable in col_rows:
                        columns.append({
                            "column": col_name,
                            "data_type": data_type,
                            "business_meaning": business_meaning or "",
                            "nullable": nullable if nullable is not None else True
                        })

                    catalog[table_name] = {
                        "table_name": table_name,
                        "description": description or table_name,
                        "row_count": row_count,
                        "columns": columns,
                        "created_at": created_at
                    }

                except Exception as e:
                    logger.warning(f"[KB] Error loading columns for {table_name}: {e}")
                    continue

            conn.close()

            self.catalog_cache = catalog
            logger.info(f"[KB] Loaded catalog: {len(catalog)} tables, "
                        f"{sum(len(t.get('columns', [])) for t in catalog.values())} columns")
            return catalog

        except Exception as e:
            logger.error(f"[KB] Failed to load catalog: {e}")
            return {}

    # ────────────────────────────────────────────────────────────────────
    # EXAMPLES LOADING
    # ────────────────────────────────────────────────────────────────────

    def load_examples(self, min_quality: float = 0.80, limit: int = 5,
                      force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Load validated query examples for few-shot learning.

        Args:
            min_quality: Minimum quality score (0.0 - 1.0)
            limit: Max examples to return
            force_refresh: Ignore cache

        Returns:
            List of example dicts with question, sql, category, quality_score
        """
        if self.examples_cache and not force_refresh:
            logger.debug(f"[KB] Using cached examples ({len(self.examples_cache)} examples)")
            return self.examples_cache[:limit]

        try:
            conn = duckdb.connect(self.db_file, read_only=True)

            rows = conn.execute("""
                SELECT
                    question,
                    sql,
                    category,
                    quality_score,
                    created_at
                FROM kb_examples
                WHERE quality_score >= ?
                ORDER BY quality_score DESC, created_at DESC
                LIMIT ?
            """, [min_quality, limit * 2]).fetchall()

            conn.close()

            if not rows:
                logger.debug(f"[KB] No examples with quality >= {min_quality}")
                self.examples_cache = []
                return []

            examples = []
            for question, sql, category, quality_score, created_at in rows:
                examples.append({
                    "question": question,
                    "sql": sql,
                    "category": category,
                    "quality_score": quality_score,
                    "created_at": created_at
                })

            self.examples_cache = examples
            result = examples[:limit]
            logger.info(f"[KB] Loaded {len(result)} examples (quality >= {min_quality})")
            return result

        except Exception as e:
            logger.warning(f"[KB] Failed to load examples: {e}")
            return []

    # ────────────────────────────────────────────────────────────────────
    # GLOSSARY LOADING
    # ────────────────────────────────────────────────────────────────────

    def load_glossary(self, tables: List[str] = None, limit: int = 20,
                      force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Load business glossary definitions.

        Args:
            tables: Filter by tables (optional)
            limit: Max definitions to return
            force_refresh: Ignore cache

        Returns:
            List of dicts with column_reference and definition
        """
        if self.glossary_cache and not force_refresh and not tables:
            logger.debug(f"[KB] Using cached glossary ({len(self.glossary_cache)} entries)")
            return self.glossary_cache[:limit]

        try:
            conn = duckdb.connect(self.db_file, read_only=True)

            if tables:
                placeholders = ','.join(['?' for _ in tables])
                sql = f"""
                    SELECT column_reference, definition
                    FROM kb_glossary
                    WHERE table_name IN ({placeholders})
                    ORDER BY confidence DESC
                    LIMIT ?
                """
                rows = conn.execute(sql, tables + [limit]).fetchall()
            else:
                rows = conn.execute("""
                    SELECT column_reference, definition
                    FROM kb_glossary
                    ORDER BY confidence DESC
                    LIMIT ?
                """, [limit * 2]).fetchall()

            conn.close()

            if not rows:
                logger.debug("[KB] No glossary entries found")
                return []

            glossary = [
                {"column_reference": column_reference, "definition": definition}
                for column_reference, definition in rows
            ]

            if not tables:
                self.glossary_cache = glossary

            result = glossary[:limit]
            logger.info(f"[KB] Loaded {len(result)} glossary entries")
            return result

        except Exception as e:
            logger.warning(f"[KB] Failed to load glossary: {e}")
            return []

    # ────────────────────────────────────────────────────────────────────
    # JOINS LOADING
    # ────────────────────────────────────────────────────────────────────

    def load_joins(self, force_refresh: bool = False) -> Dict[str, List[Tuple[str, str]]]:
        """
        Load join relationships from kb_joins.

        Returns:
            Dict mapping table → list of (target_table, join_column) tuples
        """
        if self.joins_cache and not force_refresh:
            logger.debug("[KB] Using cached joins")
            return self.joins_cache

        try:
            conn = duckdb.connect(self.db_file, read_only=True)

            rows = conn.execute("""
                SELECT table1, column1, table2, column2
                FROM kb_joins
            """).fetchall()

            conn.close()

            joins: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
            for table1, col1, table2, col2 in rows:
                joins[table1].append((table2, f"{col1}->{col2}"))

            self.joins_cache = dict(joins)
            logger.info(f"[KB] Loaded joins: {len(joins)} source tables, "
                        f"{sum(len(v) for v in joins.values())} relationships")
            return self.joins_cache

        except Exception as e:
            logger.warning(f"[KB] Failed to load joins: {e}")
            return {}

    # ────────────────────────────────────────────────────────────────────
    # STATISTICS
    # ────────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Get KB statistics."""
        try:
            catalog  = self.load_catalog()
            examples = self.load_examples(limit=100)
            glossary = self.load_glossary(limit=100)
            joins    = self.load_joins()

            total_columns = sum(len(t.get("columns", [])) for t in catalog.values())

            return {
                "num_tables":            len(catalog),
                "num_columns":           total_columns,
                "num_examples":          len(examples),
                "num_glossary_entries":  len(glossary),
                "num_join_relationships": sum(len(v) for v in joins.values()),
                "status": "✅ READY"
            }
        except Exception as e:
            logger.error(f"[KB] Failed to get stats: {e}")
            return {"status": "❌ ERROR", "error": str(e)}

    # ────────────────────────────────────────────────────────────────────
    # SAVE EXAMPLE
    # ────────────────────────────────────────────────────────────────────

    def save_example(self, question: str, sql: str, category: str = "general",
                     quality_score: float = 0.95) -> bool:
        """
        Save a validated query example to kb_examples.

        Args:
            question: Natural language question
            sql: SQL query
            category: Query category
            quality_score: Quality score (0.0 - 1.0)

        Returns:
            True if saved successfully
        """
        try:
            if not question or not sql:
                logger.warning("[KB] Cannot save example: question and sql required")
                return False

            if not sql.strip().lower().startswith(("select", "with")):
                logger.warning("[KB] Cannot save example: only SELECT/WITH queries allowed")
                return False

            quality_score = max(0.0, min(1.0, float(quality_score)))
            ex_id = f"{abs(hash(question)) % 10**8}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

            conn = duckdb.connect(self.db_file)
            # Upsert — delete existing if same question hash
            conn.execute("DELETE FROM kb_examples WHERE id = ?", [ex_id])
            conn.execute("""
                INSERT INTO kb_examples (id, question, sql, category, quality_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [ex_id, question, sql, category, quality_score, datetime.now().isoformat()])
            conn.close()

            self.examples_cache = None  # invalidate cache
            logger.info(f"[KB] Saved example: {question[:50]}... (quality={quality_score})")
            return True

        except Exception as e:
            logger.error(f"[KB] Failed to save example: {e}")
            return False

    # ────────────────────────────────────────────────────────────────────
    # REGISTER TABLE FROM SCHEMA PROFILE
    # ────────────────────────────────────────────────────────────────────

    def register_table(self, table_name: str, schema_profile: dict) -> bool:
        """
        Register an approved schema profile into kb_catalog + kb_documents.
        Called automatically when Tab 2 schema is approved.

        Args:
            table_name: Exact DuckDB table name
            schema_profile: Approved poc1 schema dict

        Returns:
            True if registered successfully
        """
        try:
            conn = duckdb.connect(self.db_file)

            # Upsert catalog row
            conn.execute("DELETE FROM kb_catalog WHERE table_name = ?", [table_name])
            conn.execute("""
                INSERT INTO kb_catalog (table_name, description, row_count, column_count, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, [
                table_name,
                schema_profile.get("table_name") or table_name,
                schema_profile.get("total_rows") or 0,
                schema_profile.get("total_columns") or len(schema_profile.get("schema", [])),
                datetime.now().isoformat(),
            ])

            # Upsert column documents
            conn.execute("DELETE FROM kb_documents WHERE table_name = ?", [table_name])
            for position, col in enumerate(schema_profile.get("schema", [])):
                col_name = col.get("column")
                if not col_name:
                    continue
                conn.execute("""
                    INSERT INTO kb_documents
                        (id, table_name, column_name, data_type, business_meaning,
                         nullable, ordinal_position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    f"{table_name}:{col_name}",
                    table_name,
                    col_name,
                    col.get("data_type") or "TEXT",
                    col.get("business_meaning") or "",
                    col.get("nullable", True),
                    position,
                    datetime.now().isoformat(),
                ])

                # Register glossary entry from business_meaning
                meaning = (col.get("business_meaning") or "").strip()
                if meaning:
                    conn.execute("""
                        INSERT INTO kb_glossary
                            (id, table_name, column_reference, definition, confidence, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, [
                        f"{table_name}:{col_name}:meaning",
                        table_name,
                        f"{table_name}.{col_name}",
                        meaning,
                        0.9,
                        datetime.now().isoformat(),
                    ])

            conn.close()

            # Invalidate caches
            self.catalog_cache  = None
            self.glossary_cache = None
            logger.info(f"[KB] Registered table={table_name} into kb_catalog + kb_documents")
            return True

        except Exception as e:
            logger.error(f"[KB] Failed to register table {table_name}: {e}")
            return False

    # ────────────────────────────────────────────────────────────────────
    # INFER JOIN KEYS
    # ────────────────────────────────────────────────────────────────────

    def infer_join_keys(self) -> Set[str]:
        """
        Infer join key column names from catalog.

        Returns:
            Set of column names that look like join keys
        """
        try:
            catalog = self.load_catalog()

            pattern_scores: Dict[str, int] = defaultdict(int)
            for table, meta in catalog.items():
                for col in meta.get("columns", []):
                    col_name = (col.get("column") or "").lower()
                    if not col_name:
                        continue
                    if col_name == "id":
                        pattern_scores[col_name] += 10
                    elif col_name.endswith("_id"):
                        pattern_scores[col_name] += 10
                    elif col_name.endswith("_key"):
                        pattern_scores[col_name] += 10
                    elif col_name.endswith("_code"):
                        pattern_scores[col_name] += 8
                    elif col_name.endswith("_number"):
                        pattern_scores[col_name] += 6

            join_keys = {col for col, score in pattern_scores.items() if score >= 6}
            logger.info(f"[KB] Inferred join keys: {sorted(join_keys)}")
            return join_keys

        except Exception as e:
            logger.warning(f"[KB] Failed to infer join keys: {e}")
            return set()

    # ────────────────────────────────────────────────────────────────────
    # CACHE MANAGEMENT
    # ────────────────────────────────────────────────────────────────────

    def clear_cache(self):
        """Clear all caches so next call reloads from DB."""
        self.catalog_cache  = None
        self.examples_cache = None
        self.glossary_cache = None
        self.joins_cache    = None
        logger.info("[KB] Cleared all caches")


# ============================================================================
# FUNCTION-BASED API
# These wrap the class-based KBManager and are called from app.py / poc8.
# They mirror the old kb_manager.py function signatures exactly so no
# other file needs to change.
# ============================================================================

import re
from typing import Optional as _Optional

_KB_STATE_FILE = "kb_state.json"


def _kb_state_path(storage_dir: str) -> str:
    kb_dir = os.path.join(storage_dir, "kb")
    os.makedirs(kb_dir, exist_ok=True)
    return os.path.join(kb_dir, _KB_STATE_FILE)


def should_refresh_kb(storage_dir: str) -> bool:
    """Return True if a timed KB refresh is due (off by default)."""
    interval = int(os.getenv("KB_REFRESH_INTERVAL_SECONDS", "0") or "0")
    if interval <= 0:
        return False
    path = _kb_state_path(storage_dir)
    if not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            state = json.load(f)
        last = float(state.get("last_refresh_epoch", 0))
        return (datetime.utcnow().timestamp() - last) >= interval
    except Exception:
        return True


def _save_kb_state(storage_dir: str, meta: dict):
    path = _kb_state_path(storage_dir)
    meta["last_refresh_epoch"] = datetime.utcnow().timestamp()
    meta["last_refresh_iso"]   = datetime.utcnow().isoformat()
    with open(path, "w") as f:
        json.dump(meta, f, indent=2, default=str)


def persist_kb(storage_dir: str, dataset_id: str, schema_profile: dict,
               table_name: _Optional[str] = None) -> dict:
    """
    Persist an approved schema profile into the KB.
    Wraps KBManager.register_table() so it writes into the main DuckDB.
    Also infers simple join edges between the newly registered table and
    any others already in the catalog.
    """
    kb = get_kb_manager(_DB_FILE_REF[0] if _DB_FILE_REF else storage_dir)
    canonical = table_name or schema_profile.get("table_name") or dataset_id
    ok = kb.register_table(canonical, schema_profile)

    # Infer join edges against existing catalog
    join_edges = 0
    try:
        catalog = kb.load_catalog(force_refresh=True)
        edges   = _infer_join_edges(catalog, canonical)
        if edges:
            conn = duckdb.connect(kb.db_file)
            conn.execute("DELETE FROM kb_joins WHERE table1 = ? OR table2 = ?",
                         [canonical, canonical])
            for e in edges:
                conn.execute(
                    "INSERT INTO kb_joins VALUES (?, ?, ?, ?, ?, ?)",
                    [e["table1"], e["column1"], e["table2"], e["column2"],
                     e["confidence"], e["source"]],
                )
            conn.close()
            kb.joins_cache = None
            join_edges = len(edges)
    except Exception as ex:
        logger.warning(f"[kb] join edge inference failed: {ex}")

    _save_kb_state(storage_dir, {
        "last_dataset_id": dataset_id,
        "tables": list(kb.load_catalog().keys()),
        "join_edges": join_edges,
    })
    return {"success": ok, "documents_written": len(schema_profile.get("schema", [])) + 1,
            "join_edges": join_edges}


def _infer_join_edges(catalog: dict, primary_table: str) -> list:
    """Heuristic join edge inference between primary_table and all others."""
    edges = []
    if primary_table not in catalog:
        return edges

    def col_names(meta):
        return [(c.get("column") or "").lower() for c in meta.get("columns", [])]

    def is_key(col):
        return col == "id" or col.endswith("_id") or col.endswith("_key")

    left_cols = set(filter(is_key, col_names(catalog[primary_table])))

    for other, meta in catalog.items():
        if other == primary_table:
            continue
        right_cols = set(filter(is_key, col_names(meta)))
        shared = left_cols & right_cols
        for col in sorted(shared):
            edges.append({
                "table1": primary_table, "column1": col,
                "table2": other,         "column2": col,
                "confidence": 0.7, "source": "inferred",
            })
            edges.append({
                "table1": other,         "column1": col,
                "table2": primary_table, "column2": col,
                "confidence": 0.7, "source": "inferred",
            })
    return edges


def refresh_kb_from_duckdb(db_file: str, storage_dir: str) -> dict:
    """
    Full re-scan of all tables in the main DuckDB and rebuild KB catalog.
    """
    kb = get_kb_manager(db_file)
    _DB_FILE_REF[0] = db_file

    try:
        conn_ro = duckdb.connect(db_file, read_only=True)
        rows    = conn_ro.execute("""
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
            conn_ro   = duckdb.connect(db_file, read_only=True)
            describe  = conn_ro.execute(f'DESCRIBE "{tname}"').fetchall()
            row_count = conn_ro.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
            conn_ro.close()

            schema_profile = {
                "table_name":    tname,
                "total_rows":    int(row_count),
                "total_columns": len(describe),
                "schema": [
                    {"column": c[0], "data_type": c[1],
                     "business_meaning": "", "nullable": True}
                    for c in describe
                ],
            }
            ok = kb.register_table(tname, schema_profile)
            if ok:
                refreshed.append(tname)
                docs_written += len(describe) + 1
        except Exception as e:
            logger.warning(f"[kb] refresh skipped table={tname}: {e}")

    # Rebuild join edges for all refreshed tables
    join_edges = 0
    try:
        catalog = kb.load_catalog(force_refresh=True)
        conn    = duckdb.connect(db_file)
        conn.execute("DELETE FROM kb_joins")
        all_edges = []
        processed = set()
        for tname in refreshed:
            for e in _infer_join_edges(catalog, tname):
                pair = tuple(sorted([e["table1"], e["table2"], e["column1"]]))
                if pair not in processed:
                    processed.add(pair)
                    all_edges.append(e)
        for e in all_edges:
            conn.execute("INSERT INTO kb_joins VALUES (?, ?, ?, ?, ?, ?)",
                         [e["table1"], e["column1"], e["table2"], e["column2"],
                          e["confidence"], e["source"]])
        conn.close()
        kb.joins_cache = None
        join_edges = len(all_edges)
    except Exception as ex:
        logger.warning(f"[kb] join edge rebuild failed: {ex}")

    _save_kb_state(storage_dir, {
        "tables":       refreshed,
        "join_edges":   join_edges,
        "refresh_mode": "full_duckdb_scan",
    })
    return {
        "success":          True,
        "tables_indexed":   len(refreshed),
        "documents_written": docs_written,
        "join_edges":       join_edges,
        "tables":           refreshed,
    }


def upsert_example_pair(storage_dir: str, dataset_id: str, question: str,
                         sql: str, tables: list, tags: list = None) -> dict:
    """Save a successful NL→SQL pair as a reusable example."""
    kb = get_kb_manager(_DB_FILE_REF[0] if _DB_FILE_REF else storage_dir)
    category = "join" if len(tables) > 1 else "single_table"
    ok = kb.save_example(question, sql, category=category, quality_score=0.85)
    return {"success": ok}


def upsert_join_edges(storage_dir: str, edges: list) -> dict:
    """Persist join edges discovered during a Tab 8 query."""
    kb = get_kb_manager(_DB_FILE_REF[0] if _DB_FILE_REF else storage_dir)
    if not edges:
        return {"success": True, "edges_written": 0}
    try:
        conn = duckdb.connect(kb.db_file)
        inserted = 0
        for e in edges:
            lt = e.get("left_table")  or e.get("table1")
            lc = e.get("left_column") or e.get("column1") or ""
            rt = e.get("right_table") or e.get("table2")
            rc = e.get("right_column") or e.get("column2") or ""
            if not lt or not rt:
                continue
            conf   = float(e.get("confidence") or 0.5)
            source = str(e.get("source") or "tab8")
            conn.execute(
                "DELETE FROM kb_joins WHERE table1=? AND column1=? AND table2=? AND source=?",
                [lt, lc, rt, source],
            )
            conn.execute("INSERT INTO kb_joins VALUES (?, ?, ?, ?, ?, ?)",
                         [lt, lc, rt, rc, conf, source])
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

# Holds the db_file path once first set so function-based helpers can find it
_DB_FILE_REF: list = [None]

_KB_MANAGER_INSTANCE: Optional[KBManager] = None


def get_kb_manager(db_file: str = None) -> KBManager:
    """Get or create the global KBManager instance."""
    global _KB_MANAGER_INSTANCE
    if db_file:
        _DB_FILE_REF[0] = db_file
    if _KB_MANAGER_INSTANCE is None:
        target = db_file or _DB_FILE_REF[0]
        if not target:
            raise RuntimeError("kb_manager: db_file must be provided on first call")
        _KB_MANAGER_INSTANCE = KBManager(target)
    return _KB_MANAGER_INSTANCE


def init_kb_manager(db_file: str) -> KBManager:
    """Initialize (or re-initialize) the global KBManager. Call once at startup."""
    global _KB_MANAGER_INSTANCE
    _DB_FILE_REF[0]       = db_file
    _KB_MANAGER_INSTANCE  = KBManager(db_file)
    # Pre-warm caches
    try:
        _KB_MANAGER_INSTANCE.load_catalog()
        _KB_MANAGER_INSTANCE.load_examples(limit=10)
        _KB_MANAGER_INSTANCE.load_glossary(limit=50)
        _KB_MANAGER_INSTANCE.load_joins()
        stats = _KB_MANAGER_INSTANCE.get_stats()
        logger.info(f"[KB] KBManager initialized: {stats}")
    except Exception as e:
        logger.warning(f"[KB] KBManager pre-warm issue: {e}")
    return _KB_MANAGER_INSTANCE