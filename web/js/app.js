const state = {
  catalog: null,
  results: null,
  diagrams: {},
  chart: null,
  family: "rtl",
  activeComponent: null,
  activeDiagramId: null,
  detailTab: "source",
  view: "home",
};

function waitForMermaid() {
  if (window.__mermaid) return Promise.resolve(window.__mermaid);
  return new Promise((resolve) => {
    document.addEventListener("mermaid-ready", () => resolve(window.__mermaid), {
      once: true,
    });
  });
}

async function loadJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to load ${path}: ${res.status}`);
  return res.json();
}

async function loadText(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to load ${path}: ${res.status}`);
  return res.text();
}

function familyMeta(id) {
  return state.catalog.families.find((f) => f.id === id) ?? { id, label: id, sourceLabel: id };
}

function componentsInFamily(family) {
  return state.catalog.components.filter((c) => c.family === family);
}

function findComponent(id) {
  return state.catalog.components.find((c) => c.id === id);
}

function parseRoute() {
  let raw = (location.hash || "#home").replace(/^#/, "");
  // Legacy redirects
  if (raw === "rtl" || raw.startsWith("rtl/")) {
    raw = raw.replace(/^rtl/, "catalog/rtl");
  }

  const parts = raw.split("/").filter(Boolean);
  const view = parts[0] || "home";

  if (view === "catalog") {
    return {
      view: "catalog",
      family: parts[1] === "cpp" || parts[1] === "rtl" ? parts[1] : null,
      componentId: parts[2] || null,
    };
  }
  if (view === "connect") return { view: "connect", family: null, componentId: null };
  return { view: "home", family: null, componentId: null };
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((el) => {
    const on = el.dataset.view === view;
    el.classList.toggle("is-active", on);
    el.hidden = !on;
  });
  document.querySelectorAll("a[data-nav]").forEach((a) => {
    const inNav = a.closest(".nav-links, .mobile-menu, .wordmark, .nav-actions");
    if (!inNav) return;
    if (a.dataset.nav === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-dot]").forEach((dot) => {
    dot.hidden = dot.dataset.dot !== view;
  });
  closeMobile();
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
}

function closeMobile() {
  const menu = document.getElementById("mobile-menu");
  const toggle = document.getElementById("menu-toggle");
  menu.hidden = true;
  menu.classList.remove("is-open");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Open menu");
}

function initNav() {
  const header = document.getElementById("site-header");
  const sentinel = document.getElementById("nav-sentinel");
  const observer = new IntersectionObserver(([entry]) => {
    header.classList.toggle("is-solid", !entry.isIntersecting);
  });
  observer.observe(sentinel);

  const toggle = document.getElementById("menu-toggle");
  const menu = document.getElementById("mobile-menu");
  toggle.addEventListener("click", () => {
    const open = menu.hidden;
    menu.hidden = !open;
    menu.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    header.classList.toggle("is-solid", open || header.classList.contains("is-solid"));
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMobile();
  });

  document.getElementById("family-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-family]");
    if (!btn) return;
    location.hash = `#catalog/${btn.dataset.family}`;
  });

  document.getElementById("component-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-id]");
    if (!btn) return;
    location.hash = `#catalog/${state.family}/${btn.dataset.id}`;
  });
}

function syncFamilyTabs() {
  document.querySelectorAll("#family-tabs button").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.family === state.family));
  });
}

function renderComponentList() {
  const list = document.getElementById("component-list");
  const items = componentsInFamily(state.family);
  list.innerHTML = items
    .map((c) => {
      const planned = c.status === "planned";
      const current = state.activeComponent === c.id;
      return `<li class="${planned ? "planned" : ""}">
        <button type="button" data-id="${c.id}" ${current ? 'aria-current="true"' : ""}>
          <span class="name">${c.name}</span>
          <span class="meta">${c.status}</span>
        </button>
      </li>`;
    })
    .join("");
}

function formatValue(v, metric) {
  if (typeof v !== "number") return String(v);
  if (metric === "miss_rate") return v.toFixed(4);
  return String(v);
}

function palette(n) {
  const colors = ["#0369a1", "#0b0c0e", "#555963", "#0b7a52", "#38bdf8"];
  return Array.from({ length: n }, (_, i) => colors[i % colors.length]);
}

function renderResults(presetId) {
  const preset = state.results.presets[presetId];
  if (!preset) return;

  document.getElementById("results-meta").innerHTML =
    `<strong>${preset.title}</strong> · ${preset.fixed}<br /><code>${preset.command}</code>`;
  document.getElementById("results-insight").textContent = preset.insight;
  document.getElementById("results-note").textContent = state.results.note;

  const table = document.getElementById("results-table");
  table.innerHTML = `<thead><tr><th>${preset.axis}</th>${preset.columns
    .map((c) => `<th>${c}</th>`)
    .join("")}</tr></thead><tbody>${preset.rows
    .map(
      (row) =>
        `<tr><td>${row.label}</td>${row.values
          .map((v) => `<td>${formatValue(v, preset.metric)}</td>`)
          .join("")}</tr>`
    )
    .join("")}</tbody>`;

  const ctx = document.getElementById("results-chart");
  if (!ctx || typeof Chart === "undefined") return;

  const colors = palette(preset.rows.length);
  const datasets = preset.rows.map((row, i) => ({
    label: row.label,
    data: row.values,
    borderColor: colors[i],
    backgroundColor: colors[i],
    tension: 0.15,
    pointRadius: 3,
  }));

  if (state.chart) state.chart.destroy();
  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels: preset.columns, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "bottom",
          labels: { boxWidth: 12, font: { family: "Geist Sans, sans-serif", size: 11 } },
        },
      },
      scales: {
        y: {
          title: { display: true, text: preset.metric.replace("_", " ") },
          beginAtZero: true,
          max: preset.metric === "miss_rate" ? 1 : undefined,
        },
        x: { title: { display: true, text: preset.axis } },
      },
    },
  });
}

let diagramRenderSeq = 0;

async function showDiagram(id) {
  if (id) state.activeDiagramId = id;
  const diagramId = state.activeDiagramId;
  const frame = document.getElementById("diagram-frame");
  const panel = document.querySelector('.detail-panel[data-panel="diagram"]');
  if (!frame || !diagramId) return;

  // Mermaid lays out from the container size. Skip while the panel is hidden
  // or we get a collapsed SVG; render once the tab is visible.
  if (panel?.hidden) {
    frame.innerHTML = `<p class="hint" style="margin:0">Switch to this tab to render the diagram.</p>`;
    return;
  }

  // Always re-fetch so edits to .mmd show up without a hard reload.
  let src = state.diagrams[diagramId];
  const meta = findComponent(state.activeComponent)?.diagrams?.find((d) => d.id === diagramId);
  if (meta?.file) {
    try {
      src = await loadText(`diagrams/${meta.file}`);
      state.diagrams[diagramId] = src;
    } catch {
      /* keep cached */
    }
  }
  if (!src) {
    frame.innerHTML = `<p class="err">No diagram for ${diagramId}</p>`;
    return;
  }

  const seq = ++diagramRenderSeq;
  const mermaid = await waitForMermaid();
  if (seq !== diagramRenderSeq) return;

  frame.innerHTML = `<p class="hint" style="margin:0">Rendering…</p>`;

  try {
    const renderId = `mmd${Date.now()}${seq}`;
    const { svg } = await mermaid.render(renderId, src);
    if (seq !== diagramRenderSeq) return;
    frame.innerHTML = svg;
    const el = frame.querySelector("svg");
    if (el) {
      el.removeAttribute("width");
      el.removeAttribute("height");
      el.style.width = "100%";
      el.style.maxWidth = "100%";
      el.style.height = "auto";
      el.setAttribute("role", "img");
      el.setAttribute("aria-label", `${diagramId} diagram`);
    }
  } catch (err) {
    if (seq !== diagramRenderSeq) return;
    frame.innerHTML = `<p class="err">Mermaid error: ${err.message || err}</p><pre class="rtl-view" style="max-height:12rem"></pre>`;
    frame.querySelector("pre").textContent = src;
  }
}

async function loadSourceFile(path) {
  const view = document.getElementById("source-view");
  if (!view) return;
  view.textContent = "Loading…";
  try {
    view.textContent = await loadText(path);
  } catch (err) {
    view.textContent = err.message;
  }
}

async function ensureDiagrams(component) {
  if (!component.diagrams) return;
  await Promise.all(
    component.diagrams.map(async (d) => {
      if (!state.diagrams[d.id]) {
        state.diagrams[d.id] = await loadText(`diagrams/${d.file}`);
      }
    })
  );
}

async function setDetailTab(tab) {
  state.detailTab = tab;
  document.querySelectorAll("#detail-tabs button").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === tab));
  });
  document.querySelectorAll(".detail-panel").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tab;
  });
  if (tab === "diagram") {
    const comp = findComponent(state.activeComponent);
    const fallback = comp?.diagrams?.[0]?.id;
    await showDiagram(state.activeDiagramId || fallback);
  }
  if (tab === "performance" && state.chart) {
    // Chart.js also needs a visible canvas to size correctly.
    state.chart.resize();
  }
}

async function wireActivePanels(component) {
  const family = familyMeta(component.family);

  if (component.sources?.length) {
    const select = document.getElementById("source-select");
    select?.addEventListener("change", (e) => loadSourceFile(e.target.value));
    await loadSourceFile(component.sources[0].path);
  }

  if (component.diagrams?.length) {
    await ensureDiagrams(component);
    state.activeDiagramId = component.diagrams[0].id;
    const tabs = document.getElementById("diagram-tabs");
    tabs?.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-id]");
      if (!btn) return;
      tabs.querySelectorAll("button").forEach((b) => {
        b.setAttribute("aria-selected", String(b === btn));
      });
      showDiagram(btn.dataset.id);
    });
  }

  if (component.presets?.length) {
    const presets = document.getElementById("preset-tabs");
    presets?.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-id]");
      if (!btn) return;
      presets.querySelectorAll("button").forEach((b) => {
        b.setAttribute("aria-selected", String(b === btn));
      });
      renderResults(btn.dataset.id);
    });
    renderResults(component.presets[0]);
  }

  document.getElementById("detail-tabs")?.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    setDetailTab(btn.dataset.tab);
  });

  await setDetailTab(state.detailTab || "source");
  void family;
}

async function renderActiveComponent(id) {
  const detail = document.getElementById("component-detail");
  const component = id ? findComponent(id) : null;

  document.querySelectorAll("#component-list button").forEach((btn) => {
    if (btn.dataset.id === id) btn.setAttribute("aria-current", "true");
    else btn.removeAttribute("aria-current");
  });

  if (!component || component.family !== state.family) {
    detail.innerHTML = `<div class="detail-empty"><p>Select a component on the left.</p></div>`;
    state.activeComponent = null;
    return;
  }

  state.activeComponent = id;
  state.activeDiagramId = component.diagrams?.[0]?.id ?? null;
  const family = familyMeta(component.family);
  const sourceLabel = family.sourceLabel || "Source";

  if (component.status !== "active") {
    detail.innerHTML = `
      <div class="detail-header">
        <p class="eyebrow">Planned · ${family.label}</p>
        <h2>${component.name}</h2>
        <p>${component.summary}</p>
      </div>
      <div class="detail-tabs" role="tablist" aria-label="Detail">
        <button type="button" role="tab" data-tab="source" aria-selected="true" disabled>${sourceLabel}</button>
        <button type="button" role="tab" data-tab="diagram" aria-selected="false" disabled>Block diagram</button>
        <button type="button" role="tab" data-tab="performance" aria-selected="false" disabled>Performance results</button>
      </div>
      <div class="planned-panel">
        Same three panes as live components — ${sourceLabel.toLowerCase()} sources, curated
        diagrams, and performance results — once this block ships.
      </div>`;
    return;
  }

  detail.innerHTML = `
    <div class="detail-header">
      <p class="eyebrow">Active · ${family.label}</p>
      <h2>${component.name}</h2>
      <p>${component.summary}</p>
    </div>

    <div class="detail-tabs" id="detail-tabs" role="tablist" aria-label="Detail">
      <button type="button" role="tab" data-tab="source" aria-selected="true">${sourceLabel}</button>
      <button type="button" role="tab" data-tab="diagram" aria-selected="false">Block diagram</button>
      <button type="button" role="tab" data-tab="performance" aria-selected="false">Performance results</button>
    </div>

    <div class="detail-panel" data-panel="source">
      <p class="hint">${family.sourceHint || ""}</p>
      <div class="rtl-toolbar">
        <label>
          File
          <select id="source-select" aria-label="${sourceLabel} source file">
            ${component.sources
              .map((f) => `<option value="${f.path}">${f.label}</option>`)
              .join("")}
          </select>
        </label>
      </div>
      <pre class="rtl-view" id="source-view" tabindex="0">Loading…</pre>
    </div>

    <div class="detail-panel" data-panel="diagram" hidden>
      <p class="hint">Curated Mermaid — hierarchy from modules; address decode and FSM for intent.</p>
      <div class="diagram-tabs" id="diagram-tabs" role="tablist">
        ${component.diagrams
          .map(
            (d, i) =>
              `<button type="button" role="tab" data-id="${d.id}" aria-selected="${i === 0}">${d.title}</button>`
          )
          .join("")}
      </div>
      <div class="diagram-frame" id="diagram-frame"><p class="hint" style="margin:0">Switch to this tab to render the diagram.</p></div>
    </div>

    <div class="detail-panel" data-panel="performance" hidden>
      <p class="hint">Presets for this component. Sample tables until fresh CSVs are wired in.</p>
      <div class="preset-tabs" id="preset-tabs" role="tablist">
        ${component.presets
          .map(
            (pid, i) =>
              `<button type="button" role="tab" data-id="${pid}" aria-selected="${i === 0}">${
                state.results.presets[pid]?.title ?? pid
              }</button>`
          )
          .join("")}
      </div>
      <p class="results-meta" id="results-meta"></p>
      <div class="chart-wrap"><canvas id="results-chart" aria-label="Performance chart"></canvas></div>
      <div class="table-scroll"><table class="results" id="results-table"></table></div>
      <p class="insight" id="results-insight"></p>
      <p class="note" id="results-note"></p>
    </div>
  `;

  state.detailTab = "source";
  await wireActivePanels(component);
}

async function applyRoute() {
  const { view, family, componentId } = parseRoute();
  setView(view);

  if (view !== "catalog") return;

  if (!family) {
    location.replace("#catalog/rtl");
    return;
  }

  const familyChanged = state.family !== family;
  state.family = family;
  syncFamilyTabs();
  renderComponentList();

  if (componentId) {
    const comp = findComponent(componentId);
    if (!comp || comp.family !== family) {
      location.replace(`#catalog/${family}`);
      return;
    }
    if (familyChanged) state.detailTab = "source";
    await renderActiveComponent(componentId);
    return;
  }

  const first =
    componentsInFamily(family).find((c) => c.status === "active") ||
    componentsInFamily(family)[0];
  if (first) {
    location.replace(`#catalog/${family}/${first.id}`);
    return;
  }

  await renderActiveComponent(null);
}

function initContactForm() {
  const form = document.getElementById("contact-form");
  const note = document.getElementById("contact-form-note");
  if (!form) return;

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const data = new FormData(form);
    if (data.get("botcheck")) return;

    const name = String(data.get("name") ?? "").trim();
    const email = String(data.get("email") ?? "").trim();
    const topic = String(data.get("topic") ?? "").trim();
    const message = String(data.get("message") ?? "").trim();
    if (!name || !email || !message) {
      form.reportValidity();
      return;
    }

    const subject = `asic only enquiry${topic && topic !== "Not sure yet" ? ` — ${topic}` : ""}`;
    const body = [
      `Name: ${name}`,
      `Email: ${email}`,
      `Topic: ${topic || "—"}`,
      "",
      message,
    ].join("\n");

    window.location.href = `mailto:patrickadrianpineda@gmail.com?subject=${encodeURIComponent(
      subject
    )}&body=${encodeURIComponent(body)}`;

    note.textContent = "Your email app should open now. Thanks for reaching out!";
    note.classList.add("is-ok");
  });
}

async function main() {
  initNav();
  initContactForm();

  try {
    state.catalog = await loadJson("data/catalog.json");
    state.results = await loadJson("data/results.json");
  } catch (err) {
    document.body.insertAdjacentHTML(
      "afterbegin",
      `<p class="err container">Could not load site data. Serve from the web/ directory. ${err.message}</p>`
    );
    return;
  }

  window.addEventListener("hashchange", () => {
    applyRoute();
  });
  await applyRoute();
}

main();
