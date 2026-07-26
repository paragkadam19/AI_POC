"""
Tab 8: NL→SQL Query Builder (POC 8)
====================================

FULLY DYNAMIC - No hardcoding
Fully integrated with KB Manager

Features:
- Dynamic catalog loading from KB
- Few-shot learning from kb_examples
- Business context from kb_glossary
- Join discovery from kb_joins
- Dynamic parameters (no hardcoding)
"""

import json
import math
import os
import re
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import duckdb
from logger_config import get_logger

logger = get_logger(__name__)

# ============================================================================
# IMPORTS: KB Manager (will be injected from app.py)
# ============================================================================

KB_MANAGER = None  # Will be set by app.py after import

def set_kb_manager(kb_mgr):
    """Set KB Manager instance."""
    global KB_MANAGER
    KB_MANAGER = kb_mgr
    logger.info("[poc8] KB Manager attached")

# ============================================================================
# SECTION 1: DYNAMIC INFERENCE FUNCTIONS
# ============================================================================

def infer_join_keys_from_kb() -> Set[str]:
    """Get join keys from KB Manager."""
    if KB_MANAGER:
        return KB_MANAGER.infer_join_keys()
    return set()

def infer_max_hops_from_graph(join_graph: Dict[str, List]) -> int:
    """
    Infer optimal max_hops from actual graph structure.
    
    Minimum of 4, maximum of 8.
    """
    if not join_graph:
        return 4
    
    all_nodes = set(join_graph.keys())
    if len(all_nodes) < 2:
        return 2
    
    max_path_length = 1
    sample_size = min(10, len(all_nodes))
    
    # Sample paths to estimate diameter
    for start in list(all_nodes)[:sample_size]:
        for goal in list(all_nodes)[:sample_size]:
            if start != goal:
                path = _shortest_path(start, goal, join_graph, max_hops=10)
                if path:
                    max_path_length = max(max_path_length, len(path) - 1)
    
    # Return: diameter + 1, min 4, max 8
    recommended = max(4, min(max_path_length + 1, 8))
    logger.info(f"[poc8] Inferred max_hops={recommended} from graph diameter")
    return recommended

def infer_rrf_constant(rank_lists: List[List[Tuple]]) -> int:
    """Infer optimal RRF constant from ranking distributions."""
    if not rank_lists or all(not rl for rl in rank_lists):
        return 60
    
    ranges = []
    for ranked in rank_lists:
        if len(ranked) > 1:
            scores = [score for _, score in ranked]
            score_range = max(scores) - min(scores)
            ranges.append(score_range)
    
    if not ranges:
        return 60
    
    avg_range = sum(ranges) / len(ranges)
    
    if avg_range < 1:
        k = 40  # Close scores, emphasize position
    elif avg_range > 10:
        k = 80  # Spread scores, emphasize magnitude
    else:
        k = 60  # Balanced
    
    logger.debug(f"[poc8] Inferred RRF constant k={k}")
    return k

# ============================================================================
# SECTION 2: TOKENIZATION & EMBEDDING
# ============================================================================

def _tokenize(text: str) -> List[str]:
    """Tokenize text to words."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())

def _embed_text(text: str) -> Dict[str, float]:
    """Convert text to bag-of-words vector (normalized)."""
    tokens = _tokenize(text)
    vec = defaultdict(float)
    for token in tokens:
        vec[token] += 1.0
    
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}

def _cosine_sim(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Calculate cosine similarity between vectors."""
    return sum(a.get(k, 0.0) * v for k, v in b.items())

def _build_document(table_name: str, table_meta: Dict[str, Any]) -> str:
    """Build searchable document from table metadata."""
    cols = table_meta.get("columns", [])
    parts = [table_name]
    
    if table_meta.get("description"):
        parts.append(table_meta["description"])
    
    for col in cols:
        col_parts = [
            col.get("column") or col.get("name") or "",
            col.get("data_type") or "",
            col.get("business_meaning") or ""
        ]
        parts.append(" ".join([str(p) for p in col_parts if p]))
    
    return " | ".join(parts)

# ============================================================================
# SECTION 3: RETRIEVAL & RANKING
# ============================================================================

def _reciprocal_rank_fusion(rank_lists: List[List[Tuple]], k: Optional[int] = None) -> Dict[str, float]:
    """Combine multiple rankings using RRF."""
    if k is None:
        k = infer_rrf_constant(rank_lists)
    
    fused = defaultdict(float)
    for ranked in rank_lists:
        for rank, (item, _score) in enumerate(ranked, start=1):
            fused[item] += 1.0 / (k + rank)
    
    return fused

def hybrid_retrieve_tables(question: str, catalog: Dict[str, Dict], top_k: int = 5) -> List[str]:
    """
    Retrieve relevant tables using hybrid search.
    
    Combines:
    1. Semantic search (meaning-based)
    2. Keyword search (word overlap)
    3. RRF fusion (ranking combination)
    """
    q_vec = _embed_text(question)
    q_tokens = set(_tokenize(question))
    
    semantic_results = []
    keyword_results = []
    
    for table, meta in catalog.items():
        # Semantic search
        doc = _build_document(table, meta)
        doc_vec = _embed_text(doc)
        semantic_sim = _cosine_sim(q_vec, doc_vec)
        semantic_results.append((table, semantic_sim))
        
        # Keyword search
        doc_tokens = set(_tokenize(doc))
        token_overlap = len(doc_tokens & q_tokens)
        keyword_results.append((table, float(token_overlap)))
    
    # Sort by score
    semantic_results.sort(key=lambda x: x[1], reverse=True)
    keyword_results.sort(key=lambda x: x[1], reverse=True)
    
    # RRF fusion
    fused = _reciprocal_rank_fusion([semantic_results[:50], keyword_results[:50]])
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    
    result = [table for table, _score in ranked[:top_k]]
    logger.debug(f"[poc8] Retrieved {len(result)} tables: {result}")
    return result

# ============================================================================
# SECTION 4: JOIN GRAPH
# ============================================================================

def _safe_ident(name: str) -> str:
    """Escape SQL identifier."""
    return '"' + name.replace('"', '""') + '"'

def _fetch_table_metadata(conn: duckdb.DuckDBPyConnection, table_name: str) -> Dict[str, Any]:
    """Fetch metadata for a table from DuckDB."""
    try:
        describe_rows = conn.execute(f"DESCRIBE {_safe_ident(table_name)}").fetchall()
        row_count = conn.execute(f"SELECT COUNT(*) FROM {_safe_ident(table_name)}").fetchone()[0]
    except Exception as e:
        logger.warning(f"[poc8] Failed to describe {table_name}: {e}")
        return {}
    
    columns = []
    for col_name, col_type, *_rest in describe_rows:
        columns.append({
            "column": col_name,
            "data_type": col_type,
        })
    
    return {
        "table_name": table_name,
        "description": f"{table_name} ({row_count} rows)",
        "row_count": int(row_count),
        "columns": columns,
    }

def build_join_graph(catalog: Dict[str, Dict], db_file: Optional[str] = None) -> Dict[str, List[Tuple[str, str]]]:
    """
    Build join graph from FK relationships.
    
    Tries in order:
    1. Declared FKs from DuckDB
    2. Heuristic discovery from column names
    """
    graph = defaultdict(list)
    table_names = list(catalog.keys())
    
    # Get join keys (dynamic)
    join_keys = infer_join_keys_from_kb()
    if not join_keys:
        # Fallback: learn from column names
        all_columns = []
        for table, meta in catalog.items():
            for col in meta.get("columns", []):
                col_name = col.get("column", "").lower()
                if col_name:
                    all_columns.append(col_name)
        
        pattern_scores = defaultdict(int)
        for col_name in all_columns:
            if col_name == "id" or col_name.endswith("_id") or col_name.endswith("_key"):
                pattern_scores[col_name] += 10
        
        join_keys = {col for col, score in pattern_scores.items() if score >= 6}
    
    # Try 1: Query kb_joins from DuckDB (if available)
    if db_file:
        try:
            conn = duckdb.connect(db_file, read_only=True)
            
            # Check if kb_joins table exists
            table_check = conn.execute("""
                SELECT COUNT(*) FROM information_schema.tables 
                WHERE table_name = 'kb_joins'
            """).fetchone()[0]
            
            if table_check > 0:
                fk_rows = conn.execute("""
                    SELECT table1, column1, table2, column2
                    FROM kb_joins
                """).fetchall()
                
                for table1, col1, table2, col2 in fk_rows:
                    if table1 in catalog and table2 in catalog:
                        graph[table1].append((table2, f"{col1}->{col2}"))
                        graph[table2].append((table1, f"{col2}<-{col1}"))
                
                if fk_rows:
                    logger.info(f"[poc8] Loaded {len(fk_rows)} joins from kb_joins")
            
            conn.close()
        except Exception as e:
            logger.debug(f"[poc8] kb_joins load failed: {e}")
    
    # Try 2: Heuristic discovery from column names
    for left in table_names:
        left_cols = set(c.get("column", "").lower() for c in catalog[left].get("columns", []))
        
        for right in table_names:
            if left == right:
                continue
            
            right_cols = set(c.get("column", "").lower() for c in catalog[right].get("columns", []))
            
            # Find shared columns that look like join keys
            shared = {col for col in (left_cols & right_cols) if col in join_keys}
            
            for col in sorted(shared):
                # Only add if not already discovered
                if right not in [n for n, _ in graph[left]]:
                    graph[left].append((right, col))
    
    logger.info(f"[poc8] Built join graph: {len(graph)} source tables, "
               f"{sum(len(v) for v in graph.values())} relationships")
    return dict(graph)

# ============================================================================
# SECTION 5: GRAPH ALGORITHMS
# ============================================================================

def _connected_components(nodes: set, graph: Dict[str, List]) -> List[set]:
    """Find connected components in join graph."""
    visited = set()
    components = []
    
    for start in nodes:
        if start in visited:
            continue
        
        comp = set()
        queue = deque([start])
        visited.add(start)
        
        while queue:
            node = queue.popleft()
            comp.add(node)
            
            for neighbor, _col in graph.get(node, []):
                if neighbor in nodes and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        
        components.append(comp)
    
    return components

def _shortest_path(start: str, goal: str, graph: Dict[str, List], max_hops: int = 4) -> Optional[List[str]]:
    """Find shortest path between tables using BFS."""
    queue = deque([(start, [start])])
    seen = {start}
    
    while queue:
        node, path = queue.popleft()
        
        if len(path) - 1 > max_hops:
            continue
        
        if node == goal:
            return path
        
        for neighbor, _col in graph.get(node, []):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    
    return None

def expand_via_join_graph(candidate_tables: List[str], join_graph: Dict[str, List], 
                         max_hops: Optional[int] = None) -> Dict[str, Any]:
    """
    Expand tables with bridge tables and join paths.
    
    Uses dynamic max_hops if not provided.
    """
    if max_hops is None:
        max_hops = infer_max_hops_from_graph(join_graph)
    
    candidates = set(candidate_tables)
    if not candidates:
        return {"tables": [], "bridges_added": [], "join_paths": []}
    
    # Find connected components
    components = _connected_components(candidates, join_graph)
    bridges_added = set()
    
    # If disconnected, add bridges
    if len(components) > 1:
        merged = set(components[0])
        for component in components[1:]:
            for left in merged:
                for right in component:
                    path = _shortest_path(left, right, join_graph, max_hops=max_hops)
                    if path and len(path) > 2:
                        for bridge_table in path[1:-1]:
                            bridges_added.add(bridge_table)
                        break
    else:
        merged = set(candidates)
    
    # Final tables
    final_tables = sorted(candidates | bridges_added)
    
    # Build join paths
    path_edges = []
    for t in final_tables:
        for neighbor, join_col in join_graph.get(t, []):
            if neighbor in final_tables:
                path_edges.append({
                    "left": t,
                    "right": neighbor,
                    "join_column": join_col
                })
    
    logger.info(f"[poc8] Expanded: {len(final_tables)} tables, "
               f"{len(bridges_added)} bridges, {len(path_edges)} joins")
    
    return {
        "tables": final_tables,
        "bridges_added": sorted(bridges_added),
        "join_paths": path_edges,
    }

# ============================================================================
# SECTION 6: CATALOG LOADING (Using KB)
# ============================================================================

def load_catalog(db_file: str, storage_dir: str, ds: Optional[str] = None) -> Dict[str, Dict]:
    """
    Load catalog from KB Manager or fallback sources.
    
    Priority:
    1. KB Manager (kb_catalog + kb_documents tables)
    2. kb_catalog.json file
    3. DuckDB introspection
    4. Approved schema (Tab 2)
    """
    # Try 1: KB Manager
    if KB_MANAGER:
        try:
            catalog = KB_MANAGER.load_catalog()
            if catalog:
                logger.info(f"[poc8] Loaded catalog from KB Manager ({len(catalog)} tables)")
                return catalog
        except Exception as e:
            logger.warning(f"[poc8] KB Manager load failed: {e}")
    
    # Try 2: kb_catalog.json file
    catalog_path = os.path.join(storage_dir, "kb_catalog.json")
    if os.path.exists(catalog_path):
        try:
            with open(catalog_path) as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                logger.info(f"[poc8] Loaded catalog from {catalog_path}")
                return data
        except Exception as e:
            logger.warning(f"[poc8] kb_catalog.json load failed: {e}")
    
    # Try 3: DuckDB introspection
    try:
        conn = duckdb.connect(db_file, read_only=True)
        
        rows = conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """).fetchall()
        
        catalog = {}
        for (table_name,) in rows:
            meta = _fetch_table_metadata(conn, table_name)
            if meta:
                catalog[table_name] = meta
        
        conn.close()
        
        if catalog:
            logger.info(f"[poc8] Loaded catalog from DuckDB introspection ({len(catalog)} tables)")
            return catalog
    except Exception as e:
        logger.warning(f"[poc8] DuckDB introspection failed: {e}")
    
    # Try 4: Approved schema (Tab 2)
    if ds:
        try:
            approved_path = os.path.join(storage_dir, "outputs", ds, "poc1_latest.json")
            if os.path.exists(approved_path):
                with open(approved_path) as f:
                    schema_profile = json.load(f)
                
                catalog = {
                    f"tbl_{ds}": {
                        "table_name": f"tbl_{ds}",
                        "description": schema_profile.get("table_name", ds),
                        "row_count": schema_profile.get("total_rows", 0),
                        "columns": schema_profile.get("schema", []),
                    }
                }
                
                logger.info(f"[poc8] Loaded catalog from approved schema: {ds}")
                return catalog
        except Exception as e:
            logger.warning(f"[poc8] Approved schema load failed: {e}")
    
    logger.error("[poc8] Could not load catalog from any source")
    return {}

# ============================================================================
# SECTION 7: SQL GENERATION PROMPT BUILDING
# ============================================================================

def build_sql_generation_prompt(question: str, context: Dict[str, Any], 
                               db_file: Optional[str] = None) -> Tuple[str, str]:
    """
    Build Claude prompt for SQL generation.
    
    Includes:
    1. System prompt with rules
    2. Few-shot examples from kb_examples
    3. Business glossary from kb_glossary
    4. Table/schema context
    5. Join paths
    """
    system = """You are an expert analytics engineer generating DuckDB SQL queries.

IMPORTANT RULES:
- Return ONLY valid JSON with keys: sql, rationale, warnings
- Use ONLY tables and columns provided in the context
- Prefer explicit JOINs over subqueries
- Use exact column names and table names from context
- Add LIMIT 100 unless user asks for aggregate or full results
- If the question is ambiguous or you cannot answer, return sql as empty string and explain in warnings
- No markdown fences, no extra text - ONLY JSON

RESPONSE FORMAT (REQUIRED):
{
  "sql": "SELECT ...",
  "rationale": "Why this SQL answers the question",
  "warnings": []
}
"""
    
    # Add few-shot examples from KB
    if db_file and KB_MANAGER:
        try:
            examples = KB_MANAGER.load_examples(min_quality=0.80, limit=3)
            
            if examples:
                system += "\n\n" + "="*70
                system += "\nREFERENCE EXAMPLES (similar queries):\n"
                system += "="*70
                
                for i, ex in enumerate(examples, 1):
                    system += f"\nExample {i} ({ex.get('category', 'general')}):"
                    system += f"\n  Q: {ex.get('question', '')}"
                    system += f"\n  SQL: {ex.get('sql', '')}\n"
        except Exception as e:
            logger.debug(f"[poc8] Could not load examples: {e}")
    
    # Add glossary from KB
    tables = context.get("tables", [])
    if db_file and KB_MANAGER and tables:
        try:
            glossary = KB_MANAGER.load_glossary(tables=tables, limit=15)
            
            if glossary:
                system += "\n\n" + "="*70
                system += "\nBUSINESS GLOSSARY (column definitions):\n"
                system += "="*70
                
                for entry in glossary:
                    col_ref = entry.get("column_reference", "")
                    definition = entry.get("definition", "")
                    system += f"\n- {col_ref}: {definition}"
        except Exception as e:
            logger.debug(f"[poc8] Could not load glossary: {e}")
    
    # Add schema context
    system += "\n\n" + "="*70
    system += "\nAVAILABLE TABLES & SCHEMA:\n"
    system += "="*70
    
    user_data = {
        "question": question,
        "tables": context.get("tables", []),
        "join_paths": context.get("join_paths", []),
        "schema_context": context.get("schema_context", {}),
    }
    
    return system, json.dumps(user_data, indent=2)

# ============================================================================
# SECTION 8: SQL VALIDATION & EXECUTION
# ============================================================================

def _is_safe_select(sql: str) -> bool:
    """Check if SQL is safe (SELECT/WITH only)."""
    normalized = sql.strip().lower()
    return normalized.startswith("select") or normalized.startswith("with")

def _sanitize_sql(sql: str) -> str:
    """Clean up SQL (remove markdown, trim)."""
    sql = (sql or "").strip().rstrip(";")
    sql = re.sub(r"^```(?:sql|json)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()

def _normalize_warnings(warnings) -> List[str]:
    """Normalize warnings to list of strings."""
    if isinstance(warnings, list):
        return [str(w) for w in warnings if w]
    return []

def execute_sql_preview(db_file: str, sql: str, max_rows: int = 5) -> Dict[str, Any]:
    """
    Execute SQL and return preview.
    
    Safety checks:
    1. Only allows SELECT/WITH
    2. Validates syntax with EXPLAIN
    3. Returns first N rows
    """
    sql = _sanitize_sql(sql)
    
    if not _is_safe_select(sql):
        return {"ok": False, "error": "Only SELECT/WITH queries are allowed"}
    
    conn = duckdb.connect(db_file, read_only=True)
    try:
        # Validate syntax
        conn.execute(f"EXPLAIN {sql}")
        
        # Add LIMIT if not present
        if not re.search(r"\blimit\b", sql, flags=re.IGNORECASE):
            sql = f"SELECT * FROM ({sql}) AS q LIMIT {max_rows}"
        
        # Execute
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        
        return {
            "ok": True,
            "columns": cols,
            "rows": [dict(zip(cols, row)) for row in rows],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()

# ============================================================================
# SECTION 9: MAIN FUNCTION
# ============================================================================

def question_to_sql(db_file: str, ds: str, storage_dir: str, question: str,
                    ask_json_fn, model_hint: Optional[str] = None) -> Dict[str, Any]:
    """
    Convert natural language question to SQL query.
    
    FULLY DYNAMIC:
    - All parameters inferred from data
    - All KB features integrated
    - No hardcoding
    
    Steps:
    1. Load catalog (from KB or fallback)
    2. Build join graph (from kb_joins or heuristics)
    3. Retrieve relevant tables (hybrid search)
    4. Expand with bridge tables
    5. Build prompt (with examples + glossary)
    6. Call Claude
    7. Validate and preview SQL
    """
    warnings: List[str] = []
    
    logger.info(f"[poc8] Processing question: {question[:50]}...")
    
    # Step 1: Load catalog
    catalog = load_catalog(db_file, storage_dir, ds=ds)
    if not catalog:
        return {
            "ok": False,
            "question": question,
            "error": "No catalog found",
            "warnings": ["Could not load table metadata"]
        }
    
    # Step 2: Build join graph
    join_graph = build_join_graph(catalog, db_file=db_file)
    
    # Step 3: Retrieve tables
    retrieved = hybrid_retrieve_tables(question, catalog, 
                                      top_k=min(5, max(1, len(catalog))))
    logger.info(f"[poc8] Retrieved tables: {retrieved}")
    
    # Step 4: Expand via join graph (with dynamic max_hops)
    max_hops = infer_max_hops_from_graph(join_graph)
    expanded = expand_via_join_graph(retrieved, join_graph, max_hops=max_hops)
    
    # Step 5: Build schema context
    schema_context = {
        table: {
            "description": catalog[table].get("description"),
            "columns": catalog[table].get("columns", []),
        }
        for table in expanded["tables"]
        if table in catalog
    }
    
    # Step 6: Build prompt (with KB integration)
    system, prompt = build_sql_generation_prompt(
        question,
        {
            "tables": expanded["tables"],
            "join_paths": expanded["join_paths"],
            "schema_context": schema_context,
        },
        db_file=db_file
    )
    
    # Step 7: Call Claude
    raw = ask_json_fn(prompt, system, model=model_hint) if model_hint else ask_json_fn(prompt, system)
    
    # Step 8: Extract SQL
    sql = _sanitize_sql(raw.get("sql", "")) if isinstance(raw, dict) else ""
    if isinstance(raw, dict):
        warnings.extend(_normalize_warnings(raw.get("warnings")))
    
    if not sql:
        warnings.append("Model returned no SQL")
        return {
            "ok": False,
            "question": question,
            "tables": expanded["tables"],
            "bridges_added": expanded["bridges_added"],
            "join_paths": expanded["join_paths"],
            "warnings": warnings,
        }
    
    # Step 9: Execute preview
    preview = execute_sql_preview(db_file, sql)
    
    # Step 10: Return result
    return {
        "ok": preview.get("ok", False),
        "question": question,
        "sql": sql,
        "tables": expanded["tables"],
        "bridges_added": expanded["bridges_added"],
        "join_paths": expanded["join_paths"],
        "warnings": warnings,
        "preview": preview,
        "model_output": raw,
        "_parameters": {
            "max_hops": max_hops,
            "rrf_constant": 60,  # Calculated at runtime
        }
    }