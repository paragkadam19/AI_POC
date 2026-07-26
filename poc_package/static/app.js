/* app.js — Manufacturing Data Quality POC Console */

const $ = (id) => document.getElementById(id);

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab))
);
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name)
  );
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === "panel-" + name)
  );
}
function markDone(name) {
  const t = document.querySelector(`.tab[data-tab="${name}"]`);
  if (t) t.classList.add("done");
}

async function api(path, opts = {}) {
  const res  = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }
function fmt(v)   { return (v === null || v === undefined || v === "") ? '<span style="color:#94A3B8">∅</span>' : v; }
function asArray(v) {
  if (Array.isArray(v)) return v;
  if (v === null || v === undefined || v === "") return [];
  return [v];
}

(async () => {
  try {
    const s = await api("/api/status");
    const b = $("statusBadge");
    if (s.csv_ready) {
      b.textContent = "CSV Ready · " + s.csv_file;
      b.classList.add("ok");
    } else {
      b.textContent = "No CSV uploaded";
    }
  } catch { $("statusBadge").textContent = "backend offline"; }
})();

/* ═══════════════════════════════════════════════════════
   TAB 1 — CSV UPLOAD + DUCKDB INGEST
   Uploads the raw file as the request body (no multipart
   envelope), so it only touches disk once on the backend.
═══════════════════════════════════════════════════════ */
const dz = $("dropzone"), fi = $("fileInput");
dz.addEventListener("click", () => fi.click());
dz.addEventListener("dragover",  (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", ()  => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault(); dz.classList.remove("drag");
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});
fi.addEventListener("change", () => { if (fi.files.length) uploadFile(fi.files[0]); });

async function uploadFile(file) {
  try {
    const res = await fetch(`/api/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    renderUpload(data);
  } catch (e) {
    alert("Upload failed: " + e.message);
  }
}

function renderUpload(data) {
  $("uploadResult").classList.remove("hidden");
  $("uploadStats").innerHTML = `
    <div class="stat"><div class="v">${data.row_count}</div><div class="l">rows</div></div>
    <div class="stat"><div class="v">${data.columns.length}</div><div class="l">columns</div></div>`;

  $("duckdbBanner").innerHTML = "";

  const cols = data.columns;
  let html = "<thead><tr>" + cols.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
  html += data.sample.map((r) =>
    "<tr>" + cols.map((c) => `<td>${fmt(r[c])}</td>`).join("") + "</tr>"
  ).join("") + "</tbody>";
  $("previewTable").innerHTML = html;

  const b = $("statusBadge");
  b.textContent = `CSV Ready · ${data.row_count} rows`;
  b.classList.add("ok");
  markDone("upload");
}

/* ═══════════════════════════════════════════════════════
   TAB 2 — SCHEMA DISCOVERY (POC 1)
   Human-in-the-loop: AI result shown for review/edit.
   Nothing saved until user clicks Approve & Save.
   Only after approval is Tab 3 unlocked.
═══════════════════════════════════════════════════════ */

// Track whether Tab 2 has been approved in this session
window._schemaApproved = false;

$("runSchemaBtn").addEventListener("click", async () => {
  const btn = $("runSchemaBtn");
  if (btn.disabled) return;
  btn.disabled = true;
  show("schemaLoader"); hide("schemaResult");

  // Reset approval flag — running AI again means previous approval is stale
  window._schemaApproved = false;
  window._pendingSchema  = null;

  try {
    const data = await api("/api/poc1/run", { method: "POST", body: "{}" });
    renderSchema(data);
  } catch (e) { alert(e.message); }
  hide("schemaLoader");
  btn.disabled = false;
});

function renderSchema(d) {
    window._pendingSchema  = d;
    window._schemaApproved = false;   // reset — new AI result, not yet approved

    const el = $("schemaResult");
    el.classList.remove("hidden");

    let html = `
        <div class="banner info" id="schemaBanner">
            ⚠ Review the AI-generated schema below. Edit anything incorrect, then click
            <strong>Approve & Save</strong> — Tab 3 will be unlocked only after approval.
        </div>
        <div class="summary-strip">
            <div class="kpi"><div class="v">${d.total_columns}</div><div class="l">columns</div></div>
            <div class="kpi"><div class="v">${d.total_rows}</div><div class="l">rows</div></div>
            <div class="kpi teal"><div class="v">${(d.key_columns || []).length}</div><div class="l">key columns</div></div>
            <div class="kpi amber"><div class="v">${(d.quality_concerns || []).length}</div><div class="l">concerns</div></div>
            <div class="kpi red"><div class="v">${(d.critical_data || []).length}</div><div class="l">critical</div></div>
        </div>`;

    html += `
        <h3>Schema Columns <span style="font-size:12px;font-weight:400;color:#94A3B8">(edit type, description or rule inline, then Approve)</span></h3>
        <div class="table-wrap"><table>
            <thead><tr>
                <th>Column</th><th>Type</th><th>Column Description</th>
                <th>Validation Rule</th><th>Nulls</th>
                <th>Sample Values</th>
            </tr></thead><tbody>`;

    (d.schema || []).forEach((col, i) => {
        const nullBadge = col.null_count > 0
            ? `<span class="badge ${col.null_count > 10 ? 'high' : 'medium'}">${col.null_count} (${col.null_pct}%)</span>`
            : `<span class="badge none">0</span>`;

        const columnDescription = (col.business_meaning || col.description || col.column_description || col.businessDescription || "").trim();

        const sampleVals = (col.sample_values || col.categorical_values || [])
            .filter(v => v !== null && v !== undefined)
            .slice(0, 5)
            .map(v => `<code style="font-size:11px">${v}</code>`)
            .join(" ");

        const sampleFallback = col.value_distribution && Object.keys(col.value_distribution).length > 0
            ? Object.entries(col.value_distribution)
                .slice(0, 5)
                .map(([k, v]) => `<code style="font-size:11px">${k}</code> <span style="color:#64748B">(${v})</span>`)
                .join("<br>")
            : '<span style="color:#94A3B8;font-size:11px">No sample values available</span>';

        const distribution = col.value_distribution && Object.keys(col.value_distribution).length > 0
            ? Object.entries(col.value_distribution)
                .map(([k, v]) => `<span style="font-size:11px"><code>${k}</code>: ${v}</span>`)
                .join("<br>")
            : (col.categorical_values && col.categorical_values.length > 0
                ? col.categorical_values.map(v => `<code style="font-size:11px">${v}</code>`).join(" ")
                : '<span style="color:#94A3B8;font-size:11px">—</span>');

        html += `<tr>
            <td>
                <strong>${col.column}</strong>
                <br><span style="font-size:11px;color:#475569;display:block;max-width:220px;line-height:1.35;margin-top:4px">
                    ${columnDescription || '<span style="color:#94A3B8">No description</span>'}
                </span>
            </td>
            <td>
                <select id="type_${i}" onchange="updatePendingSchema(${i}, 'data_type', this.value)"
                    style="width:100%;font:inherit;font-size:10px;line-height:1.3;border:1px solid #E2E8F0;border-radius:4px;padding:6px 8px;min-width:120px;background:#fff">
                    ${["VARCHAR","BIGINT","INTEGER","DOUBLE","DATE","BOOLEAN","FLOAT","TIMESTAMP"]
                        .map(t => `<option ${col.data_type === t ? 'selected' : ''}>${t}</option>`)
                        .join('')}
                </select>
            </td>
            <td>
                <textarea id="meaning_${i}" rows="2" wrap="soft"
                    spellcheck="false"
                    placeholder="Add column description"
                    style="width:100%;font:inherit;font-size:11px;line-height:1.3;border:1px solid #E2E8F0;border-radius:4px;padding:9px 10px;min-width:240px;resize:vertical;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere"
                    onchange="updatePendingSchema(${i}, 'business_meaning', this.value)">${escapeHtml(columnDescription)}</textarea>
            </td>
            <td>
                <textarea id="rule_${i}" rows="2" wrap="soft"
                    style="width:100%;font:inherit;font-size:11px;line-height:1.3;border:1px solid #E2E8F0;border-radius:4px;padding:9px 10px;min-width:220px;resize:vertical;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere"
                    onchange="updatePendingSchema(${i}, 'validation_rule', this.value)">${escapeHtml(col.validation_rule || '')}</textarea>
            </td>
            <td>${nullBadge}</td>
            <td>${sampleVals || sampleFallback}</td>
        </tr>`;
    });

    html += `</tbody></table></div>`;

    if (d.key_columns && d.key_columns.length) {
        html += `<h3>Key Columns</h3>
            <p style="font-size:13px;margin-bottom:16px">
                ${d.key_columns.map(c => `<code style="background:#EFF6FF;color:#2563EB;padding:2px 8px;border-radius:4px;margin-right:4px">${c}</code>`).join("")}
            </p>`;
    }

    if (d.critical_data && d.critical_data.length) {
        html += `<h3>Critical Data Points</h3><div class="card-list">`;
        d.critical_data.forEach(cd => {
            const sevCls = cd.severity === "HIGH" ? "critical" : "high";
            html += `<div class="ev-card">
                <div class="obs">
                    <span class="badge ${sevCls}" style="margin-right:8px">${cd.severity}</span>
                    <strong>${cd.column}</strong> — ${cd.concern}
                </div>
                <div class="sig">${cd.impact}</div>
            </div>`;
        });
        html += `</div>`;
    }

    if (d.quality_concerns && d.quality_concerns.length) {
        html += `<h3>Quality Concerns</h3><div class="card-list">`;
        d.quality_concerns.forEach((c, i) => {
            html += `<div class="ev-card"><div class="obs">${i + 1}. ${c}</div></div>`;
        });
        html += `</div>`;
    }

    if (d.recommended_indexes && d.recommended_indexes.length) {
        const typeStyle = {
            "B-Tree":    { bg: "#EFF6FF", color: "#2563EB" },
            "Hash":      { bg: "#F0FDF4", color: "#16A34A" },
            "Composite": { bg: "#FFFBEB", color: "#D97706" },
            "Partial":   { bg: "#FEF2F2", color: "#DC2626" },
            "Full-Text": { bg: "#F0FDFA", color: "#0D9488" },
        };
        const rows = d.recommended_indexes.map(idx => {
            const col    = typeof idx === "string" ? idx      : (idx.column     || idx);
            const type   = typeof idx === "string" ? "B-Tree" : (idx.index_type || "B-Tree");
            const reason = typeof idx === "string" ? "—"      : (idx.reason     || "—");
            const s = typeStyle[type] || { bg: "#F8FAFC", color: "#475569" };
            return `<tr>
                <td><code style="font-size:13px;font-weight:600">${col}</code></td>
                <td><span class="badge" style="background:${s.bg};color:${s.color};padding:3px 10px;border-radius:6px">${type}</span></td>
                <td style="font-size:12px;color:#475569">${reason}</td>
            </tr>`;
        }).join("");
        html += `
            <h3>Recommended Indexes</h3>
            <div class="table-wrap"><table>
                <thead><tr><th>Column</th><th>Index Type</th><th>Reason</th></tr></thead>
                <tbody>${rows}</tbody>
            </table></div>`;
    }

    if (d.conditional_dependencies && d.conditional_dependencies.length) {
        html += `<h3>Conditional Dependencies</h3><div class="card-list">`;
        d.conditional_dependencies.forEach(dep => {
            const passed = (dep.validation || "").startsWith("PASSED");
            const failed = (dep.validation || "").startsWith("FAILED") || (dep.validation || "").startsWith("POTENTIAL");
            const cls    = passed ? "none" : failed ? "critical" : "medium";
            html += `<div class="ev-card">
                <div class="obs">
                    <span class="badge ${cls}" style="margin-right:8px">${passed ? "PASS" : failed ? "FAIL" : "WARN"}</span>
                    <code>${dep.controlling_column}</code> → <code>${dep.dependent_column}</code>
                </div>
                <div class="sig">${dep.rule}</div>
                <div style="font-size:11px;color:#94A3B8;margin-top:4px">${dep.validation}</div>
            </div>`;
        });
        html += `</div>`;
    }

    if (d.data_completeness_pct !== undefined) {
        const pct   = parseFloat(d.data_completeness_pct).toFixed(1);
        const color = pct >= 90 ? "var(--green)" : pct >= 70 ? "var(--amber)" : "var(--red)";
        html += `
            <h3>Data Completeness</h3>
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
                <div style="flex:1;height:10px;background:var(--gray2);border-radius:5px;overflow:hidden">
                    <div style="width:${pct}%;height:100%;background:${color};border-radius:5px"></div>
                </div>
                <span style="font-size:15px;font-weight:700;color:${color}">${pct}%</span>
            </div>`;
    }

    html += `
        <div class="actions">
            <button class="btn" onclick="rejectSchema()">✗ Re-run AI</button>
            <button class="btn primary" id="approveSchemaBtn" onclick="approveSchema()">✓ Approve & Save</button>
        </div>`;

    el.innerHTML = html;
}

function updatePendingSchema(index, field, value) {
    if (window._pendingSchema && window._pendingSchema.schema[index]) {
        window._pendingSchema.schema[index][field] = value;
    }
}

async function approveSchema() {
    if (!window._pendingSchema) return;
    const btn = $("approveSchemaBtn");
    if (btn) btn.disabled = true;
    try {
        const data = await api("/api/poc1/approve", {
            method: "POST",
            body: JSON.stringify(window._pendingSchema)
        });

        // Mark as approved — Tab 3 is now unlocked
        window._schemaApproved = true;

        // Update banner to success
        const banner = $("schemaBanner");
        if (banner) {
            banner.className = "banner ok";
            banner.innerHTML = `✓ Schema approved and saved · <code>${data.version_file}</code>
                ${data.contract_created ? " · Contract locked" : ""}
                — <strong>Tab 3 is now unlocked</strong>`;
        }
        if (btn) btn.textContent = "✓ Approved";
        markDone("schema");
    } catch (e) {
        alert("Save failed: " + e.message);
        if (btn) btn.disabled = false;
    }
}

function rejectSchema() {
    if (confirm("Re-run AI schema discovery? This will discard current results.")) {
        $("runSchemaBtn").click();
    }
}


/* ═══════════════════════════════════════════════════════
   TAB 3 — SODA YAML (POC 7)
   STRICT GATE: only runs if Tab 2 has been approved
   in this session (window._schemaApproved = true) OR
   if a previously-approved poc1_latest.json exists on disk
   (confirmed by the backend — returns error if not found).
═══════════════════════════════════════════════════════ */
$("runSodaBtn").addEventListener("click", async () => {
  // Frontend gate — if Tab 2 was run in this session but not approved, block immediately
  if (window._pendingSchema && !window._schemaApproved) {
    alert("Schema Discovery has been run but not approved yet.\n\nPlease go to Tab 2 and click 'Approve & Save' before generating the YAML.");
    switchTab("schema");
    return;
  }

  const btn = $("runSodaBtn");
  if (btn.disabled) return;
  btn.disabled = true;
  show("sodaLoader"); hide("sodaResult");

  try {
    // Send plain empty body — backend reads only from approved poc1_latest.json
    const data = await api("/api/poc7/run", { method: "POST", body: "{}" });
    renderSoda(data);
  } catch (e) {
    alert(e.message);
  }
  hide("sodaLoader");
  btn.disabled = false;
});

function renderSoda(d) {
  const el = $("sodaResult");
  el.classList.remove("hidden");

  window._originalYaml = d.yaml;

  let html = `
    <div class="banner ok">✓ Generated from approved Tab 2 schema</div>
    <div class="banner info">Review the YAML below. Edit directly if needed, then click Approve to save.</div>
    <div class="summary-strip">
      <div class="kpi teal"><div class="v">${d.check_count}</div><div class="l">checks generated</div></div>
    </div>
    <h3>Generated YAML
      <button class="btn sm" onclick="copyYaml()" style="margin-left:8px">Copy</button>
      <button class="btn sm" onclick="resetYaml()" style="margin-left:4px;color:#94A3B8">Reset to AI version</button>
    </h3>
    <textarea id="yamlEditor"
      style="width:100%;min-height:560px;font-family:'SF Mono',Consolas,monospace;font-size:13px;
             background:#1C2B3A;color:#A8D8A8;padding:18px;border-radius:8px;border:none;
             resize:vertical;line-height:1.6;margin-top:10px;outline:none;"
      spellcheck="false">${escapeHtml(d.yaml)}</textarea>
    <div class="actions">
      <button class="btn" onclick="rejectSoda()">✗ Re-generate</button>
      <button class="btn primary" id="approveSodaBtn" onclick="approveSoda()">✓ Approve & Save</button>
    </div>`;

  el.innerHTML = html;
}

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function copyYaml() {
  const editor = $("yamlEditor");
  if (editor) {
    navigator.clipboard.writeText(editor.value);
    alert("Copied to clipboard!");
  }
}

function resetYaml() {
  const editor = $("yamlEditor");
  if (editor && window._originalYaml) {
    if (confirm("Reset to the original AI-generated YAML? Your edits will be lost.")) {
      editor.value = window._originalYaml;
    }
  }
}

function downloadYaml() {
  const editor  = $("yamlEditor");
  const content = editor ? editor.value : (window._originalYaml || "");
  const blob    = new Blob([content], { type: "text/yaml" });
  const a       = document.createElement("a");
  a.href        = URL.createObjectURL(blob);
  a.download    = "DQ_checks.yaml";
  a.click();
}

async function approveSoda() {
  const yamlEditor = $("yamlEditor");
  if (!yamlEditor) return;

  const yamlContent = yamlEditor.value.trim();
  if (!yamlContent) {
    alert("YAML content is empty — nothing to save.");
    return;
  }

  const btn = $("approveSodaBtn");
  if (btn) btn.disabled = true;

  try {
    const data = await api("/api/poc7/approve", {
      method: "POST",
      body: JSON.stringify({ yaml: yamlContent })
    });

    // Update banners
    const banners = document.querySelectorAll("#sodaResult .banner.info");
    banners.forEach(b => b.remove());
    const okBanner = document.querySelector("#sodaResult .banner.ok");
    if (okBanner) {
      okBanner.innerHTML = `✓ YAML approved and saved · ${data.check_count} checks · <code>${data.version_file}</code>`;
    }

    // Make textarea read-only to signal approval
    yamlEditor.style.opacity  = "0.7";
    yamlEditor.readOnly       = true;

    // Replace action buttons
    const actions = document.querySelector("#sodaResult .actions");
    if (actions) {
      actions.innerHTML = `
        <button class="btn" onclick="rejectSoda()">✗ Re-generate</button>
        <button class="btn" onclick="downloadYaml()">⬇ Download DQ_checks.yaml</button>
        <span style="font-size:12px;color:var(--green);font-weight:600;align-self:center">✓ Approved</span>`;
    }
    markDone("soda");
  } catch (e) {
    alert("Save failed: " + e.message);
    if (btn) btn.disabled = false;
  }
}

function rejectSoda() {
  if (confirm("Re-generate YAML? This will discard your current edits.")) {
    $("runSodaBtn").click();
  }
}

/* ═══════════════════════════════════════════════════════
   TAB 7 — NL → SQL
═══════════════════════════════════════════════════════ */
const nlQuery = $("nlQuery");
const runQueryBtn = $("runQueryBtn");
const clearQueryBtn = $("clearQueryBtn");

if (clearQueryBtn && nlQuery) {
  clearQueryBtn.addEventListener("click", () => {
    nlQuery.value = "";
    $("queryResult").classList.add("hidden");
    $("queryResult").innerHTML = "";
  });
}

if (runQueryBtn) {
  runQueryBtn.addEventListener("click", async () => {
    const question = (nlQuery?.value || "").trim();
    if (!question) {
      alert("Please enter a natural-language question.");
      return;
    }

    show("queryLoader"); hide("queryResult");
    runQueryBtn.disabled = true;

    try {
      const data = await api("/api/poc8/query", {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      renderQuery(data);
    } catch (e) {
      $("queryResult").classList.remove("hidden");
      $("queryResult").innerHTML = `<div class="banner error">✗ ${e.message}</div>`;
    }

    hide("queryLoader");
    runQueryBtn.disabled = false;
  });
}

function renderQuery(d) {
  const el = $("queryResult");
  el.classList.remove("hidden");

  if (!d.ok) {
    const warnings = asArray(d.warnings).map(w => `<li>${escapeHtml(String(w))}</li>`).join("");
    el.innerHTML = `
      <div class="banner error">✗ Could not generate a safe query</div>
      <div class="card-list">
        <div class="ev-card"><div class="obs">Question</div><div class="sig">${escapeHtml(d.question || "")}</div></div>
        <div class="ev-card"><div class="obs">Warnings</div><div class="sig"><ul style="margin:0;padding-left:18px">${warnings || "<li>No SQL returned</li>"}</ul></div></div>
      </div>`;
    return;
  }

  const tables = (d.tables || []).map(t => `<code style="margin-right:6px">${t}</code>`).join("");
  const joins = (d.join_paths || []).map(j => `<li><code>${j.left}</code> ↔ <code>${j.right}</code> via <code>${j.join_column}</code></li>`).join("");
  const previewRows = (d.preview && d.preview.rows) || [];
  const previewCols = (d.preview && d.preview.columns) || [];

  let previewHtml = "<div style='color:#94A3B8;font-size:12px'>No preview rows returned.</div>";
  if (previewRows.length && previewCols.length) {
    previewHtml = `
      <div class="table-wrap"><table>
        <thead><tr>${previewCols.map(c => `<th>${c}</th>`).join("")}</tr></thead>
        <tbody>
          ${previewRows.map(row => `<tr>${previewCols.map(c => `<td>${fmt(row[c])}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table></div>`;
  }

  const warningsList = asArray(d.warnings);
  const warnings = warningsList.length
    ? `<div class="banner info">Warnings: ${warningsList.map((w) => escapeHtml(String(w))).join(" · ")}</div>`
    : "";

  el.innerHTML = `
    <div class="banner ok">✓ SQL generated and validated against DuckDB</div>
    ${warnings}
    <div class="summary-strip">
      <div class="kpi teal"><div class="v">${(d.tables || []).length}</div><div class="l">retrieved tables</div></div>
      <div class="kpi amber"><div class="v">${(d.bridges_added || []).length}</div><div class="l">bridge tables</div></div>
      <div class="kpi green"><div class="v">${previewRows.length}</div><div class="l">preview rows</div></div>
    </div>
    <div class="card-list">
      <div class="ev-card">
        <div class="obs">Question</div>
        <div class="sig">${escapeHtml(d.question || "")}</div>
      </div>
      <div class="ev-card">
        <div class="obs">Retrieved tables</div>
        <div class="sig">${tables || "<span style='color:#94A3B8'>None</span>"}</div>
      </div>
      <div class="ev-card">
        <div class="obs">Join paths</div>
        <div class="sig"><ul style="margin:0;padding-left:18px">${joins || "<li>None needed</li>"}</ul></div>
      </div>
    </div>
    <h3>Generated SQL</h3>
    <textarea readonly style="width:100%;min-height:180px;font-family:'SF Mono',Consolas,monospace;font-size:13px;
      background:#0F172A;color:#E2E8F0;padding:16px;border-radius:8px;border:none;resize:vertical;line-height:1.6">${escapeHtml(d.sql || "")}</textarea>
    <h3>Preview</h3>
    ${previewHtml}
  `;
}

/* ═══════════════════════════════════════════════════════
   TAB 4 — DATA QUALITY (POC 2)
   Now powered by soda_executor.py running real SQL checks
   against DuckDB — no AI call, no CSV sample over the wire.
   Response shape changed: { audit_passed, total_checks,
   passed_checks, failed_checks, checks: [...] }
═══════════════════════════════════════════════════════ */
$("runDqBtn").addEventListener("click", async () => {
  show("dqLoader"); hide("dqResult");
  try {
    const data = await api("/api/poc2/run", { method: "POST", body: "{}" });
    renderDq(data);
    markDone("dq");
  } catch (e) { alert(e.message); }
  hide("dqLoader");
});

function renderDq(d) {
  const el = $("dqResult");
  el.classList.remove("hidden");

  if (d.error) {
    el.innerHTML = `<div class="banner error">✗ ${d.error}</div>`;
    return;
  }

  const auditIcon = d.audit_passed ? "✓" : "✗";
  const auditCls  = d.audit_passed ? "ok" : "error";

  let html = `
    <div class="banner ${auditCls}">
      ${auditIcon} Data Quality Audit — ${d.audit_passed ? "PASSED" : "FAILED"}
      · ${d.failed_checks} of ${d.total_checks} checks failed
    </div>
    <div class="summary-strip">
      <div class="kpi"><div class="v">${d.total_checks}</div><div class="l">total checks</div></div>
      <div class="kpi green"><div class="v">${d.passed_checks}</div><div class="l">passed</div></div>
      <div class="kpi red"><div class="v">${d.failed_checks}</div><div class="l">failed</div></div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>Check ID</th><th>Name</th><th>Status</th>
        <th>Actual</th><th>Expected</th><th>Type</th>
      </tr></thead>
      <tbody>`;

  (d.checks || []).forEach((chk) => {
    const icon = chk.passed ? "✓" : "✗";
    const cls  = chk.passed ? "PASS" : "REJECT";
    html += `<tr>
      <td><strong>${chk.check_id}</strong></td>
      <td>${chk.check_name}</td>
      <td><span class="badge ${cls}">${icon} ${chk.passed ? "PASS" : "FAIL"}</span></td>
      <td>${fmt(chk.actual_value)}</td>
      <td><code>${chk.expected}</code></td>
      <td style="font-size:12px;color:#94A3B8">${chk.type}</td>
    </tr>`;
    if (!chk.passed && chk.error_message) {
      html += `<tr><td colspan="6" style="background:#FEF2F2;font-size:12px;padding:6px 12px">
        ${chk.error_message}
      </td></tr>`;
    }
  });

  html += `</tbody></table></div>`;
  el.innerHTML = html;
}

/* ═══════════════════════════════════════════════════════
   TAB 5 — SCHEMA VALIDATION (POC 3a)
═══════════════════════════════════════════════════════ */
$("runValidateBtn").addEventListener("click", async () => {
  show("validateLoader"); hide("validateResult");
  try {
    const data = await api("/api/poc3a/run", { method: "POST", body: "{}" });
    renderValidate(data);
    markDone("validate");
  } catch (e) { alert(e.message); }
  hide("validateLoader");
});

function renderValidate(d) {
  const el  = $("validateResult");
  el.classList.remove("hidden");
  const cls = d.validation_passed ? "ok" : (d.severity === "critical" ? "error" : "warn");

  let html = `
    <div class="banner ${cls}">${d.validation_passed ? "✓ VALIDATION PASSED" : "✗ VALIDATION FAILED"} · Severity: ${d.severity.toUpperCase()}</div>
    <div class="summary-strip">
      <div class="kpi ${d.can_pipeline_proceed ? 'green' : 'red'}">
        <div class="v">${d.can_pipeline_proceed ? "GO" : "HALT"}</div><div class="l">pipeline status</div>
      </div>
      <div class="kpi ${d.missing_columns.length ? 'red' : 'green'}">
        <div class="v">${d.missing_columns.length}</div><div class="l">missing</div>
      </div>
      <div class="kpi ${d.extra_columns.length ? 'amber' : 'green'}">
        <div class="v">${d.extra_columns.length}</div><div class="l">extra</div>
      </div>
      <div class="kpi ${d.type_mismatches.length ? 'amber' : 'green'}">
        <div class="v">${d.type_mismatches.length}</div><div class="l">type mismatches</div>
      </div>
    </div>
    <p style="font-size:14px;color:#475569;margin-bottom:16px">${d.summary}</p>`;

  if (d.missing_columns.length) {
    html += `<h3>✗ Missing Columns</h3>
      <div class="table-wrap"><table><thead><tr><th>Column</th><th>Impact</th></tr></thead><tbody>`;
    d.missing_columns.forEach((c) => {
      html += `<tr><td><strong>${c}</strong></td><td><span class="badge critical">CRITICAL</span></td></tr>`;
    });
    html += `</tbody></table></div>`;
  }
  if (d.extra_columns.length) {
    html += `<h3>+ Extra Columns</h3>
      <p style="font-size:13px;color:#475569">${d.extra_columns.join(", ")}</p>`;
  }
  if (d.type_mismatches.length) {
    html += `<h3>! Type Mismatches</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Column</th><th>Expected</th><th>Actual</th></tr></thead><tbody>`;
    d.type_mismatches.forEach((tm) => {
      html += `<tr><td><strong>${tm.column}</strong></td><td><code>${tm.expected_type}</code></td>
        <td><code style="color:#DC2626">${tm.actual_type}</code></td></tr>`;
    });
    html += `</tbody></table></div>`;
  }
  if (d.recommended_actions.length) {
    html += `<h3>Recommended Actions</h3><ol style="margin-left:20px;font-size:14px;color:#475569">`;
    d.recommended_actions.forEach((a) => { html += `<li style="margin-bottom:4px">${a}</li>`; });
    html += `</ol>`;
  }
  el.innerHTML = html;
}

/* ═══════════════════════════════════════════════════════
   TAB 6 — SCHEMA CHANGES (POC 3b)
═══════════════════════════════════════════════════════ */
$("runChangesBtn").addEventListener("click", async () => {
  show("changesLoader"); hide("changesResult");
  try {
    const data = await api("/api/poc3b/run", { method: "POST", body: "{}" });
    renderChanges(data);
    markDone("changes");
  } catch (e) { alert(e.message); }
  hide("changesLoader");
});

function renderChanges(d) {
  const el = $("changesResult");
  el.classList.remove("hidden");

  if (d.is_first_run) {
    el.innerHTML = `
      <div class="banner info">ℹ ${d.summary}</div>
      <p style="font-size:13px;color:#475569">
        Upload and re-profile a new version of this CSV later (Tab 1 → Tab 2)
        to see schema change detection in action.
      </p>`;
    return;
  }

  const cls = d.change_detected ? "warn" : "ok";
  let html = `
    <div class="banner ${cls}">${d.change_detected ? "✗ CHANGES DETECTED" : "✓ NO CHANGES"}</div>
    <p style="font-size:12px;color:#94A3B8;margin-bottom:8px">Compared against: ${d.compared_against || "previous snapshot"}</p>
    <p style="font-size:14px;color:#475569;margin-bottom:16px">${d.summary}</p>`;

  if (d.new_columns.length) {
    html += `<h3>+ New Columns Added</h3>
      <p style="font-size:13px">${d.new_columns.map(c => `<code>${c}</code>`).join(", ")}</p>`;
  }
  if (d.dropped_columns.length) {
    html += `<h3>− Columns Dropped</h3>
      <p style="font-size:13px">${d.dropped_columns.map(c => `<code style="color:#DC2626">${c}</code>`).join(", ")}</p>`;
  }
  if (d.possible_renames.length) {
    html += `<h3>~ Possible Renames</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Position</th><th>Old Name</th><th>New Name</th><th>Confidence</th></tr></thead><tbody>`;
    d.possible_renames.forEach((r) => {
      html += `<tr><td>${r.position}</td><td><code>${r.old_name}</code></td><td><code>${r.new_name}</code></td>
        <td><span class="badge ${r.confidence === 'high' ? 'PASS' : 'REVIEW'}">${r.confidence}</span></td></tr>`;
    });
    html += `</tbody></table></div>`;
  }
  if (d.type_changes && d.type_changes.length) {
    html += `<h3>~ Type Changes</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Column</th><th>Old Type</th><th>New Type</th></tr></thead><tbody>`;
    d.type_changes.forEach((tc) => {
      html += `<tr><td><strong>${tc.column}</strong></td><td><code>${tc.old_type}</code></td><td><code>${tc.new_type}</code></td></tr>`;
    });
    html += `</tbody></table></div>`;
  }
  if (d.reordered) html += `<div class="banner warn">↕ Column order has changed</div>`;
  if (d.recommended_actions.length) {
    html += `<h3>Recommended Actions</h3><ol style="margin-left:20px;font-size:14px;color:#475569">`;
    d.recommended_actions.forEach((a) => { html += `<li style="margin-bottom:4px">${a}</li>`; });
    html += `</ol>`;
  }
  el.innerHTML = html;
}
