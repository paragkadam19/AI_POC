"""
mfg_poc8_nl_sql.py — Natural Language → SQL Query Builder (+ Chart Recommendation)
====================================================================================
Tab 8: Convert natural language questions into DuckDB SQL, execute them, and
recommend a chart spec (chart type + axis mapping) so the frontend can render
a graph instead of just a table.

Flow:
  1. Retrieve relevant tables using KB Manager (vector semantic search)
  2. Expand via join graph to connect tables
  3. Build schema context from retrieved tables
  4. Send to Claude with few-shot examples
  5. Claude generates SQL
  6. Execute and return results + metadata
  7. Recommend a chart spec (rule-based, with optional LLM refinement)
          for the result set and attach it to the response.

Revision history:
  - Added `recommend_chart_spec()` / `prepare_chart_dataset()` and wired them
    into `question_to_sql()` via a `want_chart` flag. Charting is purely
    additive and fails safe (falls back to "table" chart_type) if anything
    goes wrong.
  - Fixed a mis-classification bug: grouping keys that happen to be numeric
    (e.g. an "age" column derived from DATE_PART) were being treated as the
    chart's numeric *measure* instead of the real aggregate (COUNT/SUM/...),
    which produced nonsensical bar charts (e.g. state vs. age instead of
    state vs. applicant count). `_infer_column_dtypes()` now detects the
    true aggregate column from the SQL text and demotes other numeric
    columns to categorical dimensions in GROUP BY queries.
"""

import json
import re
import os
import difflib
from collections import defaultdict
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import duckdb

from logger_config import get_logger

logger = get_logger(__name__)
DEBUG_NLQ_PROMPTS = os.getenv("DEBUG_NLQ_PROMPTS", "").lower() in {"1", "true", "yes"}
AUDIT_COLUMNS = {"system_date", "system_active", "file_path"}
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MAX_CATEGORIES = 15  # cap for bar/pie chart categories before we bucket the long tail into "Other"
CHART_INTENT_WORDS = {
    "trend", "trends", "compare", "comparison", "breakdown", "distribution",
    "over time", "month over month", "mom", "yoy", "growth", "chart",
    "graph", "visualize", "visualisation", "visualization", "plot", "by month",
    "by day", "by week", "by year", "top", "rank", "group by"
}
# Global KB Manager ref (wired in by app.py via set_kb_manager)
_KB_MANAGER: Optional[Any] = None


def set_kb_manager(kb_manager):
    """Called by app.py to wire in the KB Manager singleton."""
    global _KB_MANAGER
    _KB_MANAGER = kb_manager
    logger.info("[poc8] KB Manager wired in")


# ============================================================================
# RETRIEVAL: Vector semantic search via KB Manager
# ============================================================================

def retrieve_tables_vector(question: str, top_k: int = 20) -> Tuple[List[str], List[str]]:
    """
    Use KB Manager's vector semantic search to find relevant tables.
    Returns (table_names, warnings).

    Retrieves documents (columns or tables) most similar to the question
    using DuckDB VSS HNSW cosine search over Titan embeddings.
    """
    warnings = []

    if not _KB_MANAGER:
        logger.warning("[poc8] KB Manager not available — no semantic retrieval")
        return [], ["KB Manager not initialized"]

    try:
        catalog = _KB_MANAGER.load_catalog(force_refresh=False) or {}
        q = (question or "").lower()
        exact_tables = [
            t for t in catalog.keys()
            if t and (t.lower() in q or q in t.lower())
        ]

        # Real vector retrieval via kb_manager
        results = _KB_MANAGER.retrieve_by_vector(question, top_k=top_k)

        if not results:
            if exact_tables:
                return exact_tables[:5], []
            logger.warning("[poc8] VSS returned no results — KB may be empty")
            return [], ["No matching tables found in KB"]

        q_tokens = {tok for tok in re.findall(r"[a-z0-9_]+", q) if len(tok) >= 3}

        # Keep the best score per table and boost exact table-name matches.
        table_scores: Dict[str, float] = {}
        for doc in results:
            tname = doc.get("table_name")
            score = float(doc.get("score", 0.0) or 0.0)
            if not tname:
                continue
            if score < 0.05:
                continue
            tname_l = str(tname).lower()
            if any(tok == tname_l or tok in tname_l for tok in q_tokens):
                score += 0.35
            if tname not in table_scores or score > table_scores[tname]:
                table_scores[tname] = score
            logger.debug(
                f"[poc8] Vector retrieved: {tname} "
                f"(doc_type={doc.get('doc_type')}, "
                f"score={score:.3f})"
            )

        tables = [t for t, _ in sorted(table_scores.items(), key=lambda item: item[1], reverse=True)]
        for t in reversed(exact_tables):
            if t not in tables:
                tables.insert(0, t)
        if not tables:
            warnings.append("Vector search found documents but no table names extracted")
            return [], warnings

        tables = tables[:5]
        logger.info(f"[poc8] Vector retrieval found {len(tables)} tables from {len(results)} documents")
        return tables, warnings

    except Exception as e:
        msg = f"Vector retrieval failed, falling back to keyword search: {e}"
        logger.warning(f"[poc8] {msg}")
        warnings.append(msg)
        return [], warnings


# ============================================================================
# JOIN GRAPH: Confidence-aware expansion
# ============================================================================

def _shortest_path(
    start: str,
    goal: str,
    graph: Dict[str, List[Tuple[str, str]]],
    max_hops: int = 5,
    min_confidence: float = 0.65,
    edge_confidence: Dict[Tuple[str, str], float] = None,
) -> Optional[List[str]]:
    """
    BFS shortest path through edges with confidence >= min_confidence.
    Low-confidence edges (heuristics) are blocked by default.
    """
    from collections import deque

    edge_confidence = edge_confidence or {}
    queue = deque([(start, [start])])
    seen = {start}

    while queue:
        node, path = queue.popleft()
        if len(path) - 1 > max_hops:
            continue
        if node == goal:
            return path

        for neighbor, _col in graph.get(node, []):
            if neighbor in seen:
                continue
            conf = edge_confidence.get((node, neighbor), 0.0)
            if conf < min_confidence:
                continue
            seen.add(neighbor)
            queue.append((neighbor, path + [neighbor]))

    return None


def _connected_components(nodes: set, graph: Dict) -> List[set]:
    """Find connected components in an undirected graph."""
    from collections import deque

    visited, components = set(), []
    for start in nodes:
        if start in visited:
            continue
        comp, queue = set(), deque([start])
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


def expand_via_join_graph(
    candidate_tables: List[str],
    join_graph: Dict[str, List[Tuple[str, str]]],
    edge_confidence: Dict[Tuple[str, str], float] = None,
    max_hops: int = 5,
    min_bridge_confidence: float = 0.65,
) -> Dict[str, Any]:
    """
    Bridge disconnected table components ONLY through high-confidence edges.
    Returns {tables, bridges_added, join_paths, unresolved_components}.
    """
    candidates = set(candidate_tables)
    edge_confidence = edge_confidence or {}

    if not candidates:
        return {
            "tables": [],
            "bridges_added": [],
            "join_paths": [],
            "unresolved_components": [],
        }

    components = _connected_components(candidates, join_graph)
    bridges_added = set()
    unresolved_components: List[List[str]] = []

    if len(components) > 1:
        merged = set(components[0])
        for component in components[1:]:
            bridge_found = False
            for left in merged:
                for right in component:
                    path = _shortest_path(
                        left, right, join_graph,
                        max_hops=max_hops,
                        min_confidence=min_bridge_confidence,
                        edge_confidence=edge_confidence,
                    )
                    if path:
                        for bridge_table in path[1:-1]:
                            bridges_added.add(bridge_table)
                        bridge_found = True
                        break
                if bridge_found:
                    break

            if bridge_found:
                merged |= component
            else:
                # No confident path — report it, don't guess
                unresolved_components.append(sorted(component))
    else:
        merged = set(candidates)

    final_tables = sorted(candidates | bridges_added)
    path_edges = []
    seen_pairs = set()

    for t in final_tables:
        for neighbor, join_col in join_graph.get(t, []):
            if neighbor in final_tables:
                pair = tuple(sorted([t, neighbor]))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    path_edges.append({
                        "left": t,
                        "right": neighbor,
                        "join_column": join_col,
                        "confidence": edge_confidence.get((t, neighbor), 0.0),
                    })

    return {
        "tables": final_tables,
        "bridges_added": sorted(bridges_added),
        "join_paths": path_edges,
        "unresolved_components": unresolved_components,
    }


# ============================================================================
# SCHEMA CONTEXT BUILDING
# ============================================================================

def _build_document(table_meta: dict) -> str:
    """
    Build a rich text document from table metadata for schema context.
    Includes table name, column names, types, meanings, samples.
    """
    tname = table_meta.get("table_name", "unknown")
    cols = table_meta.get("columns", [])

    lines = [f"TABLE: {tname}"]
    for col in cols:
        cname = col.get("column", "")
        if cname.lower() in AUDIT_COLUMNS:
            continue
        ctype = col.get("data_type", "")
        meaning = col.get("business_meaning", "")
        sample = " | ".join(str(v) for v in (col.get("sample_values") or [])[:2])

        parts = [cname, ctype]
        if meaning:
            parts.append(f"({meaning})")
        if sample:
            parts.append(f"e.g. {sample}")

        lines.append("  " + " ".join(parts))

    return "\n".join(lines)


def _build_schema_context(catalog: Dict[str, Any], tables: List[str]) -> str:
    """Build schema context string from catalog for specified tables."""
    docs = []
    for tname in sorted(tables):
        if tname in catalog:
            docs.append(_build_document(catalog[tname]))

    return "\n\n".join(docs) if docs else "(No schema found for selected tables)"


def _load_semantic_layer_context(storage_dir: str) -> str:
    try:
        from kb_manager import load_semantic_layer
        doc = load_semantic_layer(storage_dir)
        return doc.strip()
    except Exception as e:
        logger.warning(f"[poc8] semantic layer load failed: {e}")
        return ""


def _filter_semantic_layer_for_tables(semantic_layer: str, tables: List[str]) -> str:
    if not semantic_layer.strip() or not tables:
        return ""

    wanted = set(tables)
    lines = semantic_layer.splitlines()
    blocks = []
    current = []
    current_table = None

    def flush():
        nonlocal current, current_table
        if current_table in wanted and current:
            blocks.append("\n".join(current).strip())
        current = []
        current_table = None

    for line in lines:
        if line.startswith("### "):
            flush()
            current_table = line[4:].strip()
            current = [line]
        elif current_table is not None:
            current.append(line)

    flush()
    return "\n\n".join(blocks).strip()


def _repair_sql_column_names(sql: str, catalog: Dict[str, Any], tables: List[str]) -> str:
    if not sql or not catalog or not tables:
        return sql

    table_names = {str(t).strip().lower() for t in tables if t}
    known_columns = []
    for table_name in tables:
        meta = catalog.get(table_name) or {}
        for col in meta.get("columns", []):
            actual = str(col.get("column") or "").strip()
            if not actual:
                continue
            known_columns.append(actual)

    def repl(match):
        token = match.group(0)
        lowered = token.lower()
        if lowered in {"select", "from", "where", "join", "left", "right", "inner", "outer", "on", "and", "or", "as", "group", "by", "order", "limit", "partition", "over", "row_number", "with", "distinct", "case", "when", "then", "else", "end", "true", "false"}:
            return token
        if lowered in table_names:
            return token
        if token in known_columns:
            return token
        candidate = difflib.get_close_matches(token, known_columns, n=1, cutoff=0.65)
        return candidate[0] if candidate else token

    pattern = r"\b[a-zA-Z][a-zA-Z0-9_]*\b"
    return re.sub(pattern, repl, sql)


def _build_expected_complexity_hint(question: str) -> str:
    q = (question or "").lower()
    hints = []
    if any(tok in q for tok in ("growth", "trend", "month over month", "m-o-m", "mom", "yoy", "year over year")):
        hints.append("window function (LAG/LEAD) or time-series logic")
    if any(tok in q for tok in ("average", "sum", "total", "count", "minimum", "maximum", "revenue", "orders", "sales")):
        hints.append("aggregate fields")
    if any(tok in q for tok in ("segment", "region", "category", "premium", "east", "west", "north", "south")):
        hints.append("filters + grouping by segment/region")
    if any(tok in q for tok in ("join", "customers", "orders", "sales_performance", "order_items", "applicant")):
        hints.append("JOIN between related tables")
    if any(tok in q for tok in ("ordered by", "order by", "sorted", "rank", "top", "latest")):
        hints.append("ORDER BY")
    if not hints:
        return ""
    return "*Expected Complexity*: " + ", ".join(dict.fromkeys(hints))


def _repair_sql_with_error(
    question: str,
    sql: str,
    error_text: str,
    catalog: Dict[str, Any],
    tables: List[str],
    ask_json_fn,
) -> str:
    repair_system = """You are a DuckDB SQL repair agent.
Return ONLY valid JSON.
Use only exact table and column names from the provided schema.
Fix the SQL so it executes in DuckDB.
Do not invent joins, columns, or tables."""
    schema_context = _build_schema_context(catalog, tables)
    repair_prompt = f"""
QUESTION:
{question}

ORIGINAL_SQL:
{sql}

DUCKDB_ERROR:
{error_text}

SCHEMA:
{schema_context}

Return JSON exactly like:
{{"sql":"..."}}"""
    repaired = ask_json_fn(repair_prompt, repair_system,model="us.anthropic.claude-sonnet-4-6")
    if not repaired:
        return ""
    return (repaired.get("sql") or repaired.get("query") or "").strip()


# ============================================================================
# CHART SPEC RECOMMENDATION
# ============================================================================
# Design: don't ask the LLM to draw the chart — decide chart_type + axis
# mapping only, and let the frontend render with a charting library
# (Recharts / Chart.js / Plotly). Rule-based by default (free, deterministic,
# instant); escalates to a single lightweight LLM call only when the question
# has visualization-intent language the rules can't disambiguate on their own
# (e.g. "trend" vs "breakdown"). Every spec is validated against the actual
# result columns before being returned, so a hallucinated column name can
# never reach the frontend.

_VIZ_INTENT_TOKENS = (
    "trend", "distribution", "compare", "comparison", "breakdown", "over time",
    "share", "proportion", "correlation", "relationship", "pattern", "top ",
    "rank", "heatmap", "histogram", "pie chart", "line chart", "bar chart",
)

CHART_SPEC_SYSTEM_PROMPT = """You are a data visualization assistant.
Given a user's question, the SQL that answered it, and the shape of the result
set (column names + inferred dtypes + row count), recommend the single best
chart type to visualize this data.

Rules:
- 1 categorical column + 1 numeric column -> bar chart
- a datetime column + 1+ numeric columns -> line chart
- 1 categorical column + 1 numeric column, and the numeric values are shares
  of a whole (percentages, counts of a small set of categories), <=8 categories -> pie chart
- 2 numeric columns, many rows (>50), no time/category axis -> scatter plot
- 1 numeric column, many rows, no category/time axis -> histogram
- 2 categorical columns + 1 numeric measure -> heatmap
- a single row with 1 numeric value -> kpi (a number card, not a chart)
- if nothing above cleanly fits, or the user explicitly asked for a table/list -> table

Output STRICTLY as JSON matching this schema, and nothing else:
{"chart_type": "bar" | "line" | "pie" | "scatter" | "histogram" | "heatmap" | "kpi" | "table",
 "x_axis": "<exact column_name or null>",
 "y_axis": "<exact column_name or null>",
 "series": "<exact column_name or null, for multi-series/grouped charts>",
 "aggregation": "sum" | "avg" | "count" | "none",
 "title": "<short descriptive title>"}

Only use column names that appear exactly in the provided result columns list.
Never invent a column name."""


def _looks_like_date(value: Any) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    if isinstance(value, str):
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}", value))
    return False


# --- Measure detection -------------------------------------------------
# A column's Python dtype (int/float) alone doesn't tell you whether it's
# the thing being measured (a COUNT/SUM/AVG/...) or a grouping key that
# just happens to be numeric (age, year, month, rank, a coded category...).
# For GROUP BY / aggregate queries we detect the real measure column(s) by
# alias in the SQL text and use that to correct dtype inference below.
_AGG_ALIAS_RE = re.compile(
    r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\)\s+AS\s+(\w+)",
    re.IGNORECASE,
)


def _detect_measure_columns(sql: str, columns: List[str]) -> List[str]:
    """Columns that are real SQL aggregates (COUNT/SUM/AVG/MIN/MAX ... AS alias),
    even though their Python dtype looks like a plain number."""
    if not sql:
        return []
    aliases = {a.lower() for a in _AGG_ALIAS_RE.findall(sql)}
    if not aliases:
        return []
    return [c for c in columns if c.lower() in aliases]


def _infer_column_dtypes(
    columns: List[str],
    rows: List[Dict[str, Any]],
    sql: str = "",
    sample_size: int = 200,
) -> Dict[str, str]:
    """
    Lightweight semantic dtype inference from already-fetched rows
    (no extra DB round trip). Returns one of:
    numeric | categorical | text | datetime | boolean | unknown

    `sql` is optional but recommended: in a GROUP BY / aggregate query, any
    numeric-typed column that ISN'T the detected aggregate (COUNT/SUM/...)
    is demoted to "categorical" if it has low cardinality. Without this, a
    numeric grouping key (e.g. an age computed via DATE_PART) can get
    mistaken for the chart's measure instead of the real COUNT/SUM column,
    producing a nonsensical axis pairing (e.g. state-vs-age instead of
    state-vs-applicant-count).
    """
    dtypes: Dict[str, str] = {}
    sample = rows[:sample_size]

    for col in columns:
        values = [r.get(col) for r in sample if r.get(col) is not None]
        if not values:
            dtypes[col] = "unknown"
            continue

        v0 = values[0]
        if isinstance(v0, bool):
            dtypes[col] = "boolean"
        elif isinstance(v0, (int, float)):
            dtypes[col] = "numeric"
        elif _looks_like_date(v0):
            dtypes[col] = "datetime"
        else:
            distinct = len({str(v) for v in values})
            dtypes[col] = "categorical" if distinct <= max(20, int(len(values) * 0.5)) else "text"

    # --- Demote non-measure numeric columns to categorical dimensions ---
    measure_cols = set(_detect_measure_columns(sql, columns))
    if measure_cols and "GROUP BY" in (sql or "").upper():
        for col in columns:
            if col in measure_cols or dtypes.get(col) != "numeric":
                continue
            distinct = len({str(r.get(col)) for r in sample if r.get(col) is not None})
            if distinct <= max(20, int(len(sample) * 0.5)):
                dtypes[col] = "categorical"
                logger.debug(f"[poc8] chart dtype: demoted numeric grouping key '{col}' to categorical (measure={sorted(measure_cols)})")

    return dtypes


def _rule_based_chart_spec(
    question: str,
    columns: List[str],
    dtypes: Dict[str, str],
    row_count: int,
) -> Dict[str, Any]:
    """Deterministic heuristic chart_type + axis selection. No LLM call."""
    q = (question or "").lower()
    numeric_cols = [c for c in columns if dtypes.get(c) == "numeric"]
    categorical_cols = [c for c in columns if dtypes.get(c) == "categorical"]
    datetime_cols = [c for c in columns if dtypes.get(c) == "datetime"]

    spec: Dict[str, Any] = {
        "chart_type": "table",
        "x_axis": None,
        "y_axis": None,
        "series": None,
        "aggregation": "none",
        "title": (question or "Result").strip().capitalize(),
    }

    if row_count == 0:
        return spec

    if row_count == 1 and len(numeric_cols) >= 1 and len(columns) <= 3:
        spec["chart_type"] = "kpi"
        spec["y_axis"] = numeric_cols[0]
        return spec

    if datetime_cols and numeric_cols:
        spec["chart_type"] = "line"
        spec["x_axis"] = datetime_cols[0]
        spec["y_axis"] = numeric_cols[0]
        if len(numeric_cols) > 1:
            spec["series"] = numeric_cols[1]
        return spec

    if len(categorical_cols) == 2 and numeric_cols:
        spec["chart_type"] = "heatmap"
        spec["x_axis"] = categorical_cols[0]
        spec["y_axis"] = categorical_cols[1]
        spec["series"] = numeric_cols[0]
        return spec

    if len(categorical_cols) == 1 and len(numeric_cols) == 1:
        share_language = any(tok in q for tok in ("share", "percentage", "%", "proportion", "breakdown of", "distribution of"))
        spec["chart_type"] = "pie" if share_language else "bar"
        spec["x_axis"] = categorical_cols[0]
        spec["y_axis"] = numeric_cols[0]
        return spec

    if not categorical_cols and not datetime_cols and len(numeric_cols) == 1 and row_count > 20:
        spec["chart_type"] = "histogram"
        spec["x_axis"] = numeric_cols[0]
        return spec

    if not categorical_cols and not datetime_cols and len(numeric_cols) >= 2 and row_count > 20:
        spec["chart_type"] = "scatter"
        spec["x_axis"] = numeric_cols[0]
        spec["y_axis"] = numeric_cols[1]
        return spec

    if categorical_cols and numeric_cols:
        spec["chart_type"] = "bar"
        spec["x_axis"] = categorical_cols[0]
        spec["y_axis"] = numeric_cols[0]
        return spec

    return spec


def _has_visualization_intent(question: str) -> bool:
    q = (question or "").lower()
    return any(tok in q for tok in _VIZ_INTENT_TOKENS)


def _llm_chart_spec(
    question: str,
    sql: str,
    columns: List[str],
    dtypes: Dict[str, str],
    row_count: int,
    ask_json_fn,
    model: str = "us.anthropic.claude-sonnet-4-6",
) -> Optional[Dict[str, Any]]:
    """One lightweight LLM call: schema + row count only, never the actual data rows."""
    user_prompt = f"""
Question: {question}
SQL: {sql}
Result columns (name: dtype): {json.dumps(dtypes)}
Result row count: {row_count}
"""
    try:
        return ask_json_fn(user_prompt, CHART_SPEC_SYSTEM_PROMPT, model=model)
    except Exception as e:
        logger.warning(f"[poc8] chart LLM call failed: {e}")
        return None


def recommend_chart_spec(
    question: str,
    sql: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    ask_json_fn=None,
    use_llm: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Decide chart_type + axis mapping for a NL->SQL result set.

    Rule-based by default (fast, free, deterministic). Escalates to a single
    LLM call only when the question has visualization-intent language the
    rules can't disambiguate (e.g. "trend" vs "breakdown"), or when
    use_llm=True is forced. Always validates the final spec against the
    actual result columns and caps pie/bar category counts, so a bad LLM
    response degrades to the rule-based spec instead of breaking the chart.

    Returns:
        {
          "chart_type": "bar"|"line"|"pie"|"scatter"|"histogram"|"heatmap"|"kpi"|"table",
          "x_axis": str|None, "y_axis": str|None, "series": str|None,
          "aggregation": str, "title": str, "warnings": [str, ...]
        }
    """
    warnings: List[str] = []
    row_count = len(rows)
    dtypes = _infer_column_dtypes(columns, rows, sql=sql)

    spec = _rule_based_chart_spec(question, columns, dtypes, row_count)

    should_try_llm = use_llm if use_llm is not None else _has_visualization_intent(question)
    if should_try_llm and ask_json_fn and row_count > 0:
        llm_spec = _llm_chart_spec(question, sql, columns, dtypes, row_count, ask_json_fn)
        if llm_spec and llm_spec.get("chart_type"):
            spec = {
                "chart_type": llm_spec.get("chart_type", spec["chart_type"]),
                "x_axis": llm_spec.get("x_axis"),
                "y_axis": llm_spec.get("y_axis"),
                "series": llm_spec.get("series"),
                "aggregation": llm_spec.get("aggregation", "none"),
                "title": llm_spec.get("title") or spec["title"],
            }
        else:
            warnings.append("LLM chart recommendation unavailable; used rule-based spec")

    # --- Guardrail: every referenced column must actually exist in the result set ---
    valid_cols = set(columns)
    for axis_key in ("x_axis", "y_axis", "series"):
        val = spec.get(axis_key)
        if val and val not in valid_cols:
            match = difflib.get_close_matches(val, columns, n=1, cutoff=0.6)
            if match:
                spec[axis_key] = match[0]
            else:
                warnings.append(f"Chart spec referenced unknown column '{val}' for {axis_key}; cleared")
                spec[axis_key] = None

    # --- Guardrail: cap category count for bar/pie ---
    if spec["chart_type"] in ("bar", "pie") and spec.get("x_axis") in valid_cols:
        distinct_vals = {str(r.get(spec["x_axis"])) for r in rows}
        if len(distinct_vals) > MAX_CATEGORIES:
            if spec["chart_type"] == "pie":
                spec["chart_type"] = "bar"
                warnings.append(f"{len(distinct_vals)} categories exceeds pie chart limit ({MAX_CATEGORIES}); switched to bar")
            else:
                warnings.append(f"{len(distinct_vals)} categories exceeds display limit ({MAX_CATEGORIES}); long tail bucketed into 'Other'")

    # --- Guardrail: empty results never get charted ---
    if row_count == 0:
        spec["chart_type"] = "table"
        warnings.append("Empty result set; showing table")

    # --- Guardrail: no usable axes -> table ---
    if spec["chart_type"] not in ("kpi", "table") and not spec.get("x_axis") and not spec.get("y_axis"):
        spec["chart_type"] = "table"
        warnings.append("Could not resolve valid chart axes; falling back to table")

    spec["warnings"] = warnings
    return spec


def prepare_chart_dataset(
    rows: List[Dict[str, Any]],
    spec: Dict[str, Any],
    max_categories: int = MAX_CATEGORIES,
) -> List[Dict[str, Any]]:
    """
    For bar/pie charts with more categories than max_categories, keep the top
    (max_categories - 1) rows by y_axis value and collapse the rest into a
    single 'Other' bucket, so the frontend doesn't need this logic itself.
    No-op for every other chart type, or when the category count is already fine.
    """
    if spec.get("chart_type") not in ("bar", "pie"):
        return rows

    x_col = spec.get("x_axis")
    y_col = spec.get("y_axis")
    if not x_col or not y_col or len(rows) <= max_categories:
        return rows

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    sorted_rows = sorted(rows, key=lambda r: _num(r.get(y_col)), reverse=True)
    top = sorted_rows[: max_categories - 1]
    rest = sorted_rows[max_categories - 1:]
    other_sum = sum(_num(r.get(y_col)) for r in rest)

    if other_sum:
        top = list(top) + [{x_col: "Other", y_col: other_sum}]

    return top


def _build_chart_payload(
    question: str,
    sql: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    ask_json_fn,
) -> Dict[str, Any]:
    """Wraps recommend_chart_spec + prepare_chart_dataset with a safe fallback to 'table'."""
    try:
        spec = recommend_chart_spec(question, sql, columns, rows, ask_json_fn=ask_json_fn)
        data = prepare_chart_dataset(rows, spec)
        return {"chart_spec": spec, "chart_data": data}
    except Exception as e:
        logger.warning(f"[poc8] chart spec generation failed, falling back to table: {e}")
        return {
            "chart_spec": {
                "chart_type": "table",
                "x_axis": None, "y_axis": None, "series": None,
                "aggregation": "none", "title": question or "Result",
                "warnings": [f"chart generation failed: {e}"],
            },
            "chart_data": rows,
        }


# ============================================================================
# MAIN QUESTION → SQL FLOW
# ============================================================================

def question_to_sql(
    db_file: str,
    ds: str,
    storage_dir: str,
    question: str,
    ask_json_fn=None,
    want_chart: bool = True,
) -> Dict[str, Any]:
    """
    Convert a natural language question into SQL using KB-aware retrieval,
    execute it, and (by default) attach a recommended chart spec.

    Returns {ok, sql, tables, join_paths, warnings, error, rows, columns,
             chart_spec, chart_data, ...}

    Set want_chart=False to skip chart recommendation entirely (e.g. for
    callers that only need tabular data and want to avoid the extra work).
    """
    if not ask_json_fn:
        from bedrock_client import ask_json
        ask_json_fn = ask_json

    warnings = []
    tables = []
    join_paths = []

    try:
        # 1. RETRIEVE: Vector semantic search via KB Manager
        logger.info(f"[poc8] Starting vector retrieval for: {question[:60]}...")
        retrieved, retrieval_warnings = retrieve_tables_vector(question, top_k=20)
        warnings.extend(retrieval_warnings)

        if not retrieved:
            logger.warning("[poc8] Vector retrieval returned no tables")
            return {
                "ok": False,
                "error": "No relevant tables found for your question. "
                         "Make sure the KB has been built, or rephrase the question.",
                "tables": [],
                "warnings": warnings,
            }

        tables = retrieved
        logger.info(f"[poc8] Retrieved {len(tables)} tables: {tables}")

        # 2. EXPAND: Load KB and join graph
        if not _KB_MANAGER:
            logger.warning("[poc8] KB Manager missing — cannot load catalog/joins")
            return {
                "ok": False,
                "error": "KB Manager not initialized",
                "tables": tables,
                "warnings": warnings,
            }

        catalog = _KB_MANAGER.load_catalog(force_refresh=False)
        joins_raw = _KB_MANAGER.load_joins(force_refresh=False)

        # Build join graph + edge confidence from kb_manager's data
        join_graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        edge_confidence: Dict[Tuple[str, str], float] = {}

        for src_table, targets in (joins_raw or {}).items():
            for tgt_table, join_col in targets:
                join_graph[src_table].append((tgt_table, join_col))
                # Default confidence for KB-loaded edges
                edge_confidence[(src_table, tgt_table)] = 0.85

        # Expand via joins with confidence filtering
        expanded = expand_via_join_graph(
            tables,
            join_graph,
            edge_confidence=edge_confidence,
            min_bridge_confidence=0.65,
        )

        final_tables = expanded.get("tables", [])
        join_paths = expanded.get("join_paths", [])
        unresolved = expanded.get("unresolved_components", [])

        for comp in unresolved:
            warnings.append(
                f"Tables {comp} could not be confidently joined. "
                f"No high-confidence join path exists."
            )

        logger.info(
            f"[poc8] Expanded to {len(final_tables)} tables "
            f"({len(expanded.get('bridges_added', []))} bridges added)"
        )

        if len(final_tables) == 1 and join_graph.get(final_tables[0]):
            related = [n for n, _ in join_graph.get(final_tables[0], [])][:2]
            for rel in related:
                if rel not in final_tables:
                    final_tables.append(rel)
            logger.info(f"[poc8] Added fallback related tables: {related}")
            expanded = expand_via_join_graph(
                final_tables,
                join_graph,
                edge_confidence=edge_confidence,
                min_bridge_confidence=0.65,
            )
            final_tables = expanded.get("tables", final_tables)
            join_paths = expanded.get("join_paths", join_paths)

        # 3. BUILD CONTEXT: Schema for Claude
        schema_context = _build_schema_context(catalog, final_tables)
        semantic_layer = _filter_semantic_layer_for_tables(
            _load_semantic_layer_context(storage_dir),
            final_tables,
        )

        # 4. LOAD EXAMPLES: Few-shot pairs
        examples = _KB_MANAGER.load_examples(min_quality=0.80, limit=3)

        # 5. PROMPT: Few-shot NL→SQL
        few_shot = "\n\n".join(
            f"Q: {ex['question']}\nA: {ex['sql']}"
            for ex in examples
        )

        system_prompt = """You are an expert Duck DB SQL engineer.
Convert natural language questions into DuckDB SQL.
- Only SELECT, WITH, or EXPLAIN queries
- No INSERT/UPDATE/DELETE
- No schema modifications
- Use DuckDB syntax only
- Use ONLY exact column names and table names shown in the provided schema/context
- Do not invent, normalize, or rename columns
- Use only exact schema columns or join the table that contains them.
- If a requested concept does not exist as an exact schema column, do not invent a new column name;
  instead, find the table that contains the closest matching exact column or omit that field.
- For ranking and aggregation, use the exact numeric measure column from schema and do not rename it.
- Treat the semantic layer as the schema contract:
  - never invent joins
  - use only explicit join predicates from the context
  - if a join looks weak or fuzzy, mark it as uncertain
  - follow temporal rules when effective/date/version columns exist
  - respect table grain and PK hints to avoid duplicates
- Do not use audit/control columns for joins or filters unless the question explicitly asks for them:
  - system_date
  - system_active
  - file_path
- If you use any aggregate function like SUM, COUNT, AVG, MIN, or MAX,
  every non-aggregated selected column must appear in GROUP BY.
- Prefer GROUP BY ALL in DuckDB when grouping all non-aggregated select columns.
- If DuckDB reports a binder error, use that exact error message to repair the SQL once.
- Return ONLY JSON with a SQL field, for example {"sql":"SELECT ..."}
- If you prefer, you may also return {"query":"SELECT ..."}"""

        user_prompt = f"""
Use the semantic layer below as the source of truth for joins, grain, temporal rules, and fuzzy-match warnings.

{_build_expected_complexity_hint(question)}

SEMANTIC LAYER:
{semantic_layer if semantic_layer else "(No semantic-layer document available)"}

SCHEMA:
{schema_context}

EXAMPLES:
{few_shot if few_shot else "(No examples yet)"}

QUESTION: {question}

Generate the SQL query."""

        logger.info(f"[poc8] Calling Claude with {len(final_tables)} tables in context...")
        if DEBUG_NLQ_PROMPTS:
            logger.info(
                "\n"
                + "=" * 72
                + "\n[poc8] NLQ SYSTEM PROMPT\n"
                + f"{system_prompt}\n"
                + "=" * 72
            )
            logger.info(
                "\n"
                + "=" * 72
                + "\n[poc8] NLQ USER PROMPT\n"
                + f"{user_prompt}\n"
                + "=" * 72
            )
        response = ask_json_fn(user_prompt, system_prompt,model="us.anthropic.claude-sonnet-4-6")
        if DEBUG_NLQ_PROMPTS:
            logger.info(
                "\n"
                + "=" * 72
                + "\n[poc8] NLQ RAW RESPONSE\n"
                + f"{json.dumps(response, indent=2, default=str)}\n"
                + "=" * 72
            )

        if not response:
            return {
                "ok": False,
                "error": "Claude returned empty response",
                "tables": final_tables,
                "warnings": warnings,
            }

        sql = (
            response.get("sql")
            or response.get("query")
            or response.get("answer")
            or ""
        ).strip()

        if not sql or not sql.upper().startswith(("SELECT", "WITH", "EXPLAIN")):
            return {
                "ok": False,
                "error": f"Invalid SQL generated: {sql[:100]}",
                "detail": f"Model response keys: {list(response.keys())}",
                "tables": final_tables,
                "warnings": warnings,
            }

        logger.info(f"[poc8] SQL generated: {sql[:80]}...")
        repaired_sql = _repair_sql_column_names(sql, catalog, final_tables)
        if repaired_sql != sql:
            logger.info(f"[poc8] SQL repaired: {repaired_sql[:80]}...")
            sql = repaired_sql

        # 6. EXECUTE
        try:
            conn = duckdb.connect(db_file, read_only=True)
            result_rows = conn.execute(sql).fetchall()
            result_cols = [d[0] for d in conn.description] if conn.description else []
            conn.close()

            # Convert to dicts
            results = [dict(zip(result_cols, row)) for row in result_rows]

            # 7. CHART SPEC — additive, never blocks the SQL result on failure
            chart_payload = _build_chart_payload(question, sql, result_cols, results, ask_json_fn) if want_chart else {}

            return {
                "ok": True,
                "sql": sql,
                "tables": final_tables,
                "join_paths": join_paths,
                "rows": results,
                "row_count": len(results),
                "columns": result_cols,
                "warnings": warnings,
                "retrieval_method": "vector_semantic_search",
                **chart_payload,
            }

        except Exception as exec_err:
            logger.error(f"[poc8] SQL execution failed: {exec_err}")
            try:
                repaired_sql = _repair_sql_with_error(
                    question,
                    sql,
                    str(exec_err),
                    catalog,
                    final_tables,
                    ask_json_fn,
                )
                repaired_sql = _repair_sql_column_names(repaired_sql, catalog, final_tables)
                if repaired_sql and repaired_sql != sql:
                    logger.info(f"[poc8] retrying with repaired SQL: {repaired_sql[:120]}...")
                    conn = duckdb.connect(db_file, read_only=True)
                    result_rows = conn.execute(repaired_sql).fetchall()
                    result_cols = [d[0] for d in conn.description] if conn.description else []
                    conn.close()
                    results = [dict(zip(result_cols, row)) for row in result_rows]

                    chart_payload = _build_chart_payload(question, repaired_sql, result_cols, results, ask_json_fn) if want_chart else {}

                    return {
                        "ok": True,
                        "sql": repaired_sql,
                        "tables": final_tables,
                        "join_paths": join_paths,
                        "rows": results,
                        "row_count": len(results),
                        "columns": result_cols,
                        "warnings": warnings + [f"SQL repaired after error: {str(exec_err)}"],
                        "retrieval_method": "vector_semantic_search",
                        **chart_payload,
                    }
            except Exception as repair_err:
                logger.warning(f"[poc8] SQL repair retry failed: {repair_err}")
            return {
                "ok": False,
                "sql": sql,
                "error": f"SQL execution failed: {str(exec_err)}",
                "detail": str(exec_err),
                "tables": final_tables,
                "join_paths": join_paths,
                "warnings": warnings,
            }

    except Exception as e:
        logger.error(f"[poc8] question_to_sql failed: {e}", exc_info=True)
        return {
            "ok": False,
            "error": f"Query generation failed: {str(e)}",
            "tables": tables,
            "warnings": warnings,
        }
