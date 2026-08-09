const VIEW_OPTIONS = [
  ["original_text", "Original Text"],
  ["characters", "Characters"],
  ["scripts", "Scripts"],
];

const DEFAULT_PROJECT_ID = "bicentennial_man";
const PROJECT_STORAGE_KEY = "narrare.projectId";

function storedProjectId() {
  try {
    return window.localStorage.getItem(PROJECT_STORAGE_KEY) || "";
  } catch (_error) {
    return "";
  }
}

function rememberProjectId(projectId) {
  if (!projectId) return;
  try {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, projectId);
  } catch (_error) {
    // The app still works when browser storage is unavailable.
  }
}

const state = {
  sourcePath: "",
  projectId: storedProjectId() || DEFAULT_PROJECT_ID,
  chunkSelection: "all",
  currentJobId: "",
  currentJobOwner: "",
  pollTimer: null,
  playbackQueue: [],
  playbackIndex: -1,
  panelViews: {
    left: "original_text",
    right: "scripts",
  },
  voiceAssignments: {},
  characterEdits: {
    additions: [],
    updates: {},
    removals: [],
    merges: [],
    scriptSpeakerMerges: [],
    voiceProfileByCharacterId: {},
    systemVoiceAssignments: {},
  },
  scriptSpeakerEdits: {
    updates: {},
    inserts: [],
  },
  scriptSpeakerFilters: {
    left: "",
    right: "",
  },
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
    await resumeActiveTtsJob();
  }
}

async function loadSource(path) {
  if (!path) return;
  const data = await api(`/api/source?path=${encodeURIComponent(path)}`);
  state.sourcePath = data.path;
  state.projectId = el("project-id").value.trim() || state.projectId || DEFAULT_PROJECT_ID;
  state.chunkSelection = "all";
  clearCharacterEdits();
  clearScriptSpeakerEdits();
  el("project-id").value = state.projectId;
  setStatus("global-status", `${data.character_count} source characters loaded`);
  await refreshPanels();
  await resumeActiveTtsJob();
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

async function startStage3(ownerPanel) {
  state.projectId = el("project-id").value.trim();
  if (!state.projectId) return;
  setPanelJobStatus(ownerPanel, "starting Stage 3 character unification...", 0, 1, false);
  const job = await api("/api/stage3/jobs", {
    method: "POST",
    body: JSON.stringify({
      project_id: state.projectId,
    }),
  });
  startPolling(job, ownerPanel);
}

function startPolling(job, ownerPanel) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.currentJobId = job.job_id;
  state.currentJobOwner = ownerPanel;
  syncGlobalAudioButtons();
  renderJobStatus(job);
  state.pollTimer = setInterval(pollJob, 1000);
}

async function resumeActiveTtsJob() {
  if (!state.projectId || state.currentJobId) return;
  const payload = await api(
    `/api/projects/${encodeURIComponent(state.projectId)}/audio/jobs/active`
  );
  if (!payload.job) return;
  const ownerPanel = ["left", "right"].find(
    (panel) => state.panelViews[panel] === "characters"
  ) || "left";
  startPolling(payload.job, ownerPanel);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.currentJobId = "";
  state.currentJobOwner = "";
  syncGlobalAudioButtons();
  const progress = el("global-job-progress");
  if (progress) progress.hidden = true;
}

async function pollJob() {
  if (!state.currentJobId) return;
  const job = await api(`/api/jobs/${state.currentJobId}`);
  renderJobStatus(job);
  if (job.status === "complete" || job.status === "failed") {
    stopPolling();
    await refreshPanels();
  }
}

function renderJobStatus(job) {
  const total = job.total_segments || job.total_chunks || 0;
  const completed = job.completed_segments || job.completed_chunks || 0;
  const currentId = job.current_segment_id || job.current_chunk_id;
  const current = currentId ? ` · ${currentId}` : "";
  const speaker = job.current_speaker ? ` · ${job.current_speaker}` : "";
  const errors = job.errors?.length ? ` · ${job.errors.join("; ")}` : "";
  const message = `${job.phase} ${job.status}${current}${speaker} · ${completed}/${total || "?"}${errors}`;
  const globalProgress = el("global-job-progress");
  if (globalProgress) {
    globalProgress.hidden = false;
    globalProgress.max = total || 1;
    globalProgress.value = completed;
  }
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

function activeJobOwnerPanel() {
  return ["left", "right"].find(
    (panel) => state.panelViews[panel] === "characters"
  ) || "";
}

function syncGlobalAudioButtons() {
  const jobRunning = Boolean(state.currentJobId);
  const playbackRunning = state.playbackQueue.length > 0;
  const generate = el("generate-all-button");
  const play = el("play-all-button");
  if (generate) generate.disabled = jobRunning || playbackRunning;
  if (play) {
    play.disabled = jobRunning;
    play.textContent = playbackRunning ? "stop playback" : "play all";
  }
}

async function startGlobalAudioGeneration() {
  if (!state.projectId || state.currentJobId) return;
  if (hasCharacterCurationEdits()) {
    setStatus(
      "global-status",
      "Apply or discard pending character edits before generating audio.",
      true
    );
    return;
  }

  const payload = await api(
    `/api/projects/${encodeURIComponent(state.projectId)}/views/characters`
  );
  if (!payload.tts_generation_enabled) {
    throw new Error(payload.tts_generation_status || "TTS generation is disabled");
  }
  const activeAssignments = (payload.assignments || []).filter(
    (assignment) => assignment.representative_segment_id
  );
  const missing = activeAssignments
    .filter((assignment) => !assignment.voice_profile_id)
    .map((assignment) => assignment.speaker);
  if (missing.length) {
    throw new Error(`missing voices: ${missing.join(", ")}`);
  }
  const assignments = Object.fromEntries(
    activeAssignments.map((assignment) => [
      assignment.speaker,
      assignment.voice_profile_id,
    ])
  );
  setStatus("global-status", "starting audio generation...");
  const job = await api(
    `/api/projects/${encodeURIComponent(state.projectId)}/audio/jobs`,
    {
      method: "POST",
      body: JSON.stringify({ assignments, only_missing: true }),
    }
  );
  startPolling(job, activeJobOwnerPanel());
}

async function startGlobalPlayback() {
  if (state.playbackQueue.length) {
    stopGlobalPlayback("playback stopped");
    return;
  }
  if (!state.projectId || state.currentJobId) return;

  const playlist = await api(
    `/api/projects/${encodeURIComponent(state.projectId)}/audio/playlist`
  );
  if (!playlist.ready) {
    const preview = playlist.missing_segment_ids.slice(0, 5).join(", ");
    const more = playlist.missing_segment_ids.length > 5 ? ", ..." : "";
    throw new Error(
      `${playlist.missing_segment_ids.length} scripts have no selected current audio: ${preview}${more}`
    );
  }
  if (!playlist.items.length) {
    throw new Error("No script audio is available.");
  }

  state.playbackQueue = playlist.items;
  state.playbackIndex = -1;
  syncGlobalAudioButtons();
  await playNextGlobalAudio();
}

async function playNextGlobalAudio() {
  state.playbackIndex += 1;
  if (state.playbackIndex >= state.playbackQueue.length) {
    stopGlobalPlayback("playback complete");
    return;
  }

  const item = state.playbackQueue[state.playbackIndex];
  const player = el("global-audio-player");
  player.hidden = false;
  player.src = item.audio_url;
  player.load();
  setStatus(
    "global-status",
    `playing ${state.playbackIndex + 1}/${state.playbackQueue.length} · ${item.segment_id} · ${item.speaker}`
  );
  try {
    await player.play();
  } catch (error) {
    stopGlobalPlayback(`playback failed: ${error.message}`, true);
  }
}

function stopGlobalPlayback(message, isError = false) {
  const player = el("global-audio-player");
  if (player) {
    player.pause();
    player.removeAttribute("src");
    player.load();
    player.hidden = true;
  }
  state.playbackQueue = [];
  state.playbackIndex = -1;
  syncGlobalAudioButtons();
  if (message) setStatus("global-status", message, isError);
}

async function refreshPanels() {
  await Promise.all([renderPanel("left"), renderPanel("right")]);
}

async function renderPanel(panel) {
  const viewType = state.panelViews[panel];
  const target = el(`${panel}-panel`);
  const scrollTop = target.scrollTop;
  const scrollLeft = target.scrollLeft;
  setStatus(`${panel}-status`, "loading...");
  const params = new URLSearchParams();
  if (state.sourcePath) params.set("source_path", state.sourcePath);
  const url = `/api/projects/${encodeURIComponent(state.projectId || "project")}/views/${viewType}?${params}`;

  try {
    const payload = await api(url);
    target.replaceChildren(renderView(payload, panel));
    target.scrollTop = scrollTop;
    target.scrollLeft = scrollLeft;
    setStatus(`${panel}-status`, payload.available ? "" : "empty", !payload.available);
  } catch (error) {
    target.replaceChildren(emptyState(error.message));
    target.scrollTop = scrollTop;
    target.scrollLeft = scrollLeft;
    setStatus(`${panel}-status`, "error", true);
  }
}

function renderView(payload, panel) {
  if (!payload.available) return emptyState(payload.message);
  if (payload.view_type === "original_text") return renderOriginalText(payload, panel);
  if (payload.view_type === "chunks") return renderChunks(payload, panel);
  if (payload.view_type === "scene_summary") return renderSceneSummary(payload);
  if (payload.view_type === "characters") return renderCharacters(payload, panel);
  if (payload.view_type === "scripts") return renderScripts(payload, panel);
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

function renderCharactersLegacy(payload) {
  const fragment = document.createDocumentFragment();
  const characters = stagedCharacters(payload.characters);
  fragment.appendChild(characterEditToolbar(payload));
  fragment.appendChild(metaBar(`${characters.length} character records`));
  for (const character of characters) {
    const card = cardNode("character-card");
    const header = document.createElement("div");
    header.className = "card-header";
    const title = document.createElement("h3");
    title.textContent = `${character.character_id} · ${character.canonical_name}`;
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.textContent = "Rename";
    renameButton.addEventListener("click", () => stageCharacterRename(character));
    const mergeButton = document.createElement("button");
    mergeButton.type = "button";
    mergeButton.textContent = "Merge with";
    mergeButton.disabled = character.unsaved || characters.length < 2;
    mergeButton.addEventListener("click", () => openMergeDialog(characters, character));
    actions.append(renameButton, mergeButton);
    header.append(title, actions);
    card.appendChild(header);
    card.appendChild(fieldLine("Stable aliases", (character.stable_aliases || []).join(", ") || "none"));
    card.appendChild(fieldLine("Persona", character.persona_summary || "none"));
    card.appendChild(fieldLine("Speaking style", character.speaking_style || "none"));
    card.appendChild(fieldLine("Age impression", character.age_impression || "none"));
    card.appendChild(fieldLine("Voice notes", (character.voice_variant_notes || []).join("; ") || "none"));
    fragment.appendChild(card);
  }
  return fragment;
}

function characterEditToolbar(payload) {
  const toolbar = document.createElement("div");
  toolbar.className = "edit-toolbar";
  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.textContent = "Add character";
  addButton.addEventListener("click", () => stageCharacterAddition());
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.className = "primary";
  saveButton.textContent = "save edit";
  saveButton.disabled = !hasCharacterEdits();
  const status = document.createElement("span");
  status.className = "status edit-status";
  status.textContent = hasCharacterEdits() ? "unsaved edits" : "";
  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    status.classList.remove("error");
    status.textContent = "saving...";
    try {
      await api(`/api/projects/${encodeURIComponent(payload.project_id)}/character-edits`, {
        method: "POST",
        body: JSON.stringify(state.characterEdits),
      });
      clearCharacterEdits();
      status.textContent = "saved";
      await refreshPanels();
    } catch (error) {
      saveButton.disabled = false;
      status.textContent = error.message;
      status.classList.add("error");
    }
  });
  toolbar.append(addButton, saveButton, status);
  return toolbar;
}

function stagedCharacters(characters) {
  const mergeSources = new Set(
    state.characterEdits.merges.map((merge) => merge.source_character_id)
  );
  const output = characters
    .filter((character) => !mergeSources.has(character.character_id))
    .map((character) => ({
      ...character,
      canonical_name: state.characterEdits.renames[character.character_id] || character.canonical_name,
    }));
  state.characterEdits.additions.forEach((name, index) => {
    output.push({
      character_id: `unsaved_${index + 1}`,
      canonical_name: name,
      stable_aliases: [name],
      contextual_references: [],
      aliases: [],
      alias_evidence: [],
      persona_summary: "Unsaved character.",
      speaking_style: null,
      age_impression: null,
      voice_variant_notes: [],
      confidence: 1,
      review_notes: ["Pending save."],
      unsaved: true,
    });
  });
  return output;
}

function stageCharacterAddition() {
  const name = window.prompt("Character name");
  if (!name || !name.trim()) return;
  state.characterEdits.additions.push(name.trim());
  refreshPanels();
}

function stageCharacterRename(character) {
  if (character.unsaved) {
    const index = Number(character.character_id.replace("unsaved_", "")) - 1;
    const name = window.prompt("Character name", character.canonical_name);
    if (!name || !name.trim()) return;
    state.characterEdits.additions[index] = name.trim();
    refreshPanels();
    return;
  }
  const name = window.prompt("Character name", character.canonical_name);
  if (!name || !name.trim() || name.trim() === character.canonical_name) return;
  state.characterEdits.renames[character.character_id] = name.trim();
  refreshPanels();
}

function openMergeDialog(characters, sourceCharacter) {
  const targets = characters.filter(
    (character) => (
      character.character_id !== sourceCharacter.character_id && !character.unsaved
    )
  );
  if (targets.length === 0) return;

  const dialog = document.createElement("dialog");
  dialog.className = "modal-dialog";
  const title = document.createElement("h3");
  title.textContent = `Merge ${sourceCharacter.canonical_name} with`;
  const select = document.createElement("select");
  select.className = "wide-select";
  for (const target of targets) {
    const option = document.createElement("option");
    option.value = target.character_id;
    option.textContent = `${target.canonical_name} (${target.character_id})`;
    select.appendChild(option);
  }
  const actions = document.createElement("div");
  actions.className = "dialog-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => dialog.close());
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "primary";
  confirm.textContent = "Confirm merge";
  confirm.addEventListener("click", () => {
    state.characterEdits.merges = state.characterEdits.merges.filter(
      (merge) => merge.source_character_id !== sourceCharacter.character_id
    );
    state.characterEdits.merges.push({
      source_character_id: sourceCharacter.character_id,
      target_character_id: select.value,
    });
    dialog.close();
    refreshPanels();
  });
  actions.append(cancel, confirm);
  dialog.append(title, select, actions);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}

function hasCharacterEdits() {
  return (
    state.characterEdits.additions.length > 0 ||
    Object.keys(state.characterEdits.renames).length > 0 ||
    state.characterEdits.merges.length > 0
  );
}

function clearCharacterEdits() {
  state.characterEdits = {
    additions: [],
    updates: {},
    removals: [],
    merges: [],
    scriptSpeakerMerges: [],
    voiceProfileByCharacterId: {},
    systemVoiceAssignments: {},
  };
}

function renderCharacters(payload, panel) {
  const fragment = document.createDocumentFragment();
  const characters = curatedCharacters(payload.characters || []);
  const assignments = new Map(
    (payload.assignments || []).map((assignment) => [assignment.speaker, assignment])
  );
  fragment.appendChild(characterCurationToolbar(payload, panel));
  fragment.appendChild(metaBar(`${characters.length} characters`));
  fragment.appendChild(characterReviewSummary(payload.review || {}));

  for (const speaker of ["narrator", "unknown_speaker"]) {
    const assignment = assignments.get(speaker);
    if (assignment || speaker === "narrator") {
      fragment.appendChild(systemVoiceCard(payload, speaker, assignment));
    }
  }

  const scriptSpeakerKeys = uniqueSpeakerKeys(payload.script_speaker_keys || []);
  if (scriptSpeakerKeys.length) {
    fragment.appendChild(scriptSpeakerKeySummary(scriptSpeakerKeys));
  }

  for (const character of characters) {
    const original = (payload.characters || []).find(
      (item) => item.character_id === character.character_id
    );
    fragment.appendChild(
      characterCurationCard(
        payload,
        character,
        original,
        assignments.get(character.canonical_name),
        panel
      )
    );
  }
  const curatedNames = new Set(characters.map((character) => character.canonical_name));
  for (const speaker of scriptSpeakerKeys) {
    if (["narrator", "unknown_speaker"].includes(speaker) || curatedNames.has(speaker)) {
      continue;
    }
    fragment.appendChild(scriptSpeakerKeyCard(payload, speaker, characters));
  }
  return fragment;
}

function uniqueSpeakerKeys(speakers) {
  return [...new Set(speakers.filter((speaker) => typeof speaker === "string" && speaker))];
}

function scriptSpeakerKeySummary(speakers) {
  const card = cardNode("info-card");
  const title = document.createElement("h3");
  title.textContent = "Script speaker keys";
  const values = document.createElement("p");
  values.textContent = speakers.join(", ");
  card.append(title, values);
  return card;
}

function scriptSpeakerKeyCard(payload, speaker, characters) {
  const card = cardNode("character-card script-speaker-key-card");
  const title = document.createElement("h3");
  title.textContent = speaker;
  const detail = document.createElement("p");
  detail.textContent = "Script-only speaker key — merge it into a curated character.";
  const targets = characters.filter((character) => !character.unsaved);
  const staged = state.characterEdits.scriptSpeakerMerges.find(
    (merge) => merge.source_speaker === speaker
  );
  if (staged) {
    const target = targets.find(
      (character) => character.character_id === staged.target_character_id
    );
    const status = document.createElement("p");
    status.className = "status";
    status.textContent = `will merge into ${target?.canonical_name || staged.target_character_id}`;
    card.append(title, detail, status);
    return card;
  }
  if (!targets.length) {
    card.append(title, detail);
    return card;
  }
  const label = document.createElement("label");
  label.textContent = "Merge into";
  const select = document.createElement("select");
  for (const character of targets) {
    select.appendChild(new Option(character.canonical_name, character.character_id));
  }
  label.appendChild(select);
  const merge = document.createElement("button");
  merge.type = "button";
  merge.textContent = "merge key";
  merge.addEventListener("click", () => {
    state.characterEdits.scriptSpeakerMerges.push({
      source_speaker: speaker,
      target_character_id: select.value,
    });
    refreshPanels();
  });
  card.append(title, detail, label, merge);
  return card;
}

function curatedCharacters(characters) {
  const removed = new Set(state.characterEdits.removals);
  const mergeSources = new Set(
    state.characterEdits.merges.map((merge) => merge.source_character_id)
  );
  const output = characters
    .filter((character) => !removed.has(character.character_id) && !mergeSources.has(character.character_id))
    .map((character) => ({
      ...character,
      ...(state.characterEdits.updates[character.character_id] || {}),
    }));
  state.characterEdits.additions.forEach((addition, index) => {
    output.push({
      ...addition,
      character_id: `unsaved_${index + 1}`,
      confidence: 1,
      contextual_references: [],
      aliases: [],
      alias_evidence: [],
      review_notes: ["Pending save."],
      unsaved: true,
    });
  });
  return output;
}

function characterCurationToolbar(payload, panel) {
  const toolbar = document.createElement("div");
  toolbar.className = "edit-toolbar";
  const add = document.createElement("button");
  add.type = "button";
  add.textContent = "add character";
  add.addEventListener("click", () => openCharacterAdditionDialog());
  const save = document.createElement("button");
  save.type = "button";
  save.className = "primary";
  save.textContent = "apply characters";
  save.disabled = !hasCharacterCurationEdits();
  const status = document.createElement("span");
  status.className = "status edit-status";
  status.textContent = hasCharacterCurationEdits() ? "unsaved character decisions" : "";
  save.addEventListener("click", async () => {
    save.disabled = true;
    status.classList.remove("error");
    status.textContent = "applying character decisions...";
    try {
      await saveCharacterCuration(payload);
    } catch (error) {
      save.disabled = false;
      status.textContent = error.message;
      status.classList.add("error");
    }
  });
  const discard = document.createElement("button");
  discard.type = "button";
  discard.textContent = "discard decisions";
  discard.disabled = !hasCharacterCurationEdits();
  discard.addEventListener("click", () => {
    clearCharacterEdits();
    refreshPanels();
  });
  toolbar.append(add, save, discard, status);
  return toolbar;
}

function characterCurationSavePayload() {
  return {
    additions: state.characterEdits.additions,
    updates: Object.values(state.characterEdits.updates),
    removals: state.characterEdits.removals,
    merges: state.characterEdits.merges,
    script_speaker_merges: state.characterEdits.scriptSpeakerMerges,
    voice_profile_by_character_id: state.characterEdits.voiceProfileByCharacterId,
    system_voice_assignments: state.characterEdits.systemVoiceAssignments,
  };
}

async function saveCharacterCuration(payload) {
  await api(`/api/projects/${encodeURIComponent(payload.project_id)}/characters/curation`, {
    method: "POST",
    body: JSON.stringify(characterCurationSavePayload()),
  });
  clearCharacterEdits();
  await refreshPanels();
}

function characterReviewSummary(review) {
  const fragment = document.createDocumentFragment();
  const conflicts = review.conflicts || [];
  const unknown = review.unknown_script_segment_ids || [];
  if (conflicts.length || unknown.length) {
    const card = cardNode("info-card warning-card");
    const title = document.createElement("h3");
    title.textContent = "Review signals";
    card.appendChild(title);
    if (conflicts.length) {
      card.appendChild(fieldLine("Potential alias conflicts", conflicts.map((item) => item.alias).join(", ")));
    }
    if (unknown.length) {
      card.appendChild(fieldLine("Unknown speaker script segments", unknown.join(", ")));
    }
    fragment.appendChild(card);
  }
  return fragment;
}

function characterCurationCard(payload, character, original, assignment, panel) {
  const card = cardNode("character-card");
  const header = document.createElement("div");
  header.className = "card-header";
  const name = document.createElement("input");
  name.type = "text";
  name.className = "character-name-input";
  name.value = character.canonical_name;
  name.setAttribute("aria-label", `Character name for ${character.canonical_name}`);
  const actions = document.createElement("div");
  actions.className = "card-actions";
  if (!character.unsaved) {
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "save";
    const status = document.createElement("span");
    status.className = "status";
    save.addEventListener("click", async () => {
      await saveInlineCharacter(payload, character, name, save, status);
    });
    actions.append(save, status);
  }
  if (!character.unsaved) {
    const merge = document.createElement("button");
    merge.type = "button";
    merge.textContent = "merge";
    merge.addEventListener("click", () => openCurationMergeDialog(curatedCharacters(payload.characters), character));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "remove";
    remove.addEventListener("click", () => {
      if (window.confirm(`Remove ${character.canonical_name}? Its script segments become unknown_speaker.`)) {
        if (!state.characterEdits.removals.includes(character.character_id)) {
          state.characterEdits.removals.push(character.character_id);
        }
        delete state.characterEdits.updates[character.character_id];
        delete state.characterEdits.voiceProfileByCharacterId[character.character_id];
        state.characterEdits.merges = state.characterEdits.merges.filter(
          (merge) =>
            merge.source_character_id !== character.character_id &&
            merge.target_character_id !== character.character_id
        );
        state.characterEdits.scriptSpeakerMerges = state.characterEdits.scriptSpeakerMerges.filter(
          (merge) => merge.target_character_id !== character.character_id
        );
        refreshPanels();
      }
    });
    actions.append(merge, remove);
  }
  header.append(name, actions);
  card.appendChild(header);
  card.appendChild(fieldLine("Stable aliases", (character.stable_aliases || []).join(", ") || "none"));
  if (assignment?.representative_text) {
    const quote = document.createElement("pre");
    quote.className = "representative-line";
    quote.textContent = assignment.representative_text;
    card.appendChild(fieldLine("Representative line", ""));
    card.appendChild(quote);
  }
  if (!character.unsaved) {
    card.appendChild(characterVoiceControl(payload, character, assignment, panel));
  }
  return card;
}

async function saveInlineCharacter(payload, character, nameInput, button, status) {
  const canonicalName = nameInput.value.trim();
  if (!canonicalName) {
    status.textContent = "name is required";
    status.classList.add("error");
    return;
  }
  const staged = state.characterEdits.updates[character.character_id] || {};
  const update = {
    character_id: character.character_id,
    canonical_name: canonicalName,
    stable_aliases: staged.stable_aliases || character.stable_aliases || [],
    persona_summary: staged.persona_summary ?? character.persona_summary ?? null,
    speaking_style: staged.speaking_style ?? character.speaking_style ?? null,
    age_impression: staged.age_impression ?? character.age_impression ?? null,
    voice_variant_notes: staged.voice_variant_notes || character.voice_variant_notes || [],
  };
  if (sameCharacterCuration(character, update)) {
    delete state.characterEdits.updates[character.character_id];
  } else {
    state.characterEdits.updates[character.character_id] = update;
  }
  button.disabled = true;
  status.classList.remove("error");
  status.textContent = "saving all character changes...";
  try {
    await saveCharacterCuration(payload);
  } catch (error) {
    button.disabled = false;
    status.textContent = error.message;
    status.classList.add("error");
  }
}

function characterVoiceControl(payload, character, assignment, panel) {
  const controls = document.createElement("div");
  controls.className = "voice-controls";
  const label = document.createElement("label");
  label.textContent = "Voice";
  const select = voiceProfileSelect(payload.voice_profiles || [], currentCharacterVoice(character, assignment));
  select.addEventListener("change", () => {
    stageCharacterVoice(character.character_id, select.value, assignment?.voice_profile_id || "");
  });
  label.appendChild(select);
  const original = document.createElement("audio");
  original.controls = true;
  original.preload = "none";
  original.className = "voice-audio";
  updateOriginalVoiceAudio(original, payload.voice_profiles || [], select.value);
  const preview = document.createElement("audio");
  preview.controls = true;
  preview.preload = "none";
  preview.className = "voice-audio";
  if (assignment?.sample_url) preview.src = assignment.sample_url;
  const sample = document.createElement("button");
  sample.type = "button";
  sample.textContent = "generate sample";
  sample.disabled = !payload.tts_generation_enabled || !select.value;
  sample.addEventListener("click", async () => {
    if (!select.value) return;
    try {
      const result = await api(`/api/projects/${encodeURIComponent(payload.project_id)}/voice-samples`, {
        method: "POST",
        body: JSON.stringify({ speaker: character.canonical_name, voice_profile_id: select.value }),
      });
      preview.src = `${result.sample_url}?t=${Date.now()}`;
      preview.load();
    } catch (error) {
      setStatus("global-status", error.message, true);
    }
  });
  controls.append(label, audioBlock("Original sample", original), sample, audioBlock("Generated sample", preview));
  return controls;
}

function systemVoiceCard(payload, speaker, assignment) {
  const card = cardNode("info-card");
  const title = document.createElement("h3");
  title.textContent = speaker === "narrator" ? "Narrator voice" : "Unknown speaker voice";
  const label = document.createElement("label");
  label.textContent = "Voice";
  const selected = state.characterEdits.systemVoiceAssignments[speaker] ?? assignment?.voice_profile_id ?? "";
  const select = voiceProfileSelect(payload.voice_profiles || [], selected);
  select.addEventListener("change", () => {
    const baseline = assignment?.voice_profile_id || "";
    if (select.value === baseline) delete state.characterEdits.systemVoiceAssignments[speaker];
    else state.characterEdits.systemVoiceAssignments[speaker] = select.value;
    refreshPanels();
  });
  label.appendChild(select);
  const original = document.createElement("audio");
  original.controls = true;
  original.preload = "none";
  original.className = "voice-audio";
  updateOriginalVoiceAudio(original, payload.voice_profiles || [], select.value);
  const preview = document.createElement("audio");
  preview.controls = true;
  preview.preload = "none";
  preview.className = "voice-audio";
  if (assignment?.sample_url) preview.src = assignment.sample_url;
  const sample = document.createElement("button");
  sample.type = "button";
  sample.textContent = "generate sample";
  sample.disabled = !payload.tts_generation_enabled || !select.value || !assignment?.representative_text;
  sample.addEventListener("click", async () => {
    if (!select.value) return;
    try {
      const result = await api(`/api/projects/${encodeURIComponent(payload.project_id)}/voice-samples`, {
        method: "POST",
        body: JSON.stringify({ speaker, voice_profile_id: select.value }),
      });
      preview.src = `${result.sample_url}?t=${Date.now()}`;
      preview.load();
    } catch (error) {
      setStatus("global-status", error.message, true);
    }
  });
  card.append(title, label);
  if (assignment?.representative_text) {
    const quote = document.createElement("pre");
    quote.className = "representative-line";
    quote.textContent = assignment.representative_text;
    card.append(fieldLine("Representative line", ""), quote);
  }
  card.append(audioBlock("Original sample", original), sample, audioBlock("Generated sample", preview));
  return card;
}

function voiceProfileSelect(profiles, selected) {
  const select = document.createElement("select");
  select.appendChild(new Option("Select voice", ""));
  for (const profile of profiles) {
    const option = new Option(profile.display_name, profile.profile_id);
    option.disabled = !profile.available;
    select.appendChild(option);
  }
  select.value = selected;
  return select;
}

function currentCharacterVoice(character, assignment) {
  return state.characterEdits.voiceProfileByCharacterId[character.character_id] ?? assignment?.voice_profile_id ?? "";
}

function stageCharacterVoice(characterId, profileId, baseline) {
  if (profileId === baseline) delete state.characterEdits.voiceProfileByCharacterId[characterId];
  else state.characterEdits.voiceProfileByCharacterId[characterId] = profileId;
  refreshPanels();
}

function openCharacterAdditionDialog() {
  const dialog = document.createElement("dialog");
  dialog.className = "modal-dialog";
  const title = document.createElement("h3");
  title.textContent = "Add character";
  const name = curationField("Name", "");
  const aliases = curationField("Stable aliases (comma separated)", "");
  const persona = curationField("Persona", "", true);
  const speakingStyle = curationField("Speaking style", "", true);
  const age = curationField("Age impression", "");
  const voiceNotes = curationField("Voice notes (comma separated)", "");
  const actions = document.createElement("div");
  actions.className = "dialog-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => dialog.close());
  const add = document.createElement("button");
  add.type = "button";
  add.className = "primary";
  add.textContent = "Stage character";
  add.addEventListener("click", () => {
    const canonicalName = name.input.value.trim();
    if (!canonicalName) return;
    state.characterEdits.additions.push({
      canonical_name: canonicalName,
      stable_aliases: csvValues(aliases.input.value),
      persona_summary: persona.input.value || null,
      speaking_style: speakingStyle.input.value || null,
      age_impression: age.input.value || null,
      voice_variant_notes: csvValues(voiceNotes.input.value),
    });
    dialog.close();
    refreshPanels();
  });
  actions.append(cancel, add);
  dialog.append(title, name.label, aliases.label, persona.label, speakingStyle.label, age.label, voiceNotes.label, actions);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}

function openCharacterEditor(original, character) {
  const dialog = document.createElement("dialog");
  dialog.className = "modal-dialog";
  const title = document.createElement("h3");
  title.textContent = `Edit ${character.canonical_name}`;
  const name = curationField("Name", character.canonical_name);
  const aliases = curationField("Stable aliases (comma separated)", (character.stable_aliases || []).join(", "));
  const persona = curationField("Persona", character.persona_summary || "", true);
  const speakingStyle = curationField("Speaking style", character.speaking_style || "", true);
  const age = curationField("Age impression", character.age_impression || "");
  const voiceNotes = curationField("Voice notes (comma separated)", (character.voice_variant_notes || []).join(", "));
  const actions = document.createElement("div");
  actions.className = "dialog-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => dialog.close());
  const save = document.createElement("button");
  save.type = "button";
  save.className = "primary";
  save.textContent = "Stage character";
  save.addEventListener("click", () => {
    const canonicalName = name.input.value.trim();
    if (!canonicalName) return;
    stageCharacterUpdate(original, {
      character_id: character.character_id,
      canonical_name: canonicalName,
      stable_aliases: csvValues(aliases.input.value),
      persona_summary: persona.input.value || null,
      speaking_style: speakingStyle.input.value || null,
      age_impression: age.input.value || null,
      voice_variant_notes: csvValues(voiceNotes.input.value),
    });
    dialog.close();
  });
  actions.append(cancel, save);
  dialog.append(title, name.label, aliases.label, persona.label, speakingStyle.label, age.label, voiceNotes.label, actions);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}

function curationField(labelText, value, multiline = false) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = multiline ? document.createElement("textarea") : document.createElement("input");
  input.value = value;
  label.appendChild(input);
  return { label, input };
}

function csvValues(value) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function stageCharacterUpdate(original, update) {
  if (sameCharacterCuration(original, update)) {
    delete state.characterEdits.updates[original.character_id];
  } else {
    state.characterEdits.updates[original.character_id] = update;
  }
  refreshPanels();
}

function sameCharacterCuration(character, update) {
  return (
    character.canonical_name === update.canonical_name &&
    sameStringList(character.stable_aliases, update.stable_aliases) &&
    (character.persona_summary || null) === (update.persona_summary || null) &&
    (character.speaking_style || null) === (update.speaking_style || null) &&
    (character.age_impression || null) === (update.age_impression || null) &&
    sameStringList(character.voice_variant_notes, update.voice_variant_notes)
  );
}

function sameStringList(left = [], right = []) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function openCurationMergeDialog(characters, sourceCharacter) {
  const targets = characters.filter(
    (character) => !character.unsaved && character.character_id !== sourceCharacter.character_id
  );
  if (!targets.length) return;
  const target = window.prompt(
    `Merge ${sourceCharacter.canonical_name} into character ID`,
    targets[0].character_id
  );
  if (!target || !targets.some((character) => character.character_id === target)) return;
  state.characterEdits.merges = state.characterEdits.merges.filter(
    (merge) => merge.source_character_id !== sourceCharacter.character_id
  );
  delete state.characterEdits.updates[sourceCharacter.character_id];
  delete state.characterEdits.voiceProfileByCharacterId[sourceCharacter.character_id];
  state.characterEdits.scriptSpeakerMerges = state.characterEdits.scriptSpeakerMerges.filter(
    (merge) => merge.target_character_id !== sourceCharacter.character_id
  );
  state.characterEdits.merges.push({
    source_character_id: sourceCharacter.character_id,
    target_character_id: target,
  });
  refreshPanels();
}

function hasCharacterCurationEdits() {
  return (
    state.characterEdits.additions.length > 0 ||
    Object.keys(state.characterEdits.updates).length > 0 ||
    state.characterEdits.removals.length > 0 ||
    state.characterEdits.merges.length > 0 ||
    state.characterEdits.scriptSpeakerMerges.length > 0 ||
    Object.keys(state.characterEdits.voiceProfileByCharacterId).length > 0 ||
    Object.keys(state.characterEdits.systemVoiceAssignments).length > 0
  );
}

function renderScripts(payload, panel) {
  const fragment = document.createDocumentFragment();
  const report = payload.validation_report;
  const speakerFilter = state.scriptSpeakerFilters[panel] || "";
  const segments = payload.segments.filter((segment) => {
    if (!speakerFilter) return true;
    const chunkId = segment.chunk_id || payload.selected_chunk_id || payload.chunk_id;
    return scriptCurrentSpeaker(segment, chunkId) === speakerFilter;
  });
  const status = report?.exact_reconstruction_success ? "validation passed" : "validation pending/failed";
  fragment.appendChild(scriptEditToolbar(payload, panel));
  fragment.appendChild(metaBar(`${payload.script_source} · ${payload.segments.length} segments · ${status}`));
  appendStagedScriptInserts(
    fragment,
    payload,
    null,
    payload.segments?.[0]?.chunk_id || payload.selected_chunk_id || payload.chunk_id
  );
  for (const segment of segments) {
    const chunkId = segment.chunk_id || payload.selected_chunk_id || payload.chunk_id;
    const editKey = scriptSpeakerEditKey(segment.segment_id, chunkId);
    const edit = state.scriptSpeakerEdits.updates[editKey];
    const currentSpeaker = edit?.speaker || segment.speaker;
    const currentText = edit?.text ?? segment.text;
    const block = cardNode(`segment ${segment.validation_status}`);
    const header = document.createElement("div");
    header.className = "script-card-header";
    const chunkLabel = segment.chunk_id ? `${segment.chunk_id} · ` : "";
    const label = document.createElement("span");
    label.className = "speaker speaker-label";
    label.textContent = `${chunkLabel}${segment.segment_id}`;
    const select = speakerSelect(scriptEditableSpeakers(payload), currentSpeaker);
    select.addEventListener("change", () => {
      stageScriptContentEdit(
        segment,
        select.value,
        text.value,
        chunkId,
        editKey
      );
    });
    const actions = document.createElement("div");
    actions.className = "script-card-actions";
    const generateTake = document.createElement("button");
    generateTake.type = "button";
    generateTake.className = "primary";
    generateTake.textContent = "generate take";
    generateTake.disabled = hasScriptSpeakerEdits();
    if (hasScriptSpeakerEdits()) {
      generateTake.title = "Save or discard pending script edits before generating audio";
    }
    generateTake.addEventListener("click", async () => {
      generateTake.disabled = true;
      try {
        const job = await api(
          `/api/projects/${encodeURIComponent(payload.project_id)}/audio/segment-jobs`,
          {
            method: "POST",
            body: JSON.stringify({ segment_id: segment.segment_id }),
          }
        );
        startPolling(job, panel);
      } catch (error) {
        generateTake.disabled = false;
        setStatus("global-status", error.message, true);
      }
    });
    const text = document.createElement("textarea");
    text.className = "script-text-editor";
    text.value = currentText;
    text.setAttribute("aria-label", `Script text for ${segment.segment_id}`);
    text.addEventListener("input", () => {
      stageScriptContentEdit(
        segment,
        select.value,
        text.value,
        chunkId,
        editKey
      );
    });
    const insertButton = document.createElement("button");
    insertButton.type = "button";
    insertButton.textContent = "insert below";
    insertButton.addEventListener("click", () => {
      openScriptInsertDialog(payload, segment.segment_id, chunkId, currentSpeaker);
    });
    actions.append(generateTake, insertButton);
    header.append(label, select, actions);
    block.append(header, text, scriptAudioTakeControls(payload, segment));
    if (segment.validation_errors.length > 0) {
      const errors = document.createElement("p");
      errors.className = "segment-errors";
      errors.textContent = segment.validation_errors.join("; ");
      block.appendChild(errors);
    }
    fragment.appendChild(block);
    appendStagedScriptInserts(fragment, payload, segment.segment_id, chunkId);
  }
  return fragment;
}

function scriptAudioTakeControls(payload, segment) {
  const controls = document.createElement("div");
  controls.className = "script-take-controls";
  const takes = (segment.audio_takes || []).filter((take) => take.audio_url);
  if (!takes.length) {
    const status = document.createElement("span");
    status.className = "script-take-status";
    status.textContent = segment.stale_audio_take_count
      ? "Previous audio is stale — generate a new take"
      : "No audio take yet";
    controls.appendChild(status);
    return controls;
  }

  const selected = takes.find((take) => take.selected) || takes[0];
  let selectedTakeNumber = selected.take_number;
  const player = document.createElement("audio");
  player.controls = true;
  player.preload = "none";
  player.className = "voice-audio script-take-player";
  player.src = selected.audio_url;

  const takeLabel = document.createElement("label");
  takeLabel.className = "script-take-picker";
  takeLabel.textContent = "Take";
  const takeSelect = document.createElement("select");
  takeSelect.setAttribute("aria-label", `Selected audio take for ${segment.segment_id}`);
  for (const take of takes) {
    const option = new Option(`Take ${take.take_number}`, String(take.take_number));
    option.selected = take.take_number === selected.take_number;
    takeSelect.appendChild(option);
  }
  takeLabel.appendChild(takeSelect);
  const status = document.createElement("span");
  status.className = "script-take-status";
  status.textContent = "selected for final audiobook";
  takeSelect.addEventListener("change", async () => {
    const selectedTake = takes.find(
      (take) => take.take_number === Number(takeSelect.value)
    );
    if (!selectedTake) return;
    takeSelect.disabled = true;
    status.classList.remove("error");
    status.textContent = "selecting take...";
    try {
      await api(
        `/api/projects/${encodeURIComponent(payload.project_id)}/audio-takes/${encodeURIComponent(segment.segment_id)}/select`,
        {
          method: "POST",
          body: JSON.stringify({ take_number: selectedTake.take_number }),
        }
      );
      player.src = selectedTake.audio_url;
      player.load();
      selectedTakeNumber = selectedTake.take_number;
      status.textContent = "selected for final audiobook";
    } catch (error) {
      takeSelect.value = String(selectedTakeNumber);
      status.textContent = error.message;
      status.classList.add("error");
    } finally {
      takeSelect.disabled = false;
    }
  });
  controls.append(player, takeLabel, status);
  return controls;
}

function scriptCurrentSpeaker(segment, chunkId) {
  const editKey = scriptSpeakerEditKey(segment.segment_id, chunkId);
  return state.scriptSpeakerEdits.updates[editKey]?.speaker || segment.speaker;
}

function appendStagedScriptInserts(fragment, payload, afterSegmentId, chunkId) {
  const inserts = state.scriptSpeakerEdits.inserts.filter(
    (insert) => insert.after_segment_id === afterSegmentId && insert.chunk_id === chunkId
  );
  for (const insert of inserts) {
    const card = cardNode("segment pending-script-insert");
    const label = document.createElement("p");
    label.className = "speaker";
    label.textContent = `Pending insert · ${insert.speaker}`;
    const text = document.createElement("pre");
    text.textContent = insert.text;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "remove pending insert";
    remove.addEventListener("click", () => {
      state.scriptSpeakerEdits.inserts = state.scriptSpeakerEdits.inserts.filter(
        (candidate) => candidate !== insert
      );
      updateScriptEditControls();
      refreshPanels();
    });
    card.append(label, text, remove);
    fragment.appendChild(card);
  }
}

function scriptEditToolbar(payload, panel) {
  const toolbar = document.createElement("div");
  toolbar.className = "edit-toolbar";
  const filterLabel = document.createElement("label");
  filterLabel.textContent = "Speaker filter";
  const filter = document.createElement("select");
  filter.className = "script-speaker-filter";
  filter.appendChild(new Option("All speakers", ""));
  for (const speaker of scriptFilterSpeakers(payload, panel)) {
    filter.appendChild(new Option(speaker, speaker));
  }
  filter.value = state.scriptSpeakerFilters[panel] || "";
  filter.addEventListener("change", () => {
    state.scriptSpeakerFilters[panel] = filter.value;
    renderPanel(panel);
  });
  filterLabel.appendChild(filter);
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.className = "primary script-save-button";
  saveButton.textContent = "save edit";
  saveButton.disabled = !hasScriptSpeakerEdits();
  const status = document.createElement("span");
  status.className = "status edit-status script-edit-status";
  status.textContent = scriptEditStatus();
  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    status.classList.remove("error");
    status.textContent = "saving...";
    try {
      await api(`/api/projects/${encodeURIComponent(payload.project_id)}/script-edits`, {
        method: "POST",
        body: JSON.stringify({
          updates: Object.values(state.scriptSpeakerEdits.updates),
          inserts: state.scriptSpeakerEdits.inserts,
        }),
      });
      clearScriptSpeakerEdits();
      status.textContent = "saved";
      await refreshPanels();
    } catch (error) {
      saveButton.disabled = false;
      status.textContent = error.message;
      status.classList.add("error");
    }
  });
  const unifyButton = document.createElement("button");
  unifyButton.type = "button";
  unifyButton.className = "primary";
  unifyButton.textContent = "auto unify characters";
  unifyButton.classList.add("script-unify-button");
  unifyButton.dataset.stage3Enabled = String(payload.stage3_enabled);
  unifyButton.disabled = !payload.stage3_enabled || hasScriptSpeakerEdits();
  if (!payload.stage3_enabled) {
    unifyButton.title = `Stage 3 requires ${payload.stage3_missing_inputs.join(", ")}`;
  } else if (hasScriptSpeakerEdits()) {
    unifyButton.title = "Save or discard pending speaker edits first";
  }
  unifyButton.addEventListener("click", () => {
    unifyButton.disabled = true;
    startStage3(panel).catch((error) => {
      unifyButton.disabled = false;
      setPanelJobStatus(panel, error.message, 0, 1, true);
    });
  });
  const insertButton = document.createElement("button");
  insertButton.type = "button";
  insertButton.textContent = "insert at start";
  insertButton.addEventListener("click", () => {
    const firstChunkId = payload.segments?.[0]?.chunk_id;
    openScriptInsertDialog(
      payload,
      null,
      firstChunkId || payload.selected_chunk_id || payload.chunk_id,
      "narrator"
    );
  });
  const repairButton = document.createElement("button");
  repairButton.type = "button";
  repairButton.textContent = "repair source spans";
  repairButton.addEventListener("click", async () => {
    repairButton.disabled = true;
    status.classList.remove("error");
    status.textContent = "repairing spans...";
    try {
      await api(`/api/projects/${encodeURIComponent(payload.project_id)}/script-edits`, {
        method: "POST",
        body: JSON.stringify({ updates: [], inserts: [] }),
      });
      status.textContent = "source spans repaired";
      await refreshPanels();
    } catch (error) {
      repairButton.disabled = false;
      status.textContent = error.message;
      status.classList.add("error");
    }
  });
  const discardButton = document.createElement("button");
  discardButton.type = "button";
  discardButton.textContent = "discard edits";
  discardButton.disabled = !hasScriptSpeakerEdits();
  discardButton.className = "script-discard-button";
  discardButton.addEventListener("click", () => {
    clearScriptSpeakerEdits();
    updateScriptEditControls();
    refreshPanels();
  });
  toolbar.append(
    unifyButton,
    saveButton,
    insertButton,
    repairButton,
    discardButton,
    filterLabel,
    status
  );
  return toolbar;
}

function scriptFilterSpeakers(payload, panel) {
  return uniqueSpeakerKeys([
    ...scriptEditableSpeakers(payload),
    state.scriptSpeakerFilters[panel] || "",
  ]);
}

function scriptEditableSpeakers(payload) {
  const stagedSpeakers = [
    ...Object.values(state.scriptSpeakerEdits.updates).map((edit) => edit.speaker),
    ...state.scriptSpeakerEdits.inserts.map((insert) => insert.speaker),
  ];
  return uniqueSpeakerKeys([
    ...(payload.speaker_options || []),
    ...(payload.speaker_filter_options || []),
    ...(payload.segments || []).map((segment) => scriptCurrentSpeaker(
      segment,
      segment.chunk_id || payload.selected_chunk_id || payload.chunk_id
    )),
    ...stagedSpeakers,
  ]);
}

function speakerSelect(options, currentSpeaker) {
  const select = document.createElement("select");
  select.className = "speaker-select";
  const seen = new Set();
  for (const value of [currentSpeaker, ...options]) {
    if (!value || seen.has(value)) continue;
    seen.add(value);
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  }
  select.value = currentSpeaker;
  return select;
}

function stageScriptContentEdit(segment, speaker, text, chunkId, editKey) {
  if (speaker === segment.speaker && text === segment.text) {
    delete state.scriptSpeakerEdits.updates[editKey];
  } else {
    state.scriptSpeakerEdits.updates[editKey] = {
      segment_id: segment.segment_id,
      speaker,
      text,
      chunk_id: chunkId,
    };
  }
  setStatus("global-status", `${segment.segment_id} edit staged`);
  updateScriptEditControls();
}

function scriptSpeakerEditKey(segmentId, chunkId) {
  return `${chunkId || "complete"}:${segmentId}`;
}

function hasScriptSpeakerEdits() {
  return (
    Object.keys(state.scriptSpeakerEdits.updates).length > 0 ||
    state.scriptSpeakerEdits.inserts.length > 0
  );
}

function clearScriptSpeakerEdits() {
  state.scriptSpeakerEdits = {
    updates: {},
    inserts: [],
  };
}

function scriptEditStatus() {
  const updates = Object.keys(state.scriptSpeakerEdits.updates).length;
  const inserts = state.scriptSpeakerEdits.inserts.length;
  if (!updates && !inserts) return "";
  const parts = [];
  if (updates) parts.push(`${updates} edited segment${updates === 1 ? "" : "s"}`);
  if (inserts) parts.push(`${inserts} inserted segment${inserts === 1 ? "" : "s"}`);
  return `unsaved: ${parts.join(", ")}`;
}

function updateScriptEditControls() {
  const dirty = hasScriptSpeakerEdits();
  for (const button of document.querySelectorAll(".script-save-button, .script-discard-button")) {
    button.disabled = !dirty;
  }
  for (const status of document.querySelectorAll(".script-edit-status")) {
    status.textContent = scriptEditStatus();
    status.classList.remove("error");
  }
  for (const button of document.querySelectorAll(".script-unify-button")) {
    const enabled = button.dataset.stage3Enabled === "true";
    button.disabled = !enabled || dirty;
    if (enabled) {
      button.title = dirty ? "Save or discard pending script edits first" : "";
    }
  }
}

function openScriptInsertDialog(payload, afterSegmentId, chunkId, defaultSpeaker) {
  const dialog = document.createElement("dialog");
  dialog.className = "modal-dialog";
  const title = document.createElement("h3");
  title.textContent = afterSegmentId ? `Insert after ${afterSegmentId}` : "Insert at start";
  const speakerLabel = document.createElement("label");
  speakerLabel.textContent = "Speaker";
  const select = speakerSelect(scriptEditableSpeakers(payload), defaultSpeaker);
  speakerLabel.appendChild(select);
  const textLabel = document.createElement("label");
  textLabel.textContent = "Script text";
  const text = document.createElement("textarea");
  text.className = "script-text-editor";
  textLabel.appendChild(text);
  const actions = document.createElement("div");
  actions.className = "dialog-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => dialog.close());
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "primary";
  confirm.textContent = "Insert";
  confirm.addEventListener("click", () => {
    if (!text.value) {
      text.focus();
      return;
    }
    state.scriptSpeakerEdits.inserts.push({
      after_segment_id: afterSegmentId,
      speaker: select.value,
      text: text.value,
      chunk_id: chunkId,
    });
    dialog.close();
    setStatus("global-status", "script insertion staged");
    updateScriptEditControls();
    refreshPanels();
  });
  actions.append(cancel, confirm);
  dialog.append(title, speakerLabel, textLabel, actions);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
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

  const originalAudio = document.createElement("audio");
  originalAudio.controls = true;
  originalAudio.preload = "none";
  originalAudio.className = "voice-audio";
  updateOriginalVoiceAudio(originalAudio, payload.voice_profiles, select.value);

  const generatedAudio = document.createElement("audio");
  generatedAudio.controls = true;
  generatedAudio.preload = "none";
  generatedAudio.className = "voice-audio";
  if (assignment.sample_url) generatedAudio.src = assignment.sample_url;

  const sampleButton = document.createElement("button");
  sampleButton.className = "sample-button";
  sampleButton.type = "button";
  sampleButton.textContent = "generate sample";
  sampleButton.disabled = !payload.tts_generation_enabled || !select.value;

  const rowStatus = document.createElement("span");
  rowStatus.className = "voice-row-status status";
  if (!payload.tts_generation_enabled) {
    rowStatus.textContent = "CLI smoke pending";
  }

  select.addEventListener("change", (event) => {
    const profileId = event.target.value;
    state.voiceAssignments[assignment.speaker] = profileId;
    sampleButton.disabled = !payload.tts_generation_enabled || !profileId;
    updateOriginalVoiceAudio(originalAudio, payload.voice_profiles, profileId);
    if (!profileId) {
      status.textContent = "unassigned";
      originalAudio.removeAttribute("src");
      generatedAudio.removeAttribute("src");
      rowStatus.textContent = "";
      return;
    }
    status.textContent = "assigned";
    rowStatus.textContent = "";
  });

  sampleButton.addEventListener("click", async () => {
    if (!payload.tts_generation_enabled) return;
    const profileId = select.value;
    if (!profileId) {
      rowStatus.textContent = "select a voice first";
      rowStatus.classList.add("error");
      return;
    }
    rowStatus.classList.remove("error");
    status.textContent = "previewing";
    rowStatus.textContent = "generating preview...";
    setSampleButtonLoading(sampleButton, true);
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
      generatedAudio.src = `${result.sample_url}?t=${Date.now()}`;
      generatedAudio.load();
      status.textContent = "assigned";
      rowStatus.textContent = "preview ready";
    } catch (error) {
      status.textContent = "preview failed";
      rowStatus.textContent = error.message;
      rowStatus.classList.add("error");
    } finally {
      setSampleButtonLoading(sampleButton, false);
    }
  });

  controls.append(
    label,
    audioBlock("Original sample", originalAudio),
    sampleButton,
    audioBlock("Generated sample", generatedAudio),
    rowStatus
  );
  card.append(header, summary, quoteLabel, quote, controls);
  return card;
}

function updateOriginalVoiceAudio(audio, profiles, profileId) {
  const profile = profiles.find((item) => item.profile_id === profileId);
  if (profile?.sample_url) {
    audio.src = profile.sample_url;
    audio.load();
  } else {
    audio.removeAttribute("src");
  }
}

function audioBlock(labelText, audio) {
  const block = document.createElement("div");
  block.className = "voice-audio-block";
  const label = document.createElement("span");
  label.className = "voice-audio-label";
  label.textContent = labelText;
  block.append(label, audio);
  return block;
}

function setSampleButtonLoading(button, isLoading) {
  button.classList.toggle("loading", isLoading);
  button.disabled = isLoading;
  button.textContent = isLoading ? "generating..." : "generate sample";
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
  button.textContent = "generate audio takes";
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
  syncGlobalAudioButtons();
  el("generate-all-button").addEventListener("click", () => {
    startGlobalAudioGeneration().catch((error) =>
      setStatus("global-status", error.message, true)
    );
  });
  el("play-all-button").addEventListener("click", () => {
    startGlobalPlayback().catch((error) =>
      setStatus("global-status", error.message, true)
    );
  });
  el("global-audio-player").addEventListener("ended", () => {
    playNextGlobalAudio().catch((error) =>
      stopGlobalPlayback(`playback failed: ${error.message}`, true)
    );
  });
  el("global-audio-player").addEventListener("error", () => {
    if (state.playbackQueue.length) {
      stopGlobalPlayback("playback failed while loading audio", true);
    }
  });
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
    stopPolling();
    stopGlobalPlayback();
    state.projectId = event.target.value.trim();
    rememberProjectId(state.projectId);
    state.chunkSelection = "all";
    clearCharacterEdits();
    clearScriptSpeakerEdits();
    await refreshPanels();
    await resumeActiveTtsJob();
  });
  el("project-id").addEventListener("input", (event) => {
    state.projectId = event.target.value.trim();
    rememberProjectId(state.projectId);
  });
  loadSources().catch((error) => setStatus("global-status", error.message, true));
});
