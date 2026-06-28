"""
Manufacturing POC 3b — Schema Change Discovery
================================================
Answers : What changed since the last time this CSV was profiled?

This module only defines the prompt used by app.py's /api/poc3b/run
route. app.py finds the previous schema version for the active
dataset_id itself (scoped to outputs/<dataset_id>/poc1_*.json) and
calls ask_json() directly — no hardcoded snapshot paths here.
"""

SYSTEM = """You are a data pipeline engineer doing schema discovery.
You compare two schema snapshots to find structural changes over time.
Return valid JSON only. No markdown."""

PROMPT = """
Compare the CURRENT schema snapshot against the PREVIOUS snapshot
(from an earlier upload of this same data source) to detect any
column-level structural changes.

Rules:
- NEW column    : exists in current but not in previous
- DROPPED column: exists in previous but not in current
- RENAMED column: a column dropped from one position and a new column
                  appeared at the same position with a different name
                  (flag as possible rename, not confirmed)
- REORDERED     : all columns present but in different order
- TYPE CHANGED  : same column name, different data_type
- If no changes found, report change_detected = false

PREVIOUS SNAPSHOT (column list):
{previous_columns}

CURRENT SNAPSHOT (column list):
{current_columns}

Return EXACT JSON:
{{
  "change_detected": true,
  "summary": "one sentence describing what changed",
  "new_columns": ["col1"],
  "dropped_columns": ["col1"],
  "possible_renames": [
    {{"position": 0, "old_name": "old_col", "new_name": "new_col", "confidence": "high|medium|low"}}
  ],
  "type_changes": [
    {{"column": "col", "old_type": "string", "new_type": "integer"}}
  ],
  "reordered": false,
  "recommended_actions": ["action 1"]
}}
"""