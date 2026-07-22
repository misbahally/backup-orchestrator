const API_BASE = localStorage.getItem("api_base_url") || "http://localhost:8000";

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
  };
  const tone = map[String(status || "").toLowerCase()] || "secondary";
  return `<span class="badge text-bg-${tone}">${status}</span>`;
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

  for (const run of runs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><a href="#" class="text-decoration-none" data-run-id="${run.id}">${run.id}</a></td>
      <td>${run.binding_id}</td>
      <td>${badgeForStatus(run.status)}</td>
      <td>${run.started_at || ""}</td>
      <td>${run.finished_at || ""}</td>
      <td>${run.bytes_transferred}</td>
      <td>${run.message || ""}</td>
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
      const f = new FormData(document.getElementById("source-form"));
      const payload = {
        name: f.get("name"),
        source_type: f.get("source_type"),
        settings: parseJsonOrEmpty(f.get("settings")),
        is_active: true,
      };
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
      const f = new FormData(destinationForm);
      await api("/destinations", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(f.entries())),
      });
      destinationForm.reset();
      await refreshSelectors();
      await refreshTopology();
      setFlash("Destination saved");
    } catch (err) {
      setFlash(`Destination save failed: ${err.message}`, "danger");
    }
  });

  const sourceForm = document.getElementById("source-form");
  sourceForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const f = new FormData(sourceForm);
      const payload = {
        name: f.get("name"),
        source_type: f.get("source_type"),
        settings: parseJsonOrEmpty(f.get("settings")),
        is_active: true,
      };
      await api("/sources", { method: "POST", body: JSON.stringify(payload) });
      sourceForm.reset();
      await refreshSelectors();
      await refreshTopology();
      setFlash("Source saved");
    } catch (err) {
      setFlash(`Source save failed: ${err.message}`, "danger");
    }
  });

  const bindingForm = document.getElementById("binding-form");
  bindingForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const f = new FormData(bindingForm);
      const payload = {
        source_id: Number(f.get("source_id")),
        destination_id: Number(f.get("destination_id")),
        schedule_cron: f.get("schedule_cron"),
        policy: parseJsonOrEmpty(f.get("policy")),
        is_active: true,
      };
      await api("/bindings", { method: "POST", body: JSON.stringify(payload) });
      bindingForm.reset();
      await refreshBindings();
      await refreshTopology();
      setFlash("Binding created");
    } catch (err) {
      setFlash(`Binding create failed: ${err.message}`, "danger");
    }
  });

  document.getElementById("refresh-runs").addEventListener("click", refreshRuns);

  await refreshSelectors();
  await refreshBindings();
  await refreshTopology();
  await refreshRuns();
}

boot().catch((err) => {
  console.error(err);
  alert(`Failed to initialize UI: ${err.message}`);
});
