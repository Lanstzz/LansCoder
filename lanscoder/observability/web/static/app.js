(() => {
  "use strict";

  const app = document.querySelector("#app");
  const tabs = new Set(["overview", "input", "output", "metadata", "raw"]);
  let requestController = null;
  let pollTimer = null;
  const payloadControllers = new Set();

  const element = (name, options = {}) => {
    const node = document.createElement(name);
    if (options.className) node.className = options.className;
    if (options.text !== undefined && options.text !== null) node.textContent = String(options.text);
    if (options.type) node.type = options.type;
    if (options.href) node.href = options.href;
    if (options.title) node.title = options.title;
    if (options.value !== undefined) node.value = options.value;
    if (options.checked !== undefined) node.checked = options.checked;
    if (options.disabled !== undefined) node.disabled = options.disabled;
    if (options.name) node.name = options.name;
    if (options.placeholder) node.placeholder = options.placeholder;
    if (options.role) node.setAttribute("role", options.role);
    if (options.ariaLabel) node.setAttribute("aria-label", options.ariaLabel);
    if (options.scope) node.scope = options.scope;
    return node;
  };
  const append = (parent, ...children) => {
    children.flat().filter(Boolean).forEach((child) => parent.append(child));
    return parent;
  };
  const button = (text, listener, className = "button") => {
    const node = element("button", { text, type: "button", className });
    node.addEventListener("click", listener);
    return node;
  };
  const link = (text, href, className = "") => element("a", { text, href, className });
  const code = (value) => element("code", { text: value ?? "—", className: "mono" });
  const localTime = (value) => {
    if (!value) return element("span", { text: "—" });
    const node = element("time", { text: new Date(value).toLocaleString(), title: value });
    node.dateTime = value;
    return node;
  };
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const json = (value) => JSON.stringify(value, null, 2);
  const pre = (value) => element("pre", { text: json(value) });
  const statusBadge = (value) => element("span", { text: value, className: `badge ${value}` });
  const state = (title, detail, error = false) => append(element("section", { className: `state${error ? " error" : ""}` }), element("h2", { text: title }), detail ? element("p", { text: detail, className: "subtle" }) : null);
  const diagnostics = (items) => {
    if (!items?.length) return null;
    const details = element("details");
    append(details, element("summary", { text: "Diagnostics" }), pre(items));
    return append(element("section", { className: "diagnostics" }), element("strong", { text: "Some journal records could not be read" }), details);
  };
  const pageHead = (title, detail, actions) => append(element("header", { className: "page-head" }), append(element("div"), element("h1", { text: title }), detail ? append(element("p", { className: "subtle" }), typeof detail === "string" ? document.createTextNode(detail) : detail) : null), actions ? append(element("div", { className: "page-actions" }), actions) : null);
  const setNavigation = () => {
    const section = location.pathname.startsWith("/sessions") ? "sessions" : "traces";
    document.querySelectorAll("[data-nav]").forEach((node) => {
      if (node.dataset.nav === section) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    });
  };
  const stopPending = () => {
    if (requestController) requestController.abort();
    requestController = null;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    payloadControllers.forEach((controller) => controller.abort());
    payloadControllers.clear();
  };
  const request = async (path) => {
    requestController = new AbortController();
    const response = await fetch(path, { signal: requestController.signal, headers: { Accept: "application/json" } });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.error?.message || "Request failed");
    return data;
  };
  const navigate = (href, replace = false) => {
    const destination = new URL(href, location.origin);
    if (destination.origin !== location.origin) return;
    if (replace) history.replaceState({}, "", `${destination.pathname}${destination.search}`);
    else history.pushState({}, "", `${destination.pathname}${destination.search}`);
    renderRoute();
  };
  const apiQuery = (params) => params.toString() ? `?${params}` : "";
  const routeLink = (node) => node.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    navigate(node.getAttribute("href"));
  });

  function renderFilterInput(form, labelText, name, type = "text", wide = false) {
    const label = element("label", { className: wide ? "wide" : "" });
    const queryValue = new URLSearchParams(location.search).get(name) || "";
    const value = type === "datetime-local" && queryValue ? localInputValue(queryValue) : queryValue;
    const input = element("input", { name, type, value });
    append(label, element("span", { text: labelText }), input);
    form.append(label);
  }

  function localInputValue(value) {
    const instant = new Date(value);
    if (Number.isNaN(instant.getTime())) return "";
    const offset = instant.getTimezoneOffset() * 60_000;
    return new Date(instant.getTime() - offset).toISOString().slice(0, 16);
  }

  function explorerFilters() {
    const params = new URLSearchParams(location.search);
    const details = element("details", { className: "filters" });
    append(details, element("summary", { text: "Filters" }));
    const form = element("form", { className: "filter-form" });
    const metadataEntry = [...params.entries()].find(([key]) => key.startsWith("metadata."));
    [
      ["Project", "project"], ["Session", "session_id"], ["Model", "model"], ["Provider", "provider"],
      ["Tool", "tool"], ["From", "from", "datetime-local"], ["To", "to", "datetime-local"],
      ["Min duration (ms)", "min_duration_ms", "number"], ["Max duration (ms)", "max_duration_ms", "number"],
      ["Min tokens", "min_tokens", "number"], ["Max tokens", "max_tokens", "number"],
      ["Min observations", "min_observation_count", "number"], ["Max observations", "max_observation_count", "number"],
      ["Metadata key", "metadata_key"], ["Metadata value (JSON scalar)", "metadata_value"],
    ].forEach(([labelText, name, type]) => renderFilterInput(form, labelText, name, type));
    if (metadataEntry) {
      form.elements.metadata_key.value = metadataEntry[0].replace(/^metadata\./, "");
      form.elements.metadata_value.value = metadataEntry[1];
    }
    const status = element("label");
    const select = element("select", { name: "status" });
    append(select, element("option", { text: "Any status", value: "" }));
    ["running", "waiting_for_input", "completed", "failed", "cancelled"].forEach((value) => append(select, element("option", { text: value, value })));
    select.value = params.get("status") || "";
    append(status, element("span", { text: "Status" }), select);
    form.append(status);
    const errorLabel = element("label");
    const errorSelect = element("select", { name: "has_error" });
    [["Any error state", ""], ["Has error", "true"], ["No error", "false"]].forEach(([labelText, value]) => append(errorSelect, element("option", { text: labelText, value })));
    errorSelect.value = params.get("has_error") || "";
    append(errorLabel, element("span", { text: "Error" }), errorSelect);
    form.append(errorLabel);
    const tags = element("div", { className: "wide" });
    append(tags, element("span", { text: "Tags" }));
    const tagRows = element("div", { className: "filter-tags" });
    const addTag = (value = "") => tagRows.append(element("input", { name: "tag", value, placeholder: "Tag" }));
    (params.getAll("tag").length ? params.getAll("tag") : [""]).forEach(addTag);
    append(tags, tagRows, button("Add tag", () => addTag()));
    form.append(tags);
    const actions = element("div", { className: "filter-actions wide" });
    append(actions, button("Apply", () => form.requestSubmit(), "button button-primary"), button("Clear filters", () => navigate("/traces")));
    form.append(actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const next = new URLSearchParams();
      const data = new FormData(form);
      for (const [name, value] of data.entries()) {
        const stringValue = String(value).trim();
        if (!stringValue) continue;
        if (name === "metadata_key" || name === "metadata_value") continue;
        if (name === "from" || name === "to") next.append(name, new Date(stringValue).toISOString());
        else next.append(name, stringValue);
      }
      const metadataKey = String(data.get("metadata_key") || "").trim();
      const metadataValue = String(data.get("metadata_value") || "").trim();
      if (metadataKey && metadataValue) next.append(`metadata.${metadataKey}`, metadataValue);
      navigate(`/traces${apiQuery(next)}`);
    });
    append(details, form);
    return details;
  }

  function chips(params) {
    const entries = [...params.entries()].filter(([key]) => key !== "cursor" && key !== "limit");
    if (!entries.length) return null;
    const container = element("div", { className: "chips" });
    entries.forEach(([key, value], index) => {
      const chip = element("span", { className: "chip", text: `${key}: ${value}` });
      chip.append(button("Remove", () => {
        const next = new URLSearchParams(location.search);
        const kept = [...next.entries()].filter((entry, position) => entry[0] !== key || entry[1] !== value || position !== index);
        navigate(`/traces${apiQuery(new URLSearchParams(kept))}`);
      }));
      container.append(chip);
    });
    return container;
  }

  function traceRow(trace) {
    const row = element("tr");
    row.tabIndex = 0;
    const open = () => navigate(`/traces/${encodeURIComponent(trace.trace_id)}`);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    const input = append(element("td"), element("div", { text: text(trace.input_preview, "Input unavailable"), className: "trace-input" }), code(trace.trace_id));
    const model = append(element("td"), element("div", { text: text(trace.model) }), element("small", { text: text(trace.provider) }));
    append(row,
      append(element("td"), statusBadge(trace.status)),
      input,
      element("td", { }).appendChild(localTime(trace.started_at)).parentElement,
      element("td", { text: trace.duration_ms === null ? "—" : `${trace.duration_ms} ms` }),
      model,
      element("td", { text: text(trace.total_tokens) }),
      element("td", { text: text(trace.observation_count) }),
      append(element("td"), statusBadge(trace.branch_state), trace.detached ? statusBadge("detached") : null),
      append(element("td"), statusBadge(trace.evidence)));
    return row;
  }

  async function renderExplorer() {
    const root = append(element("section"), pageHead("Traces", "Live trace records"), explorerFilters(), chips(new URLSearchParams(location.search)));
    app.replaceChildren(root);
    try {
      const data = await request(`/api/v1/traces${location.search}`);
      root.append(diagnostics(data.diagnostics));
      if (!data.items.length) {
        const messages = { no_traces: "No traces recorded", filtered_empty: "No traces match these filters", corrupt: "Some journal records could not be read" };
        const empty = state(messages[data.empty_reason] || "No traces recorded", data.empty_reason === "filtered_empty" ? "Adjust or clear filters to continue." : "");
        if (data.empty_reason === "filtered_empty") empty.append(button("Clear filters", () => navigate("/traces")));
        root.append(empty);
        return;
      }
      const table = element("table");
      const head = element("thead");
      const row = element("tr");
      ["Status", "Trace / Input preview", "Started (local)", "Duration", "Model / Provider", "Tokens", "Observations", "Branch", "Evidence"].forEach((label) => row.append(element("th", { text: label, scope: "col" })));
      head.append(row);
      const body = element("tbody");
      data.items.forEach((trace) => body.append(traceRow(trace)));
      append(table, head, body);
      root.append(append(element("div", { className: "table-wrap" }), table));
      const controls = element("div", { className: "pagination" });
      const params = new URLSearchParams(location.search);
      if (params.has("cursor") && history.length > 1) append(controls, button("Previous page", () => history.back()));
      if (params.has("cursor")) append(controls, button("First page", () => { params.delete("cursor"); navigate(`/traces${apiQuery(params)}`); }));
      if (data.next_cursor) append(controls, button("Next page", () => { params.set("cursor", data.next_cursor); navigate(`/traces${apiQuery(params)}`); }));
      append(controls, element("span", { text: `${data.total} matching · ${data.unfiltered_total} total`, className: "subtle" }));
      root.append(controls, element("p", { text: "Live data may shift between pages.", className: "notice" }));
    } catch (error) {
      if (error.name !== "AbortError") root.append(state("Request failed", error.message, true));
    }
  }

  function dataRows(value) {
    if (value === null || value === undefined || (typeof value === "object" && !Object.keys(value).length)) return element("p", { text: "No evidence recorded.", className: "subtle" });
    if (typeof value !== "object" || Array.isArray(value)) return pre(value);
    const container = element("div", { className: "data-list" });
    Object.entries(value).forEach(([key, item]) => {
      const row = element("div", { className: "data-row" });
      append(row, element("span", { text: key, className: "data-label" }), typeof item === "object" ? pre(item) : element("span", { text: text(item) }));
      container.append(row);
    });
    return container;
  }

  function descriptorView(descriptor) {
    const container = element("div", { className: "payload" });
    if (!descriptor || typeof descriptor !== "object" || !descriptor.sha256) return dataRows(descriptor);
    append(container, code(descriptor.sha256), element("span", { text: `${descriptor.media_type} · ${descriptor.size_bytes} bytes`, className: "subtle" }));
    if (descriptor.preview === "metadata_only") return append(container, element("p", { text: "Payload is available but cannot be previewed.", className: "subtle" }));
    if (descriptor.availability === "missing") return append(container, element("p", { text: "Payload missing", className: "subtle" }));
    if (descriptor.availability === "corrupt") return append(container, element("p", { text: "Payload integrity check failed", className: "subtle" }));
    container.append(button("View payload", async () => {
      const result = element("div", { className: "subtle", text: "Loading payload…" });
      container.append(result);
      const controller = new AbortController();
      payloadControllers.add(controller);
      try {
        const response = await fetch(descriptor.url, { signal: controller.signal, headers: { Accept: descriptor.media_type } });
        if (!response.ok) {
          const problem = await response.json().catch(() => null);
          throw new Error(problem?.error?.code === "payload_missing" ? "Payload missing" : "Payload integrity check failed");
        }
        const raw = await response.text();
        let shown = raw;
        if (descriptor.preview === "json") { try { shown = json(JSON.parse(raw)); } catch {} }
        result.replaceWith(element("pre", { text: shown }));
      } catch (error) {
        if (error.name !== "AbortError") result.replaceWith(element("p", { text: error.message, className: "subtle" }));
      } finally {
        payloadControllers.delete(controller);
      }
    }, "button button-primary"));
    return container;
  }

  function evidence(trace, observation, tab) {
    const pane = append(element("section", { className: "pane evidence-pane" }), append(element("div", { className: "pane-head" }), element("h2", { text: "Evidence" }), observation ? element("p", { text: observation.display_name, className: "subtle" }) : null));
    const tabList = element("div", { className: "tabs", role: "tablist", ariaLabel: "Evidence" });
    ["overview", "input", "output", "metadata", "raw"].forEach((name) => {
      const item = button(name[0].toUpperCase() + name.slice(1), () => {
        const query = new URLSearchParams(location.search); query.set("tab", name); navigate(`${location.pathname}?${query}`);
      }, "tab");
      item.role = "tab";
      item.setAttribute("aria-selected", String(tab === name));
      tabList.append(item);
    });
    pane.append(tabList);
    if (!observation) return append(pane, state("No observations", "No observation evidence was recorded."));
    if (tab === "overview") append(pane, dataRows({ ...observation.overview, error: observation.error, diagnostics: observation.diagnostics, relations: observation.relations }));
    else if (tab === "input" || tab === "output") pane.append(descriptorView(observation[tab]));
    else if (tab === "metadata") pane.append(dataRows(observation.metadata));
    else {
      const details = element("details");
      append(details, element("summary", { text: "Raw observation events" }), pre(observation.raw));
      pane.append(details);
    }
    return pane;
  }

  function observationTree(observations, selectedId) {
    const pane = append(element("section", { className: "pane" }), append(element("div", { className: "pane-head" }), element("h2", { text: "Observation tree" })));
    const tree = element("div", { className: "tree", role: "tree" });
    observations.forEach((observation) => {
      const node = element("button", { className: `tree-node${observation.observation_id === selectedId ? " activity" : ""}`, type: "button" });
      node.style.setProperty("--depth", String(observation.depth || 0));
      append(node, element("strong", { text: observation.display_name }), append(element("span", { className: "node-meta" }), element("span", { text: observation.type }), document.createTextNode(" · "), statusBadge(observation.status), observation.incomplete ? statusBadge("incomplete") : null, observation.duration_ms === null ? null : document.createTextNode(` · ${observation.duration_ms} ms`)));
      node.addEventListener("click", () => selectObservation(observation.observation_id));
      tree.append(node);
    });
    return append(pane, tree);
  }

  function waterfall(observations, selectedId) {
    const pane = append(element("section", { className: "pane" }), append(element("div", { className: "pane-head" }), element("h2", { text: "Timeline / Waterfall" })));
    const allEnds = observations.map((item) => (item.start_offset_ms || 0) + (item.duration_ms || 0)).filter(Number.isFinite);
    const span = Math.max(1, ...allEnds);
    const list = element("div", { className: "waterfall" });
    observations.forEach((observation) => {
      const row = element("div", { className: "waterfall-row" });
      append(row, element("span", { text: observation.display_name, className: "waterfall-label" }));
      const track = element("div", { className: "timeline-track" });
      if (observation.start_offset_ms === null) append(track, element("span", { text: "Time unavailable", className: "unknown-time" }));
      else {
        const bar = element("button", { type: "button", className: `waterfall-bar ${observation.status}${observation.observation_id === selectedId ? " activity" : ""}`, title: `${observation.display_name} · ${text(observation.duration_ms, "running")}` });
        bar.style.left = `${Math.max(0, observation.start_offset_ms / span * 100)}%`;
        bar.style.width = `${Math.max(1, (observation.duration_ms || span * 0.03) / span * 100)}%`;
        bar.addEventListener("click", () => selectObservation(observation.observation_id));
        track.append(bar);
      }
      append(row, track); list.append(row);
    });
    return append(pane, list);
  }

  function selectObservation(observationId) {
    const query = new URLSearchParams(location.search);
    query.set("observation", observationId);
    if (!query.has("tab")) query.set("tab", "overview");
    navigate(`${location.pathname}?${query}`);
  }

  function relationBar(relations) {
    const bar = element("div", { className: "relation-bar" });
    if (!relations.length) return append(bar, element("span", { text: "No trace relations", className: "subtle" }));
    relations.forEach((relation) => {
      const line = element("span", { className: "relation" });
      append(line, element("span", { text: relation.relation }));
      if (relation.linked_trace_id) {
        const item = link(relation.linked_trace_id, `/traces/${encodeURIComponent(relation.linked_trace_id)}`);
        routeLink(item); line.append(item);
      }
      if (relation.dispatch_status) line.append(statusBadge(relation.dispatch_status));
      if (relation.completion_status) line.append(statusBadge(relation.completion_status));
      if (relation.detached) line.append(statusBadge("detached"));
      bar.append(line);
    });
    return bar;
  }

  function traceContext(trace) {
    const rail = element("section", { className: "context-rail activity" });
    const evidence = (label, value) => append(element("div"), element("span", { text: label, className: "data-label" }), descriptorView(value));
    append(rail,
      append(element("div", { className: "trace-id" }), code(trace.trace_id), statusBadge(trace.status), statusBadge(trace.evidence)),
      append(element("span", { className: "context-fact" }), element("span", { text: "Started", className: "data-label" }), localTime(trace.started_at)),
      element("span", { text: `Duration: ${trace.duration_ms === null ? "—" : `${trace.duration_ms} ms`}` }),
      element("span", { text: `Model / provider: ${text(trace.model)} / ${text(trace.provider)}` }),
      element("span", { text: `Tokens: ${text(trace.total_tokens)}` }),
      element("span", { text: `Branch: ${text(trace.branch_state)}` }),
      append(element("div", { className: "trace-level" }), element("strong", { text: "Trace evidence" }), evidence("Input", trace.input), evidence("Final output", trace.final_output), evidence("Metadata", trace.metadata)));
    return rail;
  }

  async function renderDetail(traceId) {
    const root = append(element("section"), pageHead("Trace detail", code(traceId)));
    app.replaceChildren(root);
    try {
      const trace = await request(`/api/v1/traces/${encodeURIComponent(traceId)}`);
      const query = new URLSearchParams(location.search);
      const rootAgent = trace.observations.find((item) => item.type === "agent" && item.parent_observation_id === null);
      const selectedId = trace.observations.some((item) => item.observation_id === query.get("observation")) ? query.get("observation") : (rootAgent || trace.observations[0])?.observation_id;
      const tab = tabs.has(query.get("tab")) ? query.get("tab") : "overview";
      const selected = trace.observations.find((item) => item.observation_id === selectedId) || null;
      root.append(traceContext(trace), relationBar(trace.relations || []));
      const grid = element("div", { className: "detail-grid" });
      append(grid, observationTree(trace.observations, selectedId), waterfall(trace.observations, selectedId), evidence(trace, selected, tab));
      root.append(grid);
      if (trace.status === "running" || trace.status === "waiting_for_input") {
        pollTimer = setTimeout(() => renderRoute(), 2000);
      }
    } catch (error) {
      if (error.name !== "AbortError") root.append(state("Request failed", error.message, true));
    }
  }

  async function renderSessions() {
    const root = append(element("section"), pageHead("Sessions", "Healthy primary sessions"));
    app.replaceChildren(root);
    try {
      const data = await request("/api/v1/sessions");
      root.append(diagnostics(data.diagnostics));
      if (!data.items.length) return root.append(state("No primary sessions", "No healthy primary sessions were recorded."));
      const table = element("table");
      const head = element("thead"); const header = element("tr");
      ["Session", "Latest user input", "Updated", "Messages", "Status"].forEach((label) => header.append(element("th", { text: label, scope: "col" })));
      head.append(header); const body = element("tbody");
      data.items.forEach((session) => {
        const row = element("tr"); row.tabIndex = 0;
        const open = () => navigate(`/sessions/${encodeURIComponent(session.session_id)}`);
        row.addEventListener("click", open); row.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
        append(row, append(element("td"), element("strong", { text: session.title }), code(session.session_id)), element("td", { text: text(session.latest_user_input) }), element("td").appendChild(localTime(session.updated_at)).parentElement, element("td", { text: text(session.message_count) }), append(element("td"), statusBadge(session.status)));
        body.append(row);
      });
      append(table, head, body); root.append(append(element("div", { className: "table-wrap" }), table));
    } catch (error) { if (error.name !== "AbortError") root.append(state("Request failed", error.message, true)); }
  }

  function branchTree(replay) {
    const pane = append(element("section", { className: "pane" }), append(element("div", { className: "pane-head" }), element("h2", { text: "Branches" })));
    const list = element("div", { className: "branch-list" });
    replay.branches.forEach((branch) => {
      const current = branch.branch_id === replay.selected_branch_id;
      const label = branch.active ? "Active" : "Historical · read-only";
      const item = append(element("button", { type: "button", className: `branch-button${current ? " activity" : ""}` }), code(branch.branch_id), element("span", { text: label, className: "subtle" }), element("small", { text: `Parent: ${text(branch.parent_branch_id)} · Base: ${text(branch.base_sequence)}` }));
      item.addEventListener("click", () => navigate(`/sessions/${encodeURIComponent(replay.session_id)}?branch=${encodeURIComponent(branch.branch_id)}`));
      list.append(item);
    });
    return append(pane, list);
  }

  function traceSummary(summary, traceId) {
    summary.replaceChildren(element("p", { text: "Select an explicitly linked trace to inspect its summary.", className: "subtle" }));
    if (!traceId) return;
    request(`/api/v1/traces/${encodeURIComponent(traceId)}`).then((trace) => {
      const open = link("Open trace", `/traces/${encodeURIComponent(traceId)}`); routeLink(open);
      summary.replaceChildren(element("h3", { text: "Selected trace" }), code(trace.trace_id), statusBadge(trace.status), element("span", { text: `Model: ${text(trace.model)}` }), element("span", { text: `Duration: ${trace.duration_ms === null ? "—" : `${trace.duration_ms} ms`}` }), statusBadge(trace.evidence), open);
    }).catch((error) => summary.replaceChildren(element("p", { text: error.message, className: "subtle" })));
  }

  async function renderReplay(sessionId) {
    const root = append(element("section"), pageHead("Session replay", code(sessionId)));
    app.replaceChildren(root);
    try {
      const branch = new URLSearchParams(location.search).get("branch");
      const replay = await request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/replay${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`);
      const summary = append(element("section", { className: "pane summary-pane" }), append(element("div", { className: "pane-head" }), element("h2", { text: "Selected trace summary" })));
      const conversation = append(element("section", { className: "pane" }), append(element("div", { className: "pane-head" }), element("h2", { text: "Conversation replay" }), element("p", { text: replay.selected_branch_id === replay.active_branch_id ? "Active branch" : "Historical · read-only", className: "subtle" })));
      const messages = element("div", { className: "conversation" });
      replay.items.forEach((item) => {
        const message = element("article", { className: "message" });
        append(message, append(element("div", { className: "message-header" }), element("strong", { text: item.role }), statusBadge(item.status), code(item.branch_id)), element("div", { text: item.content, className: "message-content" }));
        if (item.trace_id) message.append(button("Show summary", () => traceSummary(summary, item.trace_id)));
        item.linked_trace_ids.forEach((traceId) => message.append(button(`Show summary: ${traceId}`, () => traceSummary(summary, traceId))));
        messages.append(message);
      });
      if (!replay.items.length) messages.append(element("p", { text: "No conversation messages on this branch.", className: "subtle" }));
      conversation.append(messages);
      const raw = element("details"); append(raw, element("summary", { text: "Raw events" }), pre(replay.raw_events)); conversation.append(raw);
      append(root, element("div", { className: "replay-grid" }).appendChild(branchTree(replay)).parentElement);
      root.querySelector(".replay-grid").append(conversation, summary);
      traceSummary(summary, null);
    } catch (error) { if (error.name !== "AbortError") root.append(state("Request failed", error.message, true)); }
  }

  function renderRoute() {
    stopPending(); setNavigation(); app.replaceChildren();
    const path = decodeURIComponent(location.pathname);
    if (path === "/" || path === "/traces") return renderExplorer();
    const trace = path.match(/^\/traces\/([^/]+)$/);
    if (trace) return renderDetail(trace[1]);
    if (path === "/sessions") return renderSessions();
    const session = path.match(/^\/sessions\/([^/]+)$/);
    if (session) return renderReplay(session[1]);
    app.append(state("Page not found", "This route is not available.", true));
  }

  document.querySelectorAll("a[href]").forEach(routeLink);
  window.addEventListener("popstate", renderRoute);
  window.addEventListener("beforeunload", stopPending);
  renderRoute();
})();
