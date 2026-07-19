const VIEW_OPTIONS = [
  ["original_text", "Original Text"],
  ["chunks", "Chunks"],
  ["scene_summary", "Chunk Scene Summary"],
  ["character_summary", "Character Summary"],
  ["scripts", "Scripts"],
  ["voice_assignment", "Voice Assignment"],
];

const DEFAULT_PROJECT_ID = "bicentennial_man";

const state = {
  sourcePath: "",
  projectId: DEFAULT_PROJECT_ID,
  chunkSelection: "all",
  currentJobId: "",
  currentJobOwner: "",
  pollTimer: null,
  panelViews: {
    left: "original_text",
    right: "scripts",
  },
  voiceAssignments: {},
};

const el = (id) => document.getElementById(id);

function setStatus(id, message, isError = false) {
  const node = el(id);
  if (!node) return;
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

function populateViewSelects() {
  for (const panel of ["left", "right"]) {
    const select = el(`${panel}-view-select`);
    select.innerHTML = "";
    for (const [value, label] of VIEW_OPTIONS) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }
    select.value = state.panelViews[panel];
  }
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
  } else {
    setStatus("global-status", "No .txt files found in data/raw", true);
    await refreshPanels();
  }
}

async function loadSource(path) {
  if (!path) return;
  const data = await api(`/api/source?path=${encodeURIComponent(path)}`);
  state.sourcePath = data.path;
  state.projectId = el("project-id").value.trim() || state.projectId || DEFAULT_PROJECT_ID;
  state.chunkSelection = "all";
  el("project-id").value = state.projectId;
  setStatus("global-status", `${data.character_count} source characters loaded`);
  await refreshPanels();
}

async function runChunking(ownerPanel) {
  state.projectId = el("project-id").value.trim();
  if (!state.sourcePath || !state.projectId) return;
  setPanelJobStatus(ownerPanel, "chunking...", 0, 1, false);
  setStatus("global-status", "chunking...");
  const data = await api("/api/chunk", {
    method: "POST",
    body: JSON.stringify({
      source_path: state.sourcePath,
      project_id: state.projectId,
    }),
  });
  state.chunkSelection = "all";
  setPanelJobStatus(ownerPanel, `${data.chunks.length} chunks written`, 1, 1, false);
  setStatus("global-status", `${data.chunks.length} chunks written`);
  await refreshPanels();
}

async function startStage1(ownerPanel) {
  state.projectId = el("project-id").value.trim();
  if (!state.projectId) return;
  setPanelJobStatus(ownerPanel, "starting Stage 1 overview...", 0, 1, false);
  const job = await api("/api/stage1/jobs", {
    method: "POST",
    body: JSON.stringify({
      project_id: state.projectId,
    }),
  });
  startPolling(job, ownerPanel);
}

async function startStage2(ownerPanel, selection) {
  state.projectId = el("project-id").value.trim();
  if (!state.projectId || !selection) return;
  setPanelJobStatus(ownerPanel, `starting Stage 2 for ${selection}...`, 0, 1, false);
  const job = await api("/api/stage2/jobs", {
    method: "POST",
    body: JSON.stringify({
      project_id: state.projectId,
      selection,
    }),
  });
  startPolling(job, ownerPanel);
}

function startPolling(job, ownerPanel) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.currentJobId = job.job_id;
  state.currentJobOwner = ownerPanel;
  renderJobStatus(job);
  state.pollTimer = setInterval(pollJob, 1000);
}

async function pollJob() {
  if (!state.currentJobId) return;
  const job = await api(`/api/jobs/${state.currentJobId}`);
  renderJobStatus(job);
  if (job.status === "complete" || job.status === "failed") {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    state.currentJobId = "";
    await refreshPanels();
  }
}

function renderJobStatus(job) {
  const total = job.total_segments || job.total_chunks || 0;
  const completed = job.completed_segments || job.completed_chunks || 0;
  const currentId = job.current_segment_id || job.current_chunk_id;
  const current = currentId ? ` · ${currentId}` : "";
  const speaker = job.current_speaker ? ` · ${job.current_speaker}` : "";
  const message = `${job.phase} ${job.status}${current}${speaker} · ${completed}/${total || "?"}`;
  setStatus("global-status", message, job.status === "failed");
  setPanelJobStatus(state.currentJobOwner, message, completed, total || 1, job.status === "failed");
}

function setPanelJobStatus(panel, message, value, max, isError) {
  if (!panel) return;
  const progress = el(`${panel}-job-progress`);
  if (progress) {
    progress.max = max || 1;
    progress.value = value || 0;
  }
  setStatus(`${panel}-job-status`, message, isError);
}

async function refreshPanels() {
  await Promise.all([renderPanel("left"), renderPanel("right")]);
}

async function renderPanel(panel) {
  const viewType = state.panelViews[panel];
  const target = el(`${panel}-panel`);
  setStatus(`${panel}-status`, "loading...");
  target.innerHTML = "";
  const params = new URLSearchParams();
  if (state.sourcePath) params.set("source_path", state.sourcePath);
  const url = `/api/projects/${encodeURIComponent(state.projectId || "project")}/views/${viewType}?${params}`;

  try {
    const payload = await api(url);
    target.replaceChildren(renderView(payload, panel));
    setStatus(`${panel}-status`, payload.available ? "" : "empty", !payload.available);
  } catch (error) {
    target.replaceChildren(emptyState(error.message));
    setStatus(`${panel}-status`, "error", true);
  }
}

function renderView(payload, panel) {
  if (!payload.available) return emptyState(payload.message);
  if (payload.view_type === "original_text") return renderOriginalText(payload, panel);
  if (payload.view_type === "chunks") return renderChunks(payload, panel);
  if (payload.view_type === "scene_summary") return renderSceneSummary(payload);
  if (payload.view_type === "character_summary") return renderCharacters(payload);
  if (payload.view_type === "scripts") return renderScripts(payload);
  if (payload.view_type === "voice_assignment") return renderVoiceAssignment(payload, panel);
  return emptyState("Unsupported view type.");
}

function renderOriginalText(payload, panel) {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(originalActions(panel));
  fragment.appendChild(metaBar(`${payload.source.name} · ${payload.source.character_count} characters`));
  const pre = document.createElement("pre");
  pre.className = "text-block";
  pre.textContent = payload.source.text;
  fragment.appendChild(pre);
  return fragment;
}

function originalActions(panel) {
  const actions = panelActions(panel);
  const chunkButton = document.createElement("button");
  chunkButton.className = "primary";
  chunkButton.type = "button";
  chunkButton.textContent = "chunk it";
  chunkButton.addEventListener("click", () => {
    runChunking(panel).catch((error) =>
      setPanelJobStatus(panel, error.message, 0, 1, true)
    );
  });

  const overviewButton = document.createElement("button");
  overviewButton.type = "button";
  overviewButton.textContent = "overview chunks";
  overviewButton.addEventListener("click", () => {
    startStage1(panel).catch((error) =>
      setPanelJobStatus(panel, error.message, 0, 1, true)
    );
  });

  actions.controls.append(chunkButton, overviewButton);
  return actions.wrapper;
}

function renderChunks(payload, panel) {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(chunkActions(payload, panel));
  const success = payload.validation_report?.exact_reconstruction_success;
  const status = success === true ? "passed" : success === false ? "failed" : "unknown";
  fragment.appendChild(metaBar(`${payload.chunks.length} chunks · validation ${status}`));
  for (const chunk of payload.chunks) {
    const card = cardNode("chunk-card");
    const title = document.createElement("h3");
    title.textContent = `${chunk.chunk_id} · ${chunk.source_span.start}-${chunk.source_span.end}`;
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = `${chunk.character_count} characters · ${chunk.estimated_tokens} estimated tokens`;
    const pre = document.createElement("pre");
    pre.textContent = chunk.text;
    card.append(title, meta, pre);
    fragment.appendChild(card);
  }
  return fragment;
}

function chunkActions(payload, panel) {
  const actions = panelActions(panel);
  const label = document.createElement("label");
  label.textContent = "Chunk";
  const select = document.createElement("select");
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "all";
  select.appendChild(all);
  for (const chunk of payload.chunks) {
    const option = document.createElement("option");
    option.value = chunk.chunk_id;
    option.textContent = `${chunk.chunk_id} · ${chunk.estimated_tokens} tokens`;
    select.appendChild(option);
  }
  if (!payload.chunks.some((chunk) => chunk.chunk_id === state.chunkSelection)) {
    state.chunkSelection = "all";
  }
  select.value = state.chunkSelection;
  select.addEventListener("change", (event) => {
    state.chunkSelection = event.target.value;
  });
  label.appendChild(select);

  const feedButton = document.createElement("button");
  feedButton.className = "primary";
  feedButton.type = "button";
  feedButton.textContent = "feed to LLM";
  feedButton.addEventListener("click", () => {
    startStage2(panel, select.value).catch((error) =>
      setPanelJobStatus(panel, error.message, 0, 1, true)
    );
  });

  actions.controls.append(label, feedButton);
  return actions.wrapper;
}

function panelActions(panel) {
  const wrapper = document.createElement("div");
  wrapper.className = "panel-actions";
  const controls = document.createElement("div");
  controls.className = "panel-action-controls";
  const progress = document.createElement("progress");
  progress.id = `${panel}-job-progress`;
  progress.value = 0;
  progress.max = 1;
  const status = document.createElement("span");
  status.id = `${panel}-job-status`;
  status.className = "status panel-job-status";
  wrapper.append(controls, progress, status);
  return { wrapper, controls };
}

function renderSceneSummary(payload) {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(metaBar(`${payload.sections.length} ordered context artifacts`));
  for (const section of payload.sections) {
    const card = cardNode("timeline-card");
    const heading = document.createElement("h3");
    heading.textContent = section.chunk_id;
    card.appendChild(heading);
    card.appendChild(timelineText("Scene Summary", section.scene_summary));
    card.appendChild(timelineList("Active Characters", section.active_characters));
    card.appendChild(timelineList("Important Context", section.important_context));
    card.appendChild(timelineList("Aliases Observed", section.aliases_observed, (item) =>
      `${item.text} · ${item.reference_type} · ${item.likely_character_id || "unresolved"}`
    ));
    card.appendChild(timelineList("Unresolved Pronouns", section.unresolved_pronouns, (item) =>
      `${item.text} · candidates: ${(item.candidates || []).join(", ")}`
    ));
    fragment.appendChild(card);
  }
  return fragment;
}

function renderCharacters(payload) {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(metaBar(`${payload.characters.length} character records`));
  for (const character of payload.characters) {
    const card = cardNode("character-card");
    const title = document.createElement("h3");
    title.textContent = `${character.character_id} · ${character.canonical_name}`;
    card.appendChild(title);
    card.appendChild(fieldLine("Stable aliases", (character.stable_aliases || []).join(", ") || "none"));
    card.appendChild(fieldLine("Persona", character.persona_summary || "none"));
    card.appendChild(fieldLine("Speaking style", character.speaking_style || "none"));
    card.appendChild(fieldLine("Age impression", character.age_impression || "none"));
    card.appendChild(fieldLine("Voice notes", (character.voice_variant_notes || []).join("; ") || "none"));
    fragment.appendChild(card);
  }
  return fragment;
}

function renderScripts(payload) {
  const fragment = document.createDocumentFragment();
  const report = payload.validation_report;
  const status = report?.exact_reconstruction_success ? "validation passed" : "validation pending/failed";
  fragment.appendChild(metaBar(`${payload.script_source} · ${payload.segments.length} segments · ${status}`));
  for (const segment of payload.segments) {
    const block = cardNode(`segment ${segment.validation_status}`);
    const speaker = document.createElement("div");
    speaker.className = "speaker";
    const chunkLabel = segment.chunk_id ? `${segment.chunk_id} · ` : "";
    speaker.textContent = `${chunkLabel}${segment.segment_id} · ${segment.speaker}`;
    const text = document.createElement("pre");
    text.textContent = segment.text;
    block.append(speaker, text);
    if (segment.validation_errors.length > 0) {
      const errors = document.createElement("p");
      errors.className = "segment-errors";
      errors.textContent = segment.validation_errors.join("; ");
      block.appendChild(errors);
    }
    fragment.appendChild(block);
  }
  return fragment;
}

function renderVoiceAssignment(payload, panel) {
  const fragment = document.createDocumentFragment();
  state.voiceAssignments = {};
  for (const assignment of payload.assignments) {
    if (assignment.voice_profile_id) {
      state.voiceAssignments[assignment.speaker] = assignment.voice_profile_id;
    }
  }
  fragment.appendChild(
    metaBar(
      `${payload.assignments.length} speakers · ${payload.voice_profiles.length} voices · ${payload.script_artifact_path}`
    )
  );
  if (!payload.tts_generation_enabled) {
    const warning = cardNode("info-card warning-card");
    const title = document.createElement("h3");
    title.textContent = "TTS disabled";
    const body = document.createElement("p");
    body.textContent = payload.tts_generation_status || "CLI smoke pending";
    warning.append(title, body);
    fragment.appendChild(warning);
  }
  if (payload.missing_voice_profile_ids.length > 0) {
    const warning = cardNode("info-card warning-card");
    const title = document.createElement("h3");
    title.textContent = "Missing assigned voices";
    const body = document.createElement("p");
    body.textContent = payload.missing_voice_profile_ids.join(", ");
    warning.append(title, body);
    fragment.appendChild(warning);
  }
  for (const assignment of payload.assignments) {
    fragment.appendChild(voiceAssignmentCard(payload, assignment, panel));
  }
  fragment.appendChild(voiceAssignmentFooter(payload, panel));
  return fragment;
}

function voiceAssignmentCard(payload, assignment, panel) {
  const card = cardNode("voice-card");
  card.dataset.speaker = assignment.speaker;

  const header = document.createElement("div");
  header.className = "voice-card-header";
  const title = document.createElement("h3");
  title.textContent = assignment.speaker;
  const status = document.createElement("span");
  status.className = "voice-status";
  status.textContent = assignment.confirmed ? "assigned" : "unassigned";
  header.append(title, status);

  const summary = document.createElement("p");
  summary.className = "muted";
  summary.textContent = assignment.summary || "No character summary.";

  const quoteLabel = document.createElement("p");
  quoteLabel.className = "quote-label";
  quoteLabel.textContent = "代表性的台词";
  const quote = document.createElement("pre");
  quote.className = "representative-line";
  quote.textContent = assignment.representative_text || "No representative line.";

  const controls = document.createElement("div");
  controls.className = "voice-controls";
  const label = document.createElement("label");
  label.textContent = "Voice";
  const select = document.createElement("select");
  select.appendChild(new Option("Select voice", ""));
  for (const profile of payload.voice_profiles) {
    const option = new Option(profile.display_name, profile.profile_id);
    option.disabled = !profile.available;
    select.appendChild(option);
  }
  select.value = assignment.voice_profile_id || "";
  select.disabled = !payload.tts_generation_enabled;
  label.appendChild(select);

  const audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "none";
  if (assignment.sample_url) audio.src = assignment.sample_url;

  const rowStatus = document.createElement("span");
  rowStatus.className = "voice-row-status status";
  if (!payload.tts_generation_enabled) {
    rowStatus.textContent = "CLI smoke pending";
  }

  select.addEventListener("change", async (event) => {
    if (!payload.tts_generation_enabled) return;
    const profileId = event.target.value;
    state.voiceAssignments[assignment.speaker] = profileId;
    if (!profileId) {
      status.textContent = "unassigned";
      audio.removeAttribute("src");
      rowStatus.textContent = "";
      return;
    }
    status.textContent = "previewing";
    rowStatus.textContent = "generating preview...";
    try {
      const result = await api(
        `/api/projects/${encodeURIComponent(payload.project_id)}/voice-samples`,
        {
          method: "POST",
          body: JSON.stringify({
            speaker: assignment.speaker,
            voice_profile_id: profileId,
          }),
        }
      );
      audio.src = `${result.sample_url}?t=${Date.now()}`;
      audio.load();
      status.textContent = "assigned";
      rowStatus.textContent = "preview ready";
    } catch (error) {
      status.textContent = "preview failed";
      rowStatus.textContent = error.message;
      rowStatus.classList.add("error");
    }
  });

  controls.append(label, audio, rowStatus);
  card.append(header, summary, quoteLabel, quote, controls);
  return card;
}

function voiceAssignmentFooter(payload, panel) {
  const footer = document.createElement("div");
  footer.className = "voice-assignment-footer";
  const progress = document.createElement("progress");
  progress.id = `${panel}-job-progress`;
  progress.value = 0;
  progress.max = 1;
  const status = document.createElement("span");
  status.id = `${panel}-job-status`;
  status.className = "status panel-job-status";
  const button = document.createElement("button");
  button.className = "primary";
  button.type = "button";
  button.textContent = "confirm voice assignment & generate";
  button.disabled = !payload.tts_generation_enabled;
  if (!payload.tts_generation_enabled) {
    status.textContent = payload.tts_generation_status || "CLI smoke pending";
  }
  button.addEventListener("click", async () => {
    if (!payload.tts_generation_enabled) return;
    const missing = payload.assignments
      .filter((assignment) => !state.voiceAssignments[assignment.speaker])
      .map((assignment) => assignment.speaker);
    if (missing.length > 0) {
      status.textContent = `missing voices: ${missing.join(", ")}`;
      status.classList.add("error");
      return;
    }
    status.classList.remove("error");
    status.textContent = "starting audio generation...";
    try {
      const job = await api(
        `/api/projects/${encodeURIComponent(payload.project_id)}/audio/jobs`,
        {
          method: "POST",
          body: JSON.stringify({
            assignments: state.voiceAssignments,
            only_missing: true,
          }),
        }
      );
      startPolling(job, panel);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
    }
  });
  footer.append(button, progress, status);
  return footer;
}

function emptyState(message) {
  const node = document.createElement("div");
  node.className = "empty-state";
  node.textContent = message;
  return node;
}

function metaBar(text) {
  const node = document.createElement("div");
  node.className = "meta-bar";
  node.textContent = text;
  return node;
}

function cardNode(className) {
  const node = document.createElement("article");
  node.className = className;
  return node;
}

function sectionBlock(title, text) {
  const section = cardNode("info-card");
  const heading = document.createElement("h3");
  heading.textContent = title;
  const body = document.createElement("p");
  body.textContent = text || "none";
  section.append(heading, body);
  return section;
}

function timelineText(title, text) {
  const section = document.createElement("section");
  section.className = "timeline-section";
  const heading = document.createElement("h4");
  heading.textContent = title;
  const body = document.createElement("p");
  body.textContent = text || "none";
  section.append(heading, body);
  return section;
}

function timelineList(title, values = [], formatter = (value) => value) {
  const section = document.createElement("section");
  section.className = "timeline-section";
  const heading = document.createElement("h4");
  heading.textContent = title;
  const list = document.createElement("ul");
  for (const value of values) {
    const item = document.createElement("li");
    item.textContent = formatter(value);
    list.appendChild(item);
  }
  if (values.length === 0) {
    const item = document.createElement("li");
    item.textContent = "none";
    list.appendChild(item);
  }
  section.append(heading, list);
  return section;
}

function listBlock(title, values = []) {
  return objectListBlock(title, values, (value) => value);
}

function objectListBlock(title, values = [], formatter) {
  const section = cardNode("info-card");
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("ul");
  for (const value of values) {
    const item = document.createElement("li");
    item.textContent = formatter(value);
    list.appendChild(item);
  }
  if (values.length === 0) {
    const item = document.createElement("li");
    item.textContent = "none";
    list.appendChild(item);
  }
  section.append(heading, list);
  return section;
}

function fieldLine(label, value) {
  const line = document.createElement("p");
  line.className = "field-line";
  line.textContent = `${label}: ${value}`;
  return line;
}

document.addEventListener("DOMContentLoaded", () => {
  populateViewSelects();
  for (const panel of ["left", "right"]) {
    el(`${panel}-view-select`).addEventListener("change", async (event) => {
      state.panelViews[panel] = event.target.value;
      await renderPanel(panel);
    });
  }
  el("source-select").addEventListener("change", (event) => {
    loadSource(event.target.value).catch((error) =>
      setStatus("global-status", error.message, true)
    );
  });
  el("project-id").addEventListener("change", async (event) => {
    state.projectId = event.target.value.trim();
    state.chunkSelection = "all";
    await refreshPanels();
  });
  el("project-id").addEventListener("input", (event) => {
    state.projectId = event.target.value.trim();
  });
  loadSources().catch((error) => setStatus("global-status", error.message, true));
});
