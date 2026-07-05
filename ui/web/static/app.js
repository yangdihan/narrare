const state = {
  sourcePath: "",
  projectId: "",
  selectedChunkId: "",
  currentJobId: "",
  pollTimer: null,
};

const el = (id) => document.getElementById(id);

function setStatus(id, message, isError = false) {
  const node = el(id);
  node.textContent = message;
  node.classList.toggle("error", isError);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || response.statusText);
  }
  return response.json();
}

async function loadSources() {
  const data = await api("/api/sources");
  const select = el("source-select");
  select.innerHTML = "";
  for (const source of data.sources) {
    const option = document.createElement("option");
    option.value = source.path;
    option.textContent = source.name;
    option.dataset.projectId = source.default_project_id;
    select.appendChild(option);
  }
  if (data.sources.length > 0) {
    select.value = data.sources[0].path;
    await loadSource(select.value);
  }
}

async function loadSource(path) {
  if (!path) return;
  const data = await api(`/api/source?path=${encodeURIComponent(path)}`);
  state.sourcePath = data.path;
  state.projectId = data.default_project_id;
  el("project-id").value = state.projectId;
  el("stage-project-id").value = state.projectId;
  el("source-text").textContent = data.text;
  setStatus("chunk-status", `${data.character_count} characters loaded`);
}

async function runChunking() {
  state.projectId = el("project-id").value.trim();
  if (!state.sourcePath || !state.projectId) return;
  setStatus("chunk-status", "chunking...");
  const data = await api("/api/chunk", {
    method: "POST",
    body: JSON.stringify({
      source_path: state.sourcePath,
      project_id: state.projectId,
    }),
  });
  renderChunks(data);
  el("stage-project-id").value = state.projectId;
  await loadProjectChunks();
}

async function loadProjectChunks() {
  const projectId = el("stage-project-id").value.trim() || state.projectId;
  if (!projectId) return;
  state.projectId = projectId;
  const data = await api(`/api/projects/${encodeURIComponent(projectId)}/chunks`);
  renderStageChunks(data);
  if (data.chunks.length > 0) {
    selectChunk(data.chunks[0].chunk_id, data.chunks[0].text);
  }
}

function renderChunks(data) {
  const success = data.validation_report?.exact_reconstruction_success;
  setStatus(
    "chunk-status",
    `${data.chunks.length} chunks written to ${data.project_root}`,
    !success
  );
  const list = el("chunk-output");
  list.innerHTML = "";
  for (const chunk of data.chunks) {
    list.appendChild(chunkCard(chunk));
  }
}

function renderStageChunks(data) {
  const list = el("stage-chunks");
  list.innerHTML = "";
  for (const chunk of data.chunks) {
    const button = document.createElement("button");
    button.className = "chunk-row";
    button.type = "button";
    button.textContent = `${chunk.chunk_id} · ${chunk.estimated_tokens} tokens`;
    button.addEventListener("click", () => selectChunk(chunk.chunk_id, chunk.text));
    list.appendChild(button);
  }
}

function selectChunk(chunkId, text) {
  state.selectedChunkId = chunkId;
  el("selected-chunk-label").textContent = chunkId;
  el("stage-chunk-text").textContent = text;
  setStatus("stage-status", "ready");
}

function chunkCard(chunk) {
  const card = document.createElement("article");
  card.className = "chunk-card";
  const title = document.createElement("h3");
  title.textContent = `${chunk.chunk_id} · ${chunk.source_span.start}-${chunk.source_span.end}`;
  const meta = document.createElement("p");
  meta.textContent = `${chunk.character_count} characters · ${chunk.estimated_tokens} estimated tokens`;
  const text = document.createElement("pre");
  text.textContent = chunk.text;
  card.append(title, meta, text);
  return card;
}

async function startStage2() {
  const projectId = el("stage-project-id").value.trim() || state.projectId;
  if (!projectId || !state.selectedChunkId) return;
  setStatus("stage-status", "starting...");
  const job = await api("/api/stage2/jobs", {
    method: "POST",
    body: JSON.stringify({
      project_id: projectId,
      chunk_id: state.selectedChunkId,
    }),
  });
  state.currentJobId = job.job_id;
  pollJob();
  state.pollTimer = setInterval(pollJob, 1000);
}

async function pollJob() {
  if (!state.currentJobId) return;
  const job = await api(`/api/stage2/jobs/${state.currentJobId}`);
  const total = job.total_windows || 0;
  const processed = job.processed_windows || 0;
  el("stage-progress").max = total || 1;
  el("stage-progress").value = processed;
  setStatus(
    "stage-status",
    `${job.status} · ${processed}/${total || "?"} windows`,
    job.status === "failed"
  );
  if (job.status === "complete" || job.status === "failed") {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    await loadScript();
  }
}

async function loadScript() {
  const projectId = el("stage-project-id").value.trim() || state.projectId;
  if (!projectId || !state.selectedChunkId) return;
  const data = await api(
    `/api/projects/${encodeURIComponent(projectId)}/script/${encodeURIComponent(state.selectedChunkId)}`
  );
  renderScript(data);
}

function renderScript(data) {
  const pane = el("script-output");
  pane.innerHTML = "";
  const report = data.validation_report;
  if (report) {
    setStatus(
      "script-status",
      report.exact_reconstruction_success
        ? "exact reconstruction passed"
        : `exact reconstruction failed: ${report.errors.join("; ")}`,
      !report.exact_reconstruction_success
    );
  }
  for (const segment of data.segments) {
    const block = document.createElement("article");
    block.className = `segment ${segment.validation_status}`;
    const speaker = document.createElement("div");
    speaker.className = "speaker";
    speaker.textContent = `${segment.segment_id} · ${segment.speaker}`;
    const text = document.createElement("pre");
    text.textContent = segment.text;
    block.append(speaker, text);
    if (segment.validation_errors.length > 0) {
      const errors = document.createElement("p");
      errors.className = "segment-errors";
      errors.textContent = segment.validation_errors.join("; ");
      block.appendChild(errors);
    }
    pane.appendChild(block);
  }
}

function switchTab(tabName) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `${tabName}-panel`);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  el("source-select").addEventListener("change", (event) => {
    loadSource(event.target.value).catch((error) =>
      setStatus("chunk-status", error.message, true)
    );
  });
  el("chunk-button").addEventListener("click", () => {
    runChunking().catch((error) => setStatus("chunk-status", error.message, true));
  });
  el("load-project-button").addEventListener("click", () => {
    loadProjectChunks().catch((error) =>
      setStatus("stage-status", error.message, true)
    );
  });
  el("stage2-button").addEventListener("click", () => {
    startStage2().catch((error) => setStatus("stage-status", error.message, true));
  });
  loadSources().catch((error) => setStatus("chunk-status", error.message, true));
});

