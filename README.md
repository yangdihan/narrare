# Narrare

Narrare is a local-first audiobook production workflow for turning novels into faithful, multi-voice audiobooks.

Unlike tools that rewrite a novel into a radio drama script, Narrare preserves the original text exactly. AI-generated metadata, such as speaker labels, emotion, pacing, and voice suggestions, is stored separately from the source text.

The key feature is controllable production: different characters can use different voices, and humans can inspect, adjust, regenerate, or override AI decisions at each important step before final assembly.

---

# MVP

The MVP consists of two major stages.

## Stage 1

Novel

↓

Annotated Script

The system converts a novel into a structured script while preserving the original text exactly.

The generated script contains:

- speaker
- narration/dialogue
- emotion
- suggested pause
- confidence
- source span

The original text remains untouched.

---

## Stage 2

Annotated Script

Per-chunk script artifacts are deterministically assembled into one complete script after all chunks pass validation.

When a valid Stage 2 response misaligns with the source, Narrare can retry only the paragraph-bounded failed span and then revalidate the whole chunk.

↓

Audio Segments

↓

Human Review

↓

Final Audiobook

The TTS engine generates one audio segment per script entry.

Users review every segment, regenerate problematic ones, and approve each segment before final assembly.

---

# Why?

Current audiobook generators usually optimize for automation.

Narrare optimizes for quality.

Every important AI decision can be inspected, corrected, regenerated, or overridden.

---

# Design Principles

- Original text is immutable.
- Human review is required.
- AI modules are replaceable.
- Intermediate artifacts are preserved.
- The workflow is deterministic whenever possible.

---

# Current Scope

Input

- TXT (MVP)

Future

- EPUB
- MOBI
- HTML
- Markdown

Output

- Annotated Script
- Audio Segments
- Audiobook

---

# Planned Architecture

Novel

↓

Chunking

↓

LLM Pipeline

↓

Script Assembly

↓

Stage 3 Speaker Key Review

↓

Annotated Script (IR)

↓

Characters (review and voices)

↓

Segment TTS

↓

Human Review

↓

Regeneration Loop

↓

Final Assembly

↓

Audiobook

---

# Running The Current Pipeline

Set the project ID first. This must match the folder under `data/interim/`.

| Windows terminal (PowerShell) | macOS terminal (bash) | Web app |
| --- | --- | --- |
| **Open a project**<br><br>`$env:PROJECT_ID = "bicentennial_man"`<br><br>**Launch the web app**<br><br>`.\.venv\Scripts\python.exe -m uvicorn ui.web.app:app --host 127.0.0.1 --port 8000` | **Open a project**<br><br>`PROJECT_ID=bicentennial_man`<br><br>**Launch the web app**<br><br>`.venv/bin/python -m uvicorn ui.web.app:app --host 127.0.0.1 --port 8000` | Open `http://127.0.0.1:8000`, choose the source TXT file, and enter the same project ID in the context bar. |
| **Prepare chunks**<br><br>`.\.venv\Scripts\python.exe -m cli.main chunk data\raw\<source>.txt --project-id $env:PROJECT_ID` | **Prepare chunks**<br><br>`.venv/bin/python -m cli.main chunk data/raw/<source>.txt --project-id "$PROJECT_ID"` | In **Original Text**, click **chunk it**. |
| **Stage 1: context and character profiling**<br><br>`.\.venv\Scripts\python.exe -m cli.main context-profile --project-id $env:PROJECT_ID` | **Stage 1: context and character profiling**<br><br>`.venv/bin/python -m cli.main context-profile --project-id "$PROJECT_ID"` | In **Original Text**, click **overview chunks**. Review context artifacts, then use **Characters** to resolve identity evidence and curate the registry. |
| **Stage 2: script conversion**<br><br>`foreach ($chunk in (Get-ChildItem "data\interim\$env:PROJECT_ID\chunks\chunk_*.txt")) { .\.venv\Scripts\python.exe -m cli.main script-convert $chunk.FullName --project-id $env:PROJECT_ID --chunk-id $chunk.BaseName }` | **Stage 2: script conversion**<br><br>`for chunk_path in data/interim/$PROJECT_ID/chunks/chunk_*.txt; do chunk_id=$(basename "$chunk_path" .txt); .venv/bin/python -m cli.main script-convert "$chunk_path" --project-id "$PROJECT_ID" --chunk-id "$chunk_id"; done` | In **Chunks**, choose **all** or one chunk and click **feed to LLM**. Inspect segment validation in **Scripts**. |
| **Assemble valid chunk scripts**<br><br>`.\.venv\Scripts\python.exe -m cli.main script-assemble --project-id $env:PROJECT_ID` | **Assemble valid chunk scripts**<br><br>`.venv/bin/python -m cli.main script-assemble --project-id "$PROJECT_ID"` | No dedicated action yet; run the terminal command after the required Stage 2 chunks pass validation. Consecutive same-speaker scripts are capped at 500 Chinese characters, preferring sentence punctuation for splits. Unchanged segments retain their IDs, so their existing takes remain linked. If reassembly changes the script, stale Stage 3 artifacts are kept for audit but automatically bypassed; rerun Stage 3 to create a current reviewed copy. |
| **Stage 3: key review**<br><br>`.\.venv\Scripts\python.exe -m cli.main speaker-key-review --project-id $env:PROJECT_ID --batch-size 16 --max-output-tokens 2400` | **Stage 3: key review**<br><br>`.venv/bin/python -m cli.main speaker-key-review --project-id "$PROJECT_ID" --batch-size 16 --max-output-tokens 2400` | In **Scripts**, click **auto unify characters**. Stable aliases are unified locally; only unresolved keys are sent in chunk-local batches. Each batch uses GPT-5 Mini’s minimal reasoning effort and returns only compact decision records. The speaker editor can reuse every existing Script key, including a Script-only key that is awaiting a merge. |
| **Stage 4: bootstrap Qwen**<br><br>`.\.venv\Scripts\python.exe -m cli.main qwen-bootstrap --source data\incoming\qwen --model Qwen3-TTS-12Hz-1.7B-Base`<br><br>**CUDA smoke test**<br><br>`.\.venv\Scripts\python.exe -m cli.main tts-generate --text "这是一次 CUDA 推理测试。" --voice-profile-id BiuBoom --output data\interim\qwen_smoke\biuboom_cuda.wav --device cuda` | **Stage 4: bootstrap Qwen**<br><br>`.venv/bin/python -m cli.main qwen-bootstrap --source Qwen3-Audiobook-Studio-v1.0-lite --model Qwen3-TTS-12Hz-1.7B-Base`<br><br>**Create a voice prompt**<br><br>`.venv/bin/python -m cli.main voice-prompt-create --sample data/voices/qwen/samples/<sample>.m4a --text "<matching transcript>" --profile-id smoke_voice` | Qwen bootstrap and voice-prompt creation are terminal-only. Imported profiles become available in **Characters**. |
| **Stage 4: voice assignment and audio takes**<br><br>`.\.venv\Scripts\python.exe -m cli.main voice-assign-init --project-id $env:PROJECT_ID`<br><br>`.\.venv\Scripts\python.exe -m cli.main audio-generate --project-id $env:PROJECT_ID` | **Stage 4: voice assignment and audio takes**<br><br>`.venv/bin/python -m cli.main voice-assign-init --project-id "$PROJECT_ID"`<br><br>`.venv/bin/python -m cli.main audio-generate --project-id "$PROJECT_ID"` | In **Characters**, inspect all current Script speaker keys, merge script-only keys into curated records, edit a name inline and click **save** to synchronize it, then assign voices. Use the app-level **generate all** button to create missing takes and **play all** to play every selected current take in script order. In **Scripts**, historical takes whose text, speaker key, or assigned voice no longer matches the current segment are retained for audit but hidden as stale. Refreshing the page reconnects to an active TTS job and restores its progress bar. Set `NARRARE_QWEN_DEVICE=cpu` only to force CPU. |
| **Stage 5: final audiobook assembly**<br><br>`.\.venv\Scripts\python.exe -m cli.main audio-assemble --project-id $env:PROJECT_ID` | **Stage 5: final audiobook assembly**<br><br>`.venv/bin/python -m cli.main audio-assemble --project-id "$PROJECT_ID"` | Final assembly is terminal-only. It freezes the selected current take for every script into `assembly_manifest.json`, normalizes gated active-speech RMS to −20 dBFS with a −1 dBFS peak ceiling, and streams the clips in script order to `audio/final/audiobook.wav`. It fails before rendering if any script lacks a selected current take. |
| **Qwen readiness check**<br><br>`.\.venv\Scripts\python.exe -m cli.main qwen-delete-check` | **Qwen readiness check**<br><br>`.venv/bin/python -m cli.main qwen-delete-check` | No GUI action yet. Run the terminal check before deleting old Qwen assets. |

Audio takes are stored as numbered alternatives. In **Scripts**, use **generate take** to create the next take for one segment and select the take that final-audiobook assembly should use.

Final assembly stores the exact ordered take selection in `data/interim/<project_id>/assembly_manifest.json`. Per-clip normalization measurements and applied gains are recorded in `audio/final/audiobook.json` so the output can be audited and reproduced without parsing the original novel.

---

# Future Roadmap

- EPUB support
- MOBI support
- English novels
- Expressive TTS
- Background music generation
- Noise normalization
- Chapter atmosphere generation
- Voice library management
- Plugin architecture
- Multi-model support

---

# Status

Project initialization.
