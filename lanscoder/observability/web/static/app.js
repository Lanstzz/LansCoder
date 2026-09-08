(() => {
  const traces = document.querySelector("#traces");
  const detail = document.querySelector("#detail-content");
  const title = document.querySelector("#detail-title");
  const status = document.querySelector("#status");
  const sessions = document.querySelector("#sessions");
  const replayContent = document.querySelector("#replay-content");
  let pollTimer = null;

  const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char]));
  const loadTraces = async () => {
    const query = status.value ? `?status=${encodeURIComponent(status.value)}` : "";
    const data = await fetch(`/api/v1/traces${query}`).then((response) => response.json());
    traces.innerHTML = data.items.map((trace) => `<button class="trace" data-id="${esc(trace.trace_id)}"><span>${esc(trace.status)}</span><strong>${esc(trace.model || trace.trace_id)}</strong><small>${esc(trace.started_at || "")}</small></button>`).join("") || "<p>No traces.</p>";
    traces.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => loadDetail(button.dataset.id)));
  };
  const loadReplay = async (sessionId) => {
    const replay = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/replay`).then((response) => response.json());
    replayContent.innerHTML = `<div class="replay-facts"><b>${esc(replay.session_id)}</b><span>active: ${esc(replay.active_branch_id)}</span><span>root: ${esc(replay.root_branch_id)}</span></div><div class="branches">${replay.branches.map((branch) => `<article class="branch ${branch.active ? "active" : "historical"}"><h3>${esc(branch.branch_id)} ${branch.active ? "(active)" : "(historical)"}</h3><small>parent ${esc(branch.parent_branch_id || "none")} · base ${esc(branch.base_sequence ?? "-")}</small><p>${branch.events.length} journal events</p></article>`).join("")}</div><h3>Linked traces</h3><div class="linked-traces">${replay.linked_trace_ids.map((traceId) => `<a href="/traces/${encodeURIComponent(traceId)}">${esc(traceId)}</a>`).join("") || "<span>none</span>"}</div>`;
  };
  const loadSessions = async () => {
    const data = await fetch("/api/v1/sessions").then((response) => response.json());
    sessions.innerHTML = data.items.map((session) => `<button class="session" data-id="${esc(session.session_id)}"><strong>${esc(session.title || session.session_id)}</strong><small>${esc(session.updated_at || "")}</small></button>`).join("") || "<p>No primary sessions.</p>";
    sessions.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => loadReplay(button.dataset.id)));
  };
  const loadDetail = async (traceId) => {
    if (pollTimer) clearInterval(pollTimer);
    const render = async () => {
      const trace = await fetch(`/api/v1/traces/${encodeURIComponent(traceId)}`).then((response) => response.json());
      title.textContent = `${trace.trace_id} · ${trace.status}`;
      detail.innerHTML = `<div class="facts"><b>${esc(trace.model || "model unavailable")}</b><span>${esc(trace.provider || "provider unavailable")}</span><span>${trace.observation_count} observations</span><span>${trace.incomplete ? "incomplete evidence" : "complete evidence"}</span></div><h3>Timeline</h3><ol>${trace.timeline.map((item) => `<li><b>${esc(item.observation_type)}</b> ${esc(item.outcome || "running")} <small>${esc(item.duration_ms || "")} ms</small></li>`).join("")}</ol><h3>Evidence</h3><pre>${esc(JSON.stringify({input: trace.input, output: trace.final_output, trace_metadata: trace.trace_metadata, evidence_payloads: trace.evidence_payloads, parameters: trace.parameters, usage_details: trace.usage_details}, null, 2))}</pre>`;
      if (trace.status === "running" || trace.status === "waiting_for_input") pollTimer = setTimeout(render, 2000);
    };
    await render();
  };
  status.addEventListener("change", loadTraces);
  loadTraces();
  loadSessions();
  const initialTrace = window.location.pathname.match(/^\/traces\/([^/]+)$/);
  if (initialTrace) loadDetail(decodeURIComponent(initialTrace[1]));
})();
