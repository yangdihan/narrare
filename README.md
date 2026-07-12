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

If chunks do not exist yet, create them from a TXT source:

```bash
.venv/bin/python -m cli.main chunk data/raw/<source>.txt --project-id "$PROJECT_ID"
```

Recommended order for script conversion and speaker-key standardization:

1. Run Stage 1 over chunks, in order.
2. Run Stage 2 per chunk, starting from the first missing or invalid chunk.
3. Assemble the validated chunk scripts once.
4. Run Stage 3 on the assembled complete script.

Specific command sequence starting Stage 2 at chunk 16:

```bash
.venv/bin/python -m cli.main context-profile --project-id "$PROJECT_ID"

for chunk_path in data/interim/$PROJECT_ID/chunks/chunk_*.txt; do
  chunk_id=$(basename "$chunk_path" .txt)
  chunk_num=${chunk_id#chunk_}
  if [ "$chunk_num" -ge 16 ]; then
    .venv/bin/python -m cli.main script-convert "$chunk_path" \
      --project-id "$PROJECT_ID" \
      --chunk-id "$chunk_id"
  fi
done

.venv/bin/python -m cli.main script-assemble --project-id "$PROJECT_ID"

.venv/bin/python -m cli.main speaker-key-review --project-id "$PROJECT_ID"
```

General reusable form:

```bash
PROJECT_ID=<your_project_id>
START_CHUNK=<first_chunk_number_to_process>

.venv/bin/python -m cli.main context-profile --project-id "$PROJECT_ID"

for chunk_path in data/interim/$PROJECT_ID/chunks/chunk_*.txt; do
  chunk_id=$(basename "$chunk_path" .txt)
  chunk_num=${chunk_id#chunk_}
  if [ "$chunk_num" -ge "$START_CHUNK" ]; then
    .venv/bin/python -m cli.main script-convert "$chunk_path" \
      --project-id "$PROJECT_ID" \
      --chunk-id "$chunk_id"
  fi
done

.venv/bin/python -m cli.main script-assemble --project-id "$PROJECT_ID"

.venv/bin/python -m cli.main speaker-key-review --project-id "$PROJECT_ID"
```

The final key-reviewed script is written to:

```bash
data/interim/$PROJECT_ID/ir/script/complete_key_reviewed_script.json
```

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
