"use strict";

const main = document.getElementById("dashboard-main");
const content = document.getElementById("dashboard-content");
const liveIndicator = document.getElementById("live-indicator");
const page = main?.dataset.page || "overview";
const entityId = main?.dataset.entityId || "";
let refreshTimer = null;

for (const link of document.querySelectorAll("[data-nav]")) {
  const nav = link.dataset.nav;
  if (nav === page || (page === "agent" && nav === "agents") || (page === "job" && nav === "jobs") ||
      (page === "conversation" && nav === "conversations") || (page === "artifact" && nav === "artifacts")) {
    link.classList.add("active");
  }
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function badge(value) {
  const text = value === null || value === undefined ? "unavailable" : String(value);
  return node("span", text, `badge ${text.toLowerCase().replace(/[^a-z0-9_-]/g, "-")}`);
}

function displayScalar(value) {
  if (value === null || value === undefined || value === "") return node("span", "—", "muted");
  if (typeof value === "boolean") return badge(value ? "yes" : "no");
  if (typeof value === "string" && /^(exact|estimated|unavailable|queued|importing|running|provisioning|succeeded|failed|interrupted|ready|claimed|expired|active|pending|superseded|ok|degraded)$/i.test(value)) return badge(value);
  const pre = node("pre");
  pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return pre;
}

function detailsPanel(value, heading) {
  const panel = node("section", null, "panel");
  if (heading) panel.append(node("h2", heading));
  const list = node("dl");
  for (const [key, item] of Object.entries(value || {})) {
    list.append(node("dt", key.replaceAll("_", " ")));
    const definition = node("dd");
    if (key === "usage" && item && typeof item === "object") definition.append(tokenUsage(item));
    else definition.append(displayScalar(item));
    list.append(definition);
  }
  panel.append(list);
  return panel;
}

function tokenUsage(usage) {
  const wrapper = node("div", null, "token-breakdown");
  const quality = usage.quality || (usage.input_tokens !== undefined ? "exact" : "unavailable");
  const title = node("div");
  title.append(badge(quality), document.createTextNode(` ${usage.total_tokens ?? "—"} total tokens`));
  wrapper.append(title);
  const contributors = Array.isArray(usage.contributors) ? usage.contributors : [];
  if (!contributors.length) {
    for (const component of ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]) {
      if (usage[component] !== undefined) contributors.push({component, tokens: usage[component], quality, provenance: usage.source || "Codex terminal usage"});
    }
  }
  for (const contributor of contributors) {
    const row = node("div", null, "token-contributor");
    row.append(node("span", contributor.component || "component"));
    row.append(node("span", contributor.tokens ?? "—"));
    row.append(badge(contributor.quality || "unavailable"));
    row.title = contributor.provenance || "";
    wrapper.append(row);
  }
  return wrapper;
}

function linkFor(item) {
  if (item.id && item.agent_id && item.status) return `/dashboard/jobs/${encodeURIComponent(item.id)}`;
  if (item.id && item.runner_service) return `/dashboard/agents/${encodeURIComponent(item.id)}`;
  if (item.key && item.agent_id) return `/dashboard/history/${encodeURIComponent(item.key)}`;
  if (item.id && item.media_type) return `/dashboard/artifacts/${encodeURIComponent(item.id)}`;
  return null;
}

function tablePanel(items) {
  if (!items.length) return node("div", "No matching records.", "empty");
  const preferred = [
    "id", "key", "name", "active", "active_jobs", "enabled", "agent_id",
    "conversation_key", "job_id", "sequence", "status", "model", "reasoning_effort",
    "introducing_sequence", "version", "kind", "path", "prompt", "result", "error",
    "revision", "media_type", "size_bytes", "file_count", "sha256",
    "resolved_git_commit", "created_at", "updated_at"
  ];
  const available = new Set(items.flatMap((item) => Object.keys(item || {})));
  const isAgentTable = items.every((item) => item && "runner_service" in item);
  const columnLimit = isAgentTable ? 6 : 8;
  const columns = preferred.filter((key) => available.has(key)).slice(0, columnLimit);
  const panel = node("section", null, "panel");
  const table = node("table");
  const head = node("thead");
  const headRow = node("tr");
  for (const column of columns) headRow.append(node("th", column.replaceAll("_", " ")));
  head.append(headRow); table.append(head);
  const body = node("tbody");
  for (const item of items) {
    const row = node("tr");
    const target = linkFor(item);
    for (const column of columns) {
      const cell = node("td");
      const value = item[column];
      if (target && (column === "id" || column === "key" || column === "name")) {
        const link = node("a", value);
        link.href = target;
        cell.append(link);
      } else if (column === "status") cell.append(badge(value));
      else if (typeof value === "boolean") cell.append(badge(value ? "yes" : "no"));
      else cell.textContent = value === null || value === undefined ? "—" : String(value);
      row.append(cell);
    }
    body.append(row);
  }
  table.append(body); panel.append(table);
  return panel;
}

function recordsPanel(items, heading) {
  const wrapper = node("section", null, "records");
  wrapper.append(node("h2", heading), tablePanel(items));
  return wrapper;
}

async function fetchJson(url) {
  const response = await fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}});
  if (response.status === 401) { window.location.assign("/dashboard/login"); throw new Error("Authentication required"); }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { const payload = await response.json(); message = payload.detail || payload.message || message; } catch (_) { /* no-op */ }
    throw new Error(message);
  }
  return response.json();
}

async function renderOverview() {
  const summary = await fetchJson("/dashboard/api/v1/summary");
  const cards = node("section", null, "cards");
  const entries = [];
  for (const [group, value] of Object.entries(summary)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [key, count] of Object.entries(value)) entries.push([`${group} · ${key}`, count]);
    } else entries.push([group, value]);
  }
  for (const [label, value] of entries) {
    const card = node("article", null, "card");
    card.append(node("div", label.replaceAll("_", " "), "label"), node("div", value, "value"));
    cards.append(card);
  }
  content.replaceChildren(cards);
}

async function renderList(kind) {
  const payload = await fetchJson(`/dashboard/api/v1/${kind}?limit=50`);
  content.replaceChildren(tablePanel(payload.items || []));
}

async function renderDetail(kind, id) {
  const value = await fetchJson(`/dashboard/api/v1/${kind}/${encodeURIComponent(id)}`);
  const companionAdditions = Array.isArray(value.companion_additions) ? value.companion_additions : [];
  const detail = {...value};
  delete detail.companion_additions;
  content.replaceChildren(detailsPanel(detail));
  if (kind === "jobs") {
    if (companionAdditions.length) content.append(recordsPanel(companionAdditions, "Companion additions"));
    const events = await fetchJson(`/dashboard/api/v1/jobs/${encodeURIComponent(id)}/events?limit=200`);
    content.append(tablePanel(events.items || []));
  } else if (kind === "conversations") {
    const companions = await fetchJson(`/dashboard/api/v1/conversations/${encodeURIComponent(id)}/companions?limit=200`);
    if ((companions.items || []).length) content.append(recordsPanel(companions.items, "Active and pending companions"));
    const turns = await fetchJson(`/dashboard/api/v1/conversations/${encodeURIComponent(id)}/turns`);
    content.append(tablePanel(turns.items || []));
  } else if (kind === "agents") {
    const revisions = await fetchJson(`/dashboard/api/v1/agents/${encodeURIComponent(id)}/revisions?limit=50`);
    content.append(tablePanel(revisions.items || []));
  } else if (kind === "artifacts") {
    const actions = node("div", null, "actions");
    const download = node("a", "Download original");
    download.href = `/dashboard/api/v1/artifacts/${encodeURIComponent(id)}/download`;
    download.className = "badge";
    actions.append(download); content.append(actions);
    await renderArtifactPreview(id);
  }
}

async function renderArtifactPreview(id) {
  const response = await fetch(`/dashboard/api/v1/artifacts/${encodeURIComponent(id)}/preview`, {credentials: "same-origin"});
  if (!response.ok) return;
  const type = response.headers.get("content-type") || "";
  const panel = node("section", null, "panel");
  panel.append(node("h2", "Safe preview"));
  if (type.startsWith("image/png") || type.startsWith("image/jpeg")) {
    const blob = await response.blob();
    const image = node("img"); image.className = "artifact-preview"; image.alt = "Sanitized artifact preview";
    const url = URL.createObjectURL(blob); image.src = url; image.addEventListener("load", () => URL.revokeObjectURL(url), {once: true});
    panel.append(image);
  } else {
    const payload = await response.json();
    panel.append(displayScalar(payload.text || ""));
  }
  content.append(panel);
}

async function renderSystem(debug) {
  const value = await fetchJson(debug ? "/dashboard/api/v1/debug" : "/dashboard/api/v1/system");
  content.replaceChildren(detailsPanel(value));
  if (debug) {
    const actions = node("div", null, "actions");
    const link = node("a", "Download redacted diagnostics"); link.href = "/dashboard/api/v1/debug/bundle"; link.className = "badge";
    actions.append(link); content.prepend(actions);
  }
}

async function loadPage() {
  try {
    if (page === "overview") await renderOverview();
    else if (["agents", "jobs", "artifacts"].includes(page)) await renderList(page);
    else if (page === "conversations") await renderList("conversations");
    else if (page === "agent") await renderDetail("agents", entityId);
    else if (page === "job") await renderDetail("jobs", entityId);
    else if (page === "conversation") await renderDetail("conversations", entityId);
    else if (page === "artifact") await renderDetail("artifacts", entityId);
    else if (page === "system") await renderSystem(false);
    else if (page === "debug") await renderSystem(true);
  } catch (error) {
    content.replaceChildren(node("p", error instanceof Error ? error.message : "Unable to load dashboard.", "alert"));
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadPage, 350);
}

const events = new EventSource("/dashboard/events");
events.onopen = () => { liveIndicator.classList.add("live"); liveIndicator.lastChild.textContent = " Live"; };
events.onerror = () => { liveIndicator.classList.remove("live"); liveIndicator.lastChild.textContent = " Reconnecting"; };
for (const eventName of ["task.updated", "agent.updated", "conversation.updated", "artifact.created", "worker.updated", "system.degraded", "system.recovered", "job_event", "reset"]) {
  events.addEventListener(eventName, scheduleRefresh);
}

loadPage();
window.setInterval(() => {
  if (document.visibilityState === "visible") loadPage();
}, 30000);
