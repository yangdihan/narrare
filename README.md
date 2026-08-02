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

Voice Assignment

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

Specific current project:

```bash
PROJECT_ID=bicentennial_man
```

General form:

```bash
PROJECT_ID=<your_project_id>
```

```bash
.venv/bin/uvicorn ui.web.app:app --host 127.0.0.1 --port8012
```

| Terminal commands | GUI webapp |
| --- | --- |
| **Open the current project**<br><br>`PROJECT_ID=<your_project_id>` | **Launch the local workspace**<br><br>`.venv/bin/uvicorn ui.web.app:create_app --factory --reload`<br><br>Open the webapp, choose the source file, and set the same project ID in the context bar. |
| **Prepare chunks from a TXT source**<br><br>`.venv/bin/python -m cli.main chunk data/raw/<source>.txt --project-id "$PROJECT_ID"` | **Prepare chunks in the GUI**<br><br>Open the Original Text panel and click **chunk it**. |
| **Stage 1: chunk context and character profiling**<br><br>`.venv/bin/python -m cli.main context-profile --project-id "$PROJECT_ID"` | **Stage 1 in the GUI**<br><br>Open the Original Text panel and click **overview chunks**. Review the generated context in the Scene Summary and Character Summary panels. |
| **Stage 2: convert chunks into script IR**<br><br>`START_CHUNK=<first_chunk_number_to_process>`<br><br>`for chunk_path in data/interim/$PROJECT_ID/chunks/chunk_*.txt; do chunk_id=$(basename "$chunk_path" .txt); chunk_num=${chunk_id#chunk_}; if [ "$chunk_num" -ge "$START_CHUNK" ]; then .venv/bin/python -m cli.main script-convert "$chunk_path" --project-id "$PROJECT_ID" --chunk-id "$chunk_id"; fi; done` | **Stage 2 in the GUI**<br><br>Open the Chunks panel, choose **all** or a specific chunk, and click **feed to LLM**. Review segment-level validation in the Scripts panel. |
| **Assemble validated Stage 2 scripts**<br><br>`.venv/bin/python -m cli.main script-assemble --project-id "$PROJECT_ID"` | **Assembly in the GUI**<br><br>No dedicated GUI action yet. Run this terminal command after the needed Stage 2 chunks pass validation. |
| **Stage 3: speaker-key standardization review**<br><br>`.venv/bin/python -m cli.main speaker-key-review --project-id "$PROJECT_ID"`<br><br>Final script: `data/interim/$PROJECT_ID/ir/script/complete_key_reviewed_script.json` | **Stage 3 in the GUI**<br><br>Open Scripts and click **auto unify characters**. The panel shows segment-level progress and refreshes to the key-reviewed complete script when the job finishes. Use the speaker dropdown for any remaining human corrections. |
| **Stage 4 setup: Qwen TTS assets and voice prompts**<br><br>`.venv/bin/python -m cli.main qwen-bootstrap --source Qwen3-Audiobook-Studio-v1.0-lite --model Qwen3-TTS-12Hz-1.7B-Base`<br><br>`.venv/bin/python -m cli.main voice-prompt-create --sample data/voices/qwen/samples/f语文老师上公开课了.m4a --text "<matching transcript>" --profile-id smoke_f_teacher`<br><br>`.venv/bin/python -m cli.main tts-generate --text "只要不违背第一条规则或第二条规则，机器人必须保护它自身的生存。" --voice-profile-id f语文老师上公开课了 --output data/interim/qwen_smoke/f语文老师上公开课了_preview.wav` | **Stage 4 setup in the GUI**<br><br>Qwen bootstrap and voice-prompt creation are terminal-only. The GUI can use imported voice profiles after they exist under `data/voices/qwen/`. |
| **Stage 4: voice assignment and audio takes**<br><br>`.venv/bin/python -m cli.main voice-assign-init --project-id "$PROJECT_ID"`<br><br>`.venv/bin/python -m cli.main voice-assign --project-id "$PROJECT_ID" narrator=<voice_profile_id> character_key=<voice_profile_id>`<br><br>`.venv/bin/python -m cli.main audio-generate --project-id "$PROJECT_ID"` | **Stage 4 in the GUI**<br><br>Open Voice Assignment, select a voice for each speaker, optionally click **generate sample**, then click **confirm voice assignment & generate** to create missing audio takes. |
| **Qwen cleanup readiness check**<br><br>`.venv/bin/python -m cli.main qwen-delete-check` | **Cleanup check in the GUI**<br><br>No GUI action yet. Use the terminal check before deleting old Qwen folders. |

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
