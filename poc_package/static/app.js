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

  const d = data.duckdb;
  if (d && d.success) {
    $("duckdbBanner").innerHTML = `
      <div class="banner ok">
        ✓ Ingested into DuckDB — table <code>${d.table}</code> · ${d.row_count} rows · ${d.columns.length} columns
      </div>`;
  } else {
    $("duckdbBanner").innerHTML = `
      <div class="banner error">✗ DuckDB ingestion failed${d && d.error ? ": " + d.error : ""}</div>`;
  }

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
═══════════════════════════════════════════════════════ */
$("runSchemaBtn").addEventListener("click", async () => {
  show("schemaLoader"); hide("schemaResult");
  try {
    const data = await api("/api/poc1/run", { method: "POST", body: "{}" });
    renderSchema(data);
    markDone("schema");
  } catch (e) { alert(e.message); }
  hide("schemaLoader");
});

function renderSchema(d) {
  const el = $("schemaResult");
  el.classList.remove("hidden");

  let html = `
    <div class="summary-strip">
      <div class="kpi"><div class="v">${d.total_columns}</div><div class="l">columns</div></div>
      <div class="kpi"><div class="v">${d.total_rows}</div><div class="l">rows</div></div>
      <div class="kpi teal"><div class="v">${d.key_columns.length}</div><div class="l">key columns</div></div>
      <div class="kpi amber"><div class="v">${d.quality_concerns.length}</div><div class="l">concerns</div></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Column</th><th>Type</th><th>Nulls</th>
          <th>Validation Rule</th><th>Sample Values</th>
        </tr></thead>
        <tbody>`;

  d.schema.forEach((col) => {
    const nullBadge = col.null_count > 0
      ? `<span class="badge ${col.null_count > 10 ? 'high' : 'medium'}">${col.null_count}</span>`
      : `<span class="badge none">0</span>`;
    html += `<tr>
      <td><strong>${col.column}</strong><br><span style="font-size:11px;color:#94A3B8">${col.business_meaning}</span></td>
      <td><code>${col.data_type}</code></td>
      <td>${nullBadge}</td>
      <td style="font-size:12px">${col.validation_rule}</td>
      <td style="font-size:12px">${(col.sample_values || []).join(", ")}</td>
    </tr>`;
  });

  html += `</tbody></table></div>`;

  if (d.quality_concerns.length) {
    html += `<h3>Quality Concerns</h3><div class="card-list">`;
    d.quality_concerns.forEach((c, i) => {
      html += `<div class="ev-card"><div class="obs">${i+1}. ${c}</div></div>`;
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
    const rows = d.recommended_indexes.map((idx) => {
      const col    = typeof idx === "string" ? idx         : (idx.column     || idx);
      const type   = typeof idx === "string" ? "B-Tree"   : (idx.index_type || "B-Tree");
      const reason = typeof idx === "string" ? "—"        : (idx.reason     || "—");
      const s = typeStyle[type] || { bg: "#F8FAFC", color: "#475569" };
      return `<tr>
        <td><code style="font-size:13px;font-weight:600">${col}</code></td>
        <td><span class="badge" style="background:${s.bg};color:${s.color};padding:3px 10px;border-radius:6px">${type}</span></td>
        <td style="font-size:12px;color:#475569">${reason}</td>
      </tr>`;
    }).join("");
    html += `
      <h3>Recommended Indexes</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Column</th><th>Index Type</th><th>Reason</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  el.innerHTML = html;
}

/* ═══════════════════════════════════════════════════════
   TAB 3 — SODA YAML (POC 7)
═══════════════════════════════════════════════════════ */
$("runSodaBtn").addEventListener("click", async () => {
  show("sodaLoader"); hide("sodaResult");
  try {
    const data = await api("/api/poc7/run", { method: "POST", body: "{}" });
    renderSoda(data);
    markDone("soda");
  } catch (e) { alert(e.message); }
  hide("sodaLoader");
});

function renderSoda(d) {
  const el = $("sodaResult");
  el.classList.remove("hidden");

  const escaped = d.yaml
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

  let html = `
    <div class="banner ok">✓ SODA YAML Generated · ${d.check_count} checks</div>
    <div class="summary-strip">
      <div class="kpi teal"><div class="v">${d.check_count}</div><div class="l">checks generated</div></div>
    </div>
    <h3>Generated YAML <button class="btn sm" onclick="copyYaml()">Copy</button></h3>
    <div class="yaml-block" id="yamlBlock">${escaped}</div>
    <div class="actions">
      <button class="btn primary" onclick="downloadYaml()">⬇ Download manufacturing_checks.yaml</button>
    </div>`;

  el.innerHTML = html;
  window._lastYaml = d.yaml;
}

function copyYaml() {
  navigator.clipboard.writeText(window._lastYaml || "");
  alert("Copied to clipboard!");
}
function downloadYaml() {
  const blob = new Blob([window._lastYaml || ""], { type: "text/yaml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "manufacturing_checks.yaml";
  a.click();
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