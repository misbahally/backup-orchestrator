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
}

async function refreshTopology() {
  const data = await api("/topology");
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

  if (data.nodes.length === 0) {
    graph = "graph LR\n  A[No configured sources] --> B[Create source and destination first]";
  }

  const el = document.getElementById("topology");
  el.textContent = graph;

  if (window.mermaid) {
    await window.mermaid.run({ querySelector: "#topology" });
  }
}

async function refreshRuns() {
  const runs = await api("/runs");
  const body = document.getElementById("runs-body");
  body.innerHTML = "";

  for (const run of runs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${run.id}</td>
      <td>${run.binding_id}</td>
      <td>${run.status}</td>
      <td>${run.bytes_transferred}</td>
      <td>${run.message || ""}</td>
    `;
    body.appendChild(tr);
  }
}

async function boot() {
  const destinationForm = document.getElementById("destination-form");
  destinationForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(destinationForm);
    await api("/destinations", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(f.entries())),
    });
    destinationForm.reset();
    await refreshSelectors();
    await refreshTopology();
  });

  const sourceForm = document.getElementById("source-form");
  sourceForm.addEventListener("submit", async (e) => {
    e.preventDefault();
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
  });

  const bindingForm = document.getElementById("binding-form");
  bindingForm.addEventListener("submit", async (e) => {
    e.preventDefault();
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
    await refreshTopology();
  });

  document.getElementById("refresh-runs").addEventListener("click", refreshRuns);

  await refreshSelectors();
  await refreshTopology();
  await refreshRuns();
}

boot().catch((err) => {
  console.error(err);
  alert(`Failed to initialize UI: ${err.message}`);
});
