const API_BASE = localStorage.getItem("api_base_url") || "http://localhost:8000";
let editingBindingId = null;
let editingSourceId = null;
let editingDestinationId = null;

async function api(path, options = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function parseJsonOrEmpty(value) {
  const trimmed = (value || "").trim();
  if (!trimmed) {
    return {};
  }
  return JSON.parse(trimmed);
}

function setFlash(message, kind = "success") {
  const flash = document.getElementById("flash");
  flash.className = `alert alert-${kind}`;
  flash.textContent = message;
  flash.classList.remove("d-none");
  setTimeout(() => flash.classList.add("d-none"), 2500);
}

function renderValidationCard(container, title, result) {
  const card = document.createElement("div");
  card.className = "card border-0 shadow-sm mt-3";
  card.innerHTML = `
    <div class="card-body">
      <div class="d-flex justify-content-between align-items-start gap-2">
        <div>
          <h4 class="h6 mb-1">${title}</h4>
          <div class="text-muted small">${result?.message || "No output"}</div>
        </div>
        <span class="badge text-bg-${result?.ok ? "success" : "danger"}">${result?.ok ? "ok" : "error"}</span>
      </div>
      ${result?.details ? `<pre class="small mt-3 mb-0">${JSON.stringify(result.details, null, 2)}</pre>` : ""}
    </div>
  `;
  container.appendChild(card);
}

function badgeForStatus(status) {
  const map = {
    queued: "secondary",
    running: "info",
    success: "success",
    failed: "danger",
    cancelled: "warning",
  };
  const tone = map[String(status || "").toLowerCase()] || "secondary";
  return `<span class="badge text-bg-${tone}">${status}</span>`;
}

function getBindingPayloadFromForm() {
  const f = new FormData(document.getElementById("binding-form"));
  return {
    source_id: Number(f.get("source_id")),
    destination_id: Number(f.get("destination_id")),
    schedule_cron: f.get("schedule_cron"),
    policy: parseJsonOrEmpty(f.get("policy")),
    is_active: true,
  };
}

function getS3SettingsFromForm() {
  const f = new FormData(document.getElementById("source-form"));
  const settings = {};

  const bucket = String(f.get("s3_bucket") || "").trim();
  if (bucket) {
    settings.bucket = bucket;
  }

  const prefix = String(f.get("s3_prefix") || "").trim();
  if (prefix) {
    settings.prefix = prefix;
  }

  const region = String(f.get("s3_region") || "").trim();
  if (region) {
    settings.region = region;
  }

  const endpoint = String(f.get("s3_endpoint") || "").trim();
  if (endpoint) {
    settings.endpoint = endpoint;
  }

  const secretRef = String(f.get("s3_secret_ref") || "").trim();
  if (secretRef) {
    settings.secret_ref = secretRef;
  }

  const encryptionMode = String(f.get("s3_encryption_mode") || "").trim();
  if (encryptionMode) {
    const encryption = { mode: encryptionMode };
    const kmsKeyId = String(f.get("s3_kms_key_id") || "").trim();
    if (kmsKeyId) {
      encryption.kms_key_id = kmsKeyId;
    }
    const kmsKeyArn = String(f.get("s3_kms_key_arn") || "").trim();
    if (kmsKeyArn) {
      encryption.kms_key_arn = kmsKeyArn;
    }
    const customerKeyRef = String(f.get("s3_customer_key_ref") || "").trim();
    if (customerKeyRef) {
      encryption.customer_key_ref = customerKeyRef;
    }
    settings.encryption = encryption;
  }

  return settings;
}

function getDatabaseSettingsFromForm() {
  const f = new FormData(document.getElementById("source-form"));
  const settings = {};
  const host = String(f.get("db_host") || "").trim();
  if (host) settings.host = host;
  const port = String(f.get("db_port") || "").trim();
  if (port) settings.port = Number(port);
  const database = String(f.get("db_database") || "").trim();
  if (database) settings.database = database;
  const username = String(f.get("db_username") || "").trim();
  if (username) settings.username = username;
  const password = String(f.get("db_password") || "").trim();
  if (password) settings.password = password;
  const secretRef = String(f.get("db_secret_ref") || "").trim();
  if (secretRef) settings.secret_ref = secretRef;
  return settings;
}

function getSourcePayloadFromForm() {
  const f = new FormData(document.getElementById("source-form"));
  const sourceType = String(f.get("source_type") || "").trim();
  let settings = {};
  if (sourceType === "s3") {
    settings = getS3SettingsFromForm();
  } else if (sourceType === "mysql" || sourceType === "postgresql") {
    settings = getDatabaseSettingsFromForm();
    settings.engine = sourceType === "postgresql" ? "postgres" : "mysql";
  }
  return {
    name: f.get("name"),
    source_type: sourceType,
    settings,
    is_active: String(f.get("is_active") || "true").toLowerCase() !== "false",
  };
}

function getDestinationPayloadFromForm() {
  const f = new FormData(document.getElementById("destination-form"));
  const payload = {
    name: f.get("name"),
    provider: f.get("provider"),
    endpoint: f.get("endpoint"),
    bucket: f.get("bucket"),
    region: f.get("region"),
    secret_ref: f.get("secret_ref"),
    is_active: String(f.get("is_active") || "true").toLowerCase() !== "false",
  };

  const encryptionMode = String(f.get("destination_encryption_mode") || "").trim();
  if (encryptionMode) {
    const encryption = { mode: encryptionMode };
    const kmsKeyId = String(f.get("destination_kms_key_id") || "").trim();
    if (kmsKeyId) {
      encryption.kms_key_id = kmsKeyId;
    }
    const kmsKeyArn = String(f.get("destination_kms_key_arn") || "").trim();
    if (kmsKeyArn) {
      encryption.kms_key_arn = kmsKeyArn;
    }
    const customerKeyRef = String(f.get("destination_customer_key_ref") || "").trim();
    if (customerKeyRef) {
      encryption.customer_key_ref = customerKeyRef;
    }
    payload.encryption = encryption;
  }

  return payload;
}

function resetBindingEditState() {
  editingBindingId = null;
  const form = document.getElementById("binding-form");
  form.reset();
  document.getElementById("binding-submit-btn").textContent = "Create Binding";
  document.getElementById("binding-cancel-edit-btn").classList.add("d-none");
}

function toggleSourceSettingsVisibility() {
  const form = document.getElementById("source-form");
  const sourceType = String(form.querySelector("[name='source_type']").value || "").trim();
  const s3Panel = document.getElementById("s3-settings-panel");
  const databasePanel = document.getElementById("database-settings-panel");
  const showS3 = sourceType === "s3";
  const showDatabase = sourceType === "mysql" || sourceType === "postgresql";
  s3Panel.classList.toggle("d-none", !showS3);
  databasePanel.classList.toggle("d-none", !showDatabase);
}

function resetSourceEditState() {
  editingSourceId = null;
  const form = document.getElementById("source-form");
  form.reset();
  form.querySelector("[name='is_active']").value = "true";
  form.querySelector("[name='source_type']").value = "s3";
  form.querySelector("[name='s3_region']").value = "us-east-1";
  toggleSourceSettingsVisibility();
  document.getElementById("source-submit-btn").textContent = "Save Source";
  document.getElementById("source-cancel-edit-btn").classList.add("d-none");
}

function startSourceEdit(source) {
  editingSourceId = source.id;
  const form = document.getElementById("source-form");
  const settings = source.settings || {};
  const encryption = settings.encryption || settings.sse || {};
  form.querySelector("[name='name']").value = source.name;
  form.querySelector("[name='source_type']").value = source.source_type;
  form.querySelector("[name='s3_bucket']").value = settings.bucket || "";
  form.querySelector("[name='s3_prefix']").value = settings.prefix || "";
  form.querySelector("[name='s3_region']").value = settings.region || "us-east-1";
  form.querySelector("[name='s3_endpoint']").value = settings.endpoint || "";
  form.querySelector("[name='s3_secret_ref']").value = settings.secret_ref || "";
  form.querySelector("[name='s3_encryption_mode']").value = encryption.mode || "";
  form.querySelector("[name='s3_kms_key_id']").value = encryption.kms_key_id || "";
  form.querySelector("[name='s3_kms_key_arn']").value = encryption.kms_key_arn || "";
  form.querySelector("[name='s3_customer_key_ref']").value = encryption.customer_key_ref || "";
  form.querySelector("[name='is_active']").value = String(Boolean(source.is_active));
  toggleSourceSettingsVisibility();
  document.getElementById("source-submit-btn").textContent = "Save Source";
  document.getElementById("source-cancel-edit-btn").classList.remove("d-none");
}

function resetDestinationEditState() {
  editingDestinationId = null;
  const form = document.getElementById("destination-form");
  form.reset();
  form.querySelector("[name='provider']").value = "s3-compatible";
  form.querySelector("[name='region']").value = "us-east-1";
  form.querySelector("[name='is_active']").value = "true";
  document.getElementById("destination-submit-btn").textContent = "Save Destination";
  document.getElementById("destination-cancel-edit-btn").classList.add("d-none");
}

function startDestinationEdit(destination) {
  editingDestinationId = destination.id;
  const form = document.getElementById("destination-form");
  form.querySelector("[name='name']").value = destination.name;
  form.querySelector("[name='provider']").value = destination.provider;
  form.querySelector("[name='endpoint']").value = destination.endpoint;
  form.querySelector("[name='bucket']").value = destination.bucket;
  form.querySelector("[name='region']").value = destination.region;
  form.querySelector("[name='secret_ref']").value = destination.secret_ref;
  form.querySelector("[name='is_active']").value = String(Boolean(destination.is_active));
  document.getElementById("destination-submit-btn").textContent = "Save Destination";
  document.getElementById("destination-cancel-edit-btn").classList.remove("d-none");
}

function startBindingEdit(binding) {
  editingBindingId = binding.id;
  const form = document.getElementById("binding-form");
  form.querySelector("[name='source_id']").value = String(binding.source_id);
  form.querySelector("[name='destination_id']").value = String(binding.destination_id);
  form.querySelector("[name='schedule_cron']").value = binding.schedule_cron || "0 2 * * *";
  form.querySelector("[name='policy']").value = JSON.stringify(binding.policy || {}, null, 2);
  document.getElementById("binding-submit-btn").textContent = "Save Binding";
  document.getElementById("binding-cancel-edit-btn").classList.remove("d-none");
}

function showMainView(viewId) {
  document.querySelectorAll(".app-view").forEach((el) => {
    el.classList.toggle("d-none", el.id !== viewId);
  });

  document.querySelectorAll("#main-nav-list .nav-link").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === viewId);
  });
}

function showConfigView(panelId) {
  document.querySelectorAll(".config-panel").forEach((el) => {
    el.classList.toggle("d-none", el.id !== panelId);
  });

  document.querySelectorAll("#config-tab-list .nav-link").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.configView === panelId);
  });
}

function renderSourcesTable(sources) {
  const body = document.getElementById("sources-body");
  body.innerHTML = "";
  for (const s of sources) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.id}</td>
      <td>${s.name}</td>
      <td>${s.source_type}</td>
      <td>${s.is_active ? "yes" : "no"}</td>
      <td><button class="btn btn-sm btn-outline-secondary edit-source-row" data-source-id="${s.id}" type="button">Edit</button></td>
      <td><button class="btn btn-sm btn-outline-danger delete-source-row" data-source-id="${s.id}" type="button">Delete</button></td>
      <td><button class="btn btn-sm btn-outline-secondary test-source-row" data-source-id="${s.id}" type="button">Test</button></td>
    `;
    body.appendChild(tr);
  }

  body.querySelectorAll(".test-source-row").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const result = await api(`/validate/source/${Number(btn.dataset.sourceId)}`);
        setFlash(result.message || "Source validated");
      } catch (err) {
        setFlash(`Source test failed: ${err.message}`, "danger");
      }
    });
  });

  body.querySelectorAll(".edit-source-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.sourceId);
      const source = sources.find((item) => item.id === id);
      if (!source) {
        return;
      }
      startSourceEdit(source);
    });
  });

  body.querySelectorAll(".delete-source-row").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.sourceId);
      if (!window.confirm(`Delete source ${id}?`)) {
        return;
      }
      try {
        await api(`/sources/${id}`, { method: "DELETE" });
        if (editingSourceId === id) {
          resetSourceEditState();
        }
        await refreshSelectors();
        await refreshBindings();
        await refreshTopology();
        setFlash(`Source ${id} deleted`);
      } catch (err) {
        setFlash(`Source delete failed: ${err.message}`, "danger");
      }
    });
  });
}

function renderDestinationsTable(destinations) {
  const body = document.getElementById("destinations-body");
  body.innerHTML = "";
  for (const d of destinations) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${d.id}</td>
      <td>${d.name}</td>
      <td>${d.provider}</td>
      <td>${d.bucket}</td>
      <td><button class="btn btn-sm btn-outline-secondary edit-destination-row" data-destination-id="${d.id}" type="button">Edit</button></td>
      <td><button class="btn btn-sm btn-outline-danger delete-destination-row" data-destination-id="${d.id}" type="button">Delete</button></td>
      <td><button class="btn btn-sm btn-outline-secondary test-destination-row" data-destination-id="${d.id}" type="button">Test</button></td>
    `;
    body.appendChild(tr);
  }

  body.querySelectorAll(".test-destination-row").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const result = await api(`/validate/destination/${Number(btn.dataset.destinationId)}`);
        setFlash(result.message || "Destination validated");
        const container = document.getElementById("validation-results");
        container.innerHTML = "";
        renderValidationCard(container, "Destination Validation", result);
      } catch (err) {
        setFlash(`Destination test failed: ${err.message}`, "danger");
      }
    });
  });

  body.querySelectorAll(".edit-destination-row").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.destinationId);
      const destination = destinations.find((item) => item.id === id);
      if (!destination) {
        return;
      }
      startDestinationEdit(destination);
    });
  });

  body.querySelectorAll(".delete-destination-row").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.destinationId);
      if (!window.confirm(`Delete destination ${id}?`)) {
        return;
      }
      try {
        await api(`/destinations/${id}`, { method: "DELETE" });
        if (editingDestinationId === id) {
          resetDestinationEditState();
        }
        await refreshSelectors();
        await refreshBindings();
        await refreshTopology();
        setFlash(`Destination ${id} deleted`);
      } catch (err) {
        setFlash(`Destination delete failed: ${err.message}`, "danger");
      }
    });
  });
}

function renderBindingsTable(bindings) {
  const body = document.getElementById("bindings-body");
  body.innerHTML = "";
  for (const b of bindings) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${b.id}</td>
      <td>${b.source_id}</td>
      <td>${b.destination_id}</td>
      <td><code>${b.schedule_cron}</code></td>
      <td><button class="btn btn-sm btn-outline-primary trigger-run" data-binding-id="${b.id}" type="button">Trigger</button></td>
      <td><button class="btn btn-sm btn-outline-secondary edit-binding" data-binding-id="${b.id}" type="button">Edit</button></td>
      <td><button class="btn btn-sm btn-outline-danger delete-binding" data-binding-id="${b.id}" type="button">Delete</button></td>
      <td><button class="btn btn-sm btn-outline-secondary test-binding-row" data-binding-id="${b.id}" type="button">Test</button></td>
    `;
    body.appendChild(tr);
  }

  body.querySelectorAll(".trigger-run").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.bindingId);
      try {
        await api(`/runs/trigger/${id}`, { method: "POST" });
        setFlash(`Run queued for binding ${id}`);
        await refreshRuns();
      } catch (err) {
        setFlash(`Could not trigger run: ${err.message}`, "danger");
      }
    });
  });

  body.querySelectorAll(".test-binding-row").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const result = await api(`/validate/binding/${Number(btn.dataset.bindingId)}`);
        setFlash(result.message || "Binding validated");
        const container = document.getElementById("validation-results");
        container.innerHTML = "";
        renderValidationCard(container, "Binding Validation", result);
      } catch (err) {
        setFlash(`Binding test failed: ${err.message}`, "danger");
      }
    });
  });

  body.querySelectorAll(".edit-binding").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.bindingId);
      const binding = bindings.find((item) => item.id === id);
      if (!binding) {
        return;
      }
      startBindingEdit(binding);
    });
  });

  body.querySelectorAll(".delete-binding").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.bindingId);
      if (!window.confirm(`Delete binding ${id}?`)) {
        return;
      }
      try {
        await api(`/bindings/${id}`, { method: "DELETE" });
        if (editingBindingId === id) {
          resetBindingEditState();
        }
        await refreshBindings();
        await refreshTopology();
        setFlash(`Binding ${id} deleted`);
      } catch (err) {
        setFlash(`Binding delete failed: ${err.message}`, "danger");
      }
    });
  });
}

async function refreshSelectors() {
  const [sources, destinations] = await Promise.all([
    api("/sources"),
    api("/destinations"),
  ]);

  const sourceSel = document.getElementById("source-select");
  const destSel = document.getElementById("destination-select");

  sourceSel.innerHTML = "";
  destSel.innerHTML = "";

  for (const s of sources) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = `${s.id} - ${s.name} (${s.source_type})`;
    sourceSel.appendChild(opt);
  }

  for (const d of destinations) {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = `${d.id} - ${d.name} (${d.provider})`;
    destSel.appendChild(opt);
  }

  renderSourcesTable(sources);
  renderDestinationsTable(destinations);
}

async function refreshTopology() {
  const data = await api("/topology");
  const el = document.getElementById("topology");

  if (!data.nodes.length) {
    el.classList.remove("mermaid");
    el.removeAttribute("data-processed");
    el.innerHTML = `
      <div class="alert alert-secondary mb-0" role="status">
        <h3 class="h6 mb-1">No topology configured yet</h3>
        <p class="mb-0">Create at least one source, destination, and binding in Configuration Hub.</p>
      </div>
    `;
    return;
  }

  let graph = "graph LR\n";

  for (const node of data.nodes) {
    const id = node.id.replace(/-/g, "_");
    const label = `${node.label}\\n(${node.type})`;
    graph += `  ${id}[\"${label}\"]\n`;
  }

  for (const edge of data.edges) {
    const from = edge.from.replace(/-/g, "_");
    const to = edge.to.replace(/-/g, "_");
    graph += `  ${from} -->|${edge.schedule}| ${to}\n`;
  }

  el.classList.add("mermaid");
  el.removeAttribute("data-processed");
  el.textContent = graph;

  if (window.mermaid?.initialize) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "default",
    });
    await window.mermaid.run({ nodes: [el] });
  }
}

async function refreshBindings() {
  const bindings = await api("/bindings");
  renderBindingsTable(bindings);
}

async function refreshRuns() {
  const runs = await api("/runs");
  const body = document.getElementById("runs-body");
  body.innerHTML = "";
  const selectAll = document.getElementById("select-all-runs");
  selectAll.checked = false;

  for (const run of runs) {
    const cancellable = ["queued", "running"].includes(String(run.status).toLowerCase());
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input class="run-selector" type="checkbox" data-run-id="${run.id}" ${cancellable ? "" : "disabled"} /></td>
      <td><a href="#" class="text-decoration-none" data-run-id="${run.id}">${run.id}</a></td>
      <td>${run.binding_id}</td>
      <td>${badgeForStatus(run.status)}</td>
      <td>${run.started_at || ""}</td>
      <td>${run.finished_at || ""}</td>
      <td>${run.bytes_transferred}</td>
      <td>${run.message || ""}</td>
      <td><button class="btn btn-sm btn-outline-danger cancel-run" data-run-id="${run.id}" type="button" ${cancellable ? "" : "disabled"}>Cancel</button></td>
    `;
    body.appendChild(tr);
  }

  body.querySelectorAll("a[data-run-id]").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      const runId = link.dataset.runId;
      try {
        const detail = await api(`/runs/${runId}`);
        const panel = document.getElementById("run-detail-panel");
        panel.innerHTML = "";
        renderValidationCard(panel, "Run Detail", { ok: true, message: detail.message || "Run inspected", details: detail });
      } catch (err) {
        setFlash(`Could not load run detail: ${err.message}`, "danger");
      }
    });
  });

  body.querySelectorAll(".cancel-run").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const runId = Number(btn.dataset.runId);
      try {
        await api(`/runs/${runId}/cancel`, { method: "POST" });
        setFlash(`Cancel requested for run ${runId}`);
        await refreshRuns();
      } catch (err) {
        setFlash(`Run cancel failed: ${err.message}`, "danger");
      }
    });
  });
}

async function boot() {
  document.querySelectorAll("#main-nav-list .nav-link").forEach((btn) => {
    btn.addEventListener("click", async () => {
      showMainView(btn.dataset.view);
      if (btn.dataset.view === "topology-view") {
        await refreshTopology();
      }
      if (btn.dataset.view === "operations-view") {
        await refreshRuns();
      }
    });
  });

  document.querySelectorAll("#config-tab-list .nav-link").forEach((btn) => {
    btn.addEventListener("click", () => {
      showConfigView(btn.dataset.configView);
    });
  });

  document.getElementById("test-source-btn").addEventListener("click", async () => {
    try {
      const payload = getSourcePayloadFromForm();
      payload.is_active = true;
      const result = await api("/validate/source", { method: "POST", body: JSON.stringify(payload) });
      setFlash(result.message || "Source validated");
      const container = document.getElementById("validation-results");
      container.innerHTML = "";
      renderValidationCard(container, "Source Validation", result);
    } catch (err) {
      setFlash(`Source test failed: ${err.message}`, "danger");
    }
  });

  document.getElementById("test-destination-btn").addEventListener("click", async () => {
    try {
      const f = new FormData(document.getElementById("destination-form"));
      const payload = Object.fromEntries(f.entries());
      const result = await api("/validate/destination", { method: "POST", body: JSON.stringify(payload) });
      setFlash(result.message || "Destination validated");
      const container = document.getElementById("validation-results");
      container.innerHTML = "";
      renderValidationCard(container, "Destination Validation", result);
    } catch (err) {
      setFlash(`Destination test failed: ${err.message}`, "danger");
    }
  });

  document.getElementById("test-binding-btn").addEventListener("click", async () => {
    try {
      const f = new FormData(document.getElementById("binding-form"));
      const payload = {
        source_id: Number(f.get("source_id")),
        destination_id: Number(f.get("destination_id")),
        schedule_cron: f.get("schedule_cron"),
        policy: parseJsonOrEmpty(f.get("policy")),
        is_active: true,
      };
      const result = await api("/validate/binding", { method: "POST", body: JSON.stringify(payload) });
      setFlash(result.message || "Binding validated");
      const container = document.getElementById("validation-results");
      container.innerHTML = "";
      renderValidationCard(container, "Binding Validation", result);
    } catch (err) {
      setFlash(`Binding test failed: ${err.message}`, "danger");
    }
  });

  const destinationForm = document.getElementById("destination-form");
  destinationForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const payload = getDestinationPayloadFromForm();
      if (editingDestinationId) {
        await api(`/destinations/${editingDestinationId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        setFlash(`Destination ${editingDestinationId} updated`);
      } else {
        await api("/destinations", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        setFlash("Destination saved");
      }
      resetDestinationEditState();
      await refreshSelectors();
      await refreshTopology();
    } catch (err) {
      setFlash(`Destination save failed: ${err.message}`, "danger");
    }
  });

  const sourceForm = document.getElementById("source-form");
  sourceForm.querySelector("[name='source_type']").addEventListener("change", toggleSourceSettingsVisibility);
  toggleSourceSettingsVisibility();
  sourceForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const payload = getSourcePayloadFromForm();
      if (editingSourceId) {
        await api(`/sources/${editingSourceId}`, { method: "PUT", body: JSON.stringify(payload) });
        setFlash(`Source ${editingSourceId} updated`);
      } else {
        await api("/sources", { method: "POST", body: JSON.stringify(payload) });
        setFlash("Source saved");
      }
      resetSourceEditState();
      await refreshSelectors();
      await refreshTopology();
    } catch (err) {
      setFlash(`Source save failed: ${err.message}`, "danger");
    }
  });

  const bindingForm = document.getElementById("binding-form");
  bindingForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const payload = getBindingPayloadFromForm();
      if (editingBindingId) {
        await api(`/bindings/${editingBindingId}`, { method: "PUT", body: JSON.stringify(payload) });
        setFlash(`Binding ${editingBindingId} updated`);
      } else {
        await api("/bindings", { method: "POST", body: JSON.stringify(payload) });
        setFlash("Binding created");
      }
      resetBindingEditState();
      await refreshBindings();
      await refreshTopology();
    } catch (err) {
      setFlash(`Binding create failed: ${err.message}`, "danger");
    }
  });

  document.getElementById("binding-cancel-edit-btn").addEventListener("click", () => {
    resetBindingEditState();
  });

  document.getElementById("source-cancel-edit-btn").addEventListener("click", () => {
    resetSourceEditState();
  });

  document.getElementById("destination-cancel-edit-btn").addEventListener("click", () => {
    resetDestinationEditState();
  });

  document.getElementById("refresh-runs").addEventListener("click", refreshRuns);

  document.getElementById("select-all-runs").addEventListener("change", (event) => {
    const checked = event.target.checked;
    document.querySelectorAll(".run-selector").forEach((box) => {
      if (!box.disabled) {
        box.checked = checked;
      }
    });
  });

  document.getElementById("cancel-selected-runs").addEventListener("click", async () => {
    const selected = Array.from(document.querySelectorAll(".run-selector:checked")).map((box) => Number(box.dataset.runId));
    if (!selected.length) {
      setFlash("Select at least one queued or running run", "danger");
      return;
    }
    try {
      const result = await api("/runs/cancel", {
        method: "POST",
        body: JSON.stringify({ run_ids: selected }),
      });
      const okCount = (result.cancelled || []).length;
      const failCount = (result.not_cancelled || []).length;
      setFlash(`Cancel processed: ${okCount} succeeded, ${failCount} skipped`);
      await refreshRuns();
    } catch (err) {
      setFlash(`Bulk cancel failed: ${err.message}`, "danger");
    }
  });

  await refreshSelectors();
  await refreshBindings();
  await refreshTopology();
  await refreshRuns();
}

boot().catch((err) => {
  console.error(err);
  alert(`Failed to initialize UI: ${err.message}`);
});
