# Design Document

Narrare is an audiobook production pipeline.

---

# Overall Architecture

Novel

↓

Document Loader

↓

Chunk Manager

↓

LLM Pipeline

↓

Annotated Script (IR)

↓

Voice Assignment

↓

Segment Generation

↓

Human Review

↓

Audio Post-processing

↓

Final Assembly

↓

Audiobook

---

# Current Repository Context

This repository is not yet a clean implementation.

It currently contains:

- `data/raw/`

  Source novel files used for testing.

  `两百岁的寿星.txt` is the current full TXT test input.

  `两百岁的寿星1.txt` is the smaller scoped test input.

- `data/voices/`

  Raw voice sample audio files.

  These are source samples for voice cloning or voice-profile creation.

- `data/interim/`

  Intended location for generated intermediate artifacts.

  This should become the default workspace for IR, segment plans, generated takes, review state, and assembly manifests.

- `data/processed/`

  Intended location for approved final outputs.

- `notebooks/`

  Previous manual tooling.

  These notebooks and helper scripts demonstrate the workflow problems Narrare must solve:

  - convert script JSON to a TTS-friendly text format
  - detect missing `seg_###.wav` files
  - generate retry input for failed or deleted segments
  - map regenerated clips back to original segment indices
  - concatenate segment WAV files into a final output

  This folder is reference material only.

  It is not the target architecture.

- `Qwen3-Audiobook-Studio/`

  A downloaded Gradio TTS application.

  It is useful as a reference for Qwen TTS usage patterns, but it should not be treated as Narrare architecture.

- `Qwen3-Audiobook-Studio-v1.0-lite/`

  A larger downloaded package containing local Qwen models, a bundled runtime, saved `.pt` voice prompts, and Gradio demo scripts.

  This is most useful as a local model and asset source.

  Narrare should extract the functional TTS calls behind its own adapter instead of importing the GUI application directly.

---

# Existing Experimental Workflow

The previous manual workflow can be summarized as:

Novel-derived script JSON

↓

Speaker-tagged TTS text

↓

Batch TTS generation into `seg_###.wav`

↓

Manual listening and deletion of bad segments

↓

Missing segment scan

↓

Retry script generation

↓

Regenerated clips remapped to original segment indices

↓

Final WAV concatenation

This is the clearest current proof of what Narrare needs to productize.

The correct product version is not a notebook.

The correct product version is:

- explicit segment IDs
- immutable text spans
- versioned audio takes
- review status per segment
- regeneration queues
- assembly manifests
- reproducible output folders

---

# Target Directory Design

The long-term source tree should move toward:

```text
core/
  Business logic.
  No UI.
  No model-specific APIs.

core/models/
  Pydantic data models for documents, spans, IR segments, characters, voices, takes, review state, and assembly manifests.

core/document/
  TXT loading, future EPUB/MOBI/HTML/Markdown loading, source hashing, and source span management.

core/chunking/
  LLM-request chunking with paragraph grouping, character bounds, token estimates, and overlap tracking.

core/ir/
  IR generation orchestration, validation, and integrity checks.

core/review/
  Review state transitions, approval/rejection, regeneration queue creation.

core/audio/
  Segment take bookkeeping, silence insertion plans, final assembly logic.

config/
  Centralized runtime configuration.
  Provider names, model names, model paths, and API settings live here.

llm/
  Shared LLM service and LLM adapters.
  No pipeline stage calls a provider API directly.

tts/
  TTS adapters only.
  Qwen should be one adapter, not a core dependency.

storage/
  Read/write layer for JSON artifacts and audio artifact paths.

ui/
  Human review interface.
  No direct AI model calls.

docs/
  Documentation.
```

Existing `data/` should remain the default local artifact root.

Downloaded third-party projects should not become part of the core source tree.

---

# MVP LLM Provider Decision

For the MVP, Narrare uses OpenRouter as the sole LLM provider.

The default model for every LLM stage is OpenAI GPT-5 Mini through OpenRouter.

The model identifier should be stored as:

```yaml
llm:
  provider: openrouter
  model: openai/gpt-5-mini
```

An equivalent configuration file or object is acceptable.

The model name and provider must never be hard-coded inside business logic.

## Rationale

Narrare is intended to support both Chinese and English novels.

The MVP does not optimize for the absolute best model for each language.

The MVP prioritizes:

- a single, stable model for the full pipeline
- strong multilingual performance across Chinese and English
- reliable structured output
- good instruction following
- reasonable inference cost
- easy future replacement

OpenRouter provides an OpenAI-compatible API while allowing the underlying model to be swapped later with minimal code changes.

## LLM Abstraction

Every pipeline stage must request an LLM through a shared service.

No stage should directly call OpenRouter, OpenAI, or any other provider.

```text
Dialogue Extraction
        │
Speaker Identification
        │
Emotion Annotation
        │
Pause Estimation
        │
Character Summarization
        │
Future Features
        │
      LLM Service
        │
   OpenRouter Adapter
        │
 OpenAI GPT-5 Mini
```

The rest of the application must remain unaware of which provider or model is being used.

Prompts, pipeline logic, UI components, and storage formats must not require changes when changing models.

## Future Compatibility

The architecture should allow replacing the model with a single configuration change.

Examples:

- `openai/gpt-5`
- `qwen/...`
- `deepseek/...`
- `anthropic/...`
- `google/...`

## Non-goal

Do not optimize model selection during the MVP.

The priority is to build a complete, reliable, end-to-end audiobook production workflow.

After the workflow is stable, Narrare can add a benchmarking framework to compare models for quality, latency, and cost.

Only then should individual pipeline stages migrate to different models, and only when the benefit is measurable.

---

# Artifact Model

Narrare should store every important intermediate artifact.

For a single source document, the workspace should eventually look like:

```text
data/interim/<project_id>/
  source_manifest.json
  chunks.json
  ir_segments.json
  characters.json
  voice_profiles.json
  voice_assignments.json
  generation_plan.json
  review_state.json
  assembly_manifest.json
  logs/
  audio/
    segments/
      <segment_id>/
        take_0001.wav
        take_0002.wav
    final/
      audiobook.wav
```

`source_manifest.json` stores:

- source file path
- source hash
- encoding
- character count
- created timestamp

`ir_segments.json` stores immutable text segments and metadata.

`review_state.json` stores human decisions.

`assembly_manifest.json` stores the exact approved takes and pause durations used for final assembly.

The original novel file is never edited.

---

# Stage 1 — Annotated Script Generation

## Input

TXT document.

Future versions will support:

- EPUB
- MOBI
- HTML
- Markdown

---

## Chunking

Long novels exceed LLM context windows.

Documents are split into chunks.

Each chunk includes overlapping context.

The overlap exists only for reasoning.

Overlapping text must never appear twice in the final output.

---

## Multi-pass LLM Pipeline

Rather than relying on one large prompt, Narrare uses multiple specialized passes.

Every pass uses the shared LLM Service.

For the MVP, every pass uses the configured OpenRouter model.

No pass may hard-code `openai/gpt-5-mini` or any other model name.

Detailed prompt contracts live in `prompts.md`.

This document describes the architecture-level intent.

### Pass 1 — Chunk Context & Character Profiler

Summarize surrounding context for the current chunk.

This improves later reasoning about speakers, aliases, pronouns, and emotional state.

Because this pass already views the chunk from a higher-level plot perspective, it also identifies active characters, stable aliases, contextual references, lightweight profile details, and evidence-backed character registry updates.

Stable aliases are globally safe identity names such as full names and stable short names.

Contextual references are local-only hints such as honorifics, role titles, relationship titles, pronouns, and descriptive phrases. Examples include `马丁先生`, `先生`, `小先生`, `我的当事人`, and `那个自由的机器人`. These references can help Stage 2 choose a speaker key in the current scene, but they must not become global replacement aliases.

TODO: use this pass to flag age or life-phase voice variants. If the same story identity appears as a child, adult, or older version across a long enough time span to need different voices, Narrare should represent those as linked voice characters such as `character`, `character_kid`, and `character_old`.

The summary is metadata only.

It never becomes audiobook text.

---

### Pass 2 — Script Converter

Convert raw novel text into immutable structured script segments.

This pass operates directly on one deterministic chunk.

Chunking is responsible for grouping natural paragraphs into LLM-sized request units, with default character bounds around 750-2,000 characters and a target around 1,500 characters.

For the MVP, each output-bearing script segment is a single speaker-keyed object.

Narration uses the reserved key `narrator`.

Character speech or thought uses the inferred character key.

Uncertain speech uses the reserved key `unknown_speaker`.

The MVP does not distinguish dialogue from internal monologue in the script IR.

Short narration, such as speech tags, remains output-bearing script text and must not be discarded.

The LLM does not return `segment_id` or `source_span`.

Code derives IDs and spans deterministically from the ordered script text.

Concatenating every segment text must reproduce the source chunk content after voice-content normalization.

Whitespace, indentation, line breaks, and punctuation are not voice-bearing content, so Stage 2 validation ignores them.

Only voice-bearing characters are strict reconstruction targets: Chinese characters, letters, and digits.

After content-match validation succeeds, code deterministically merges adjacent same-speaker segments by concatenating their text and expanding the span, then validates reconstruction again.

If a full-chunk Stage 2 attempt returns valid JSON but fails source/script alignment, the retry path may run a shrinking repair.

Shrinking repair is internal: code finds stable script anchors before and after the mismatch, expands the failed region to paragraph boundaries, asks the LLM to regenerate only that span, splices the repaired segments back into the chunk, and validates the whole chunk again.

If no stable prefix or suffix anchor exists, Stage 2 falls back to the normal whole-chunk retry.

After every chunk has a validated Stage 2 script artifact, code assembles one complete script artifact from the chunks in manifest order.

Assembly shifts chunk-local spans into complete-script coordinates.

If the final segment of one chunk and the first segment of the next chunk share the same speaker key, assembly merges them by concatenating text, expanding the span, preserving review notes, and keeping the lower confidence.

The complete script is validated against the concatenated chunk text.

---

### Pass 3 — Speaker Key Reviewer

After Pass 1, Pass 2, and deterministic assembly, Narrare reviews suspicious speaker keys with a key-only LLM pass.

The reviewer skips segments whose key is already a canonical character name or `narrator`.

It reviews aliases such as `安德鲁`, contextual references such as `马丁先生`, and unresolved keys such as `unknown_speaker`.

For each candidate, the reviewer receives:

- current script key-value pair;
- previous and next key-value pairs when available;
- Stage 1 scene context for the segment's source chunk;
- relevant character debriefs from the Stage 1 character registry;
- the allowed replacement keys.

It returns `keep`, `replace`, or `uncertain`.

Replacement keys must be canonical character names, `narrator`, or `unknown_speaker`.

Only high-confidence replacements are applied automatically.

Only metadata keys may change:

- `script` object keys may be renamed.
- `script` object values must remain exactly unchanged.
- `source_span` values must remain unchanged.
- `segment_id` values must remain unchanged.
- existing segment confidence values must remain unchanged.
- raw speaker keys should be preserved as audit metadata on changed segments.

Deterministic text integrity validation must still pass after key review.

The old deterministic `speaker-key-normalize` command remains available as a legacy/debug step for stable-alias registry checks, but it is no longer the recommended production flow.

---

### Pass 4 — Tone & Pause Annotator

Add TTS-oriented metadata.

Examples include:

- calm
- nervous
- sarcastic
- excited
- grieving
- pause duration
- speaking rate
- vocal intensity

---

### Pass 4 — Pronunciation & TTS Hint Generator

Generate optional pronunciation and glossary metadata.

This is useful for:

- Chinese polyphonic characters
- foreign names
- fantasy terminology
- abbreviations
- numbers
- dates
- invented words

This stage never changes script text.

---

### Deterministic Integrity Validation

Before scrutiny and human review, code must verify:

```text
concatenate(all segment text) == original chunk text
```

and eventually:

```text
concatenate(all final segment text) == original novel text
```

exactly.

The LLM may inspect validation reports, but deterministic equality checks are not delegated to the LLM.

---

### Pass 5 — Script Scrutinizer

Audit the generated IR, metadata, and deterministic validation report.

This stage finds suspicious speaker attribution, alias handling, tone labels, pause durations, and pronunciation hints.

It does not modify anything.

---

### Human Review

Human review happens after the generated IR and validation reports are available.

Users edit any metadata.

The original text cannot be edited.

---

# Intermediate Representation

The IR is the most important artifact.

Example:

```json
{
  "segment_id": "...",
  "source_span": [1024,1189],
  "speaker":"Harry",
  "text":"...",
  "emotion":"nervous",
  "pause_after_ms":300,
  "confidence":0.96
}
```

Only metadata changes.

Text remains immutable.

The IR should never contain normalized or rewritten text.

If a TTS adapter needs model-specific text cleanup, that cleanup must be stored separately as generation metadata and must not replace the IR text.

---

# Character Database

Characters become reusable objects.

Each character stores:

- canonical name
- aliases
- personality summary
- speaking style
- age impression
- voice variant notes
- voice assignment

Future:

- default speed
- default emotion
- pronunciation hints
- linked age or life-phase voice variants

---

# Stage 2

## Voice Assignment

The LLM summarizes every character.

This call also goes through the shared LLM Service.

The user selects one voice profile.

TODO: support linked age or life-phase variants for the same story identity. A character may require separate voice assignments for child, adult, and older versions when the novel spans a long enough time or explicitly changes the character's voice.

This mapping is stored.

Example

Harry

↓

Voice Profile 03

---

## Segment Generation

Generate one audio file per segment.

Never overwrite previous versions.

Each generated file is a take.

Takes are linked to:

- segment ID
- TTS adapter
- model path or model identifier
- voice profile
- generation parameters
- random seed when supported
- source IR version
- created timestamp

The current Qwen experiments use `seg_###.wav`.

Narrare should move to stable segment IDs internally, while still allowing export to ordered `seg_###.wav` names when useful.

---

## Human Review

Interface

Column 1

Original Novel

Column 2

Annotated Script

Column 3

Generated Audio

Users may

- play
- replay
- approve
- reject
- regenerate

---

## Regeneration

Rejected segments enter a queue.

Regeneration may change

- random seed
- sampling

Future

- speaking rate
- pitch
- emotion
- style

---

## Version History

Every generated audio is preserved.

Users can compare multiple versions.

Approved versions become active.

---

# Qwen TTS Integration

The Qwen projects are integration references, not Narrare architecture.

Useful Qwen APIs:

- `Qwen3TTSModel.from_pretrained(...)`
- `generate_voice_clone(...)`
- `create_voice_clone_prompt(...)`
- `generate_custom_voice(...)`
- `generate_voice_design(...)`

Useful Qwen artifacts:

- local model directories under `Qwen3-Audiobook-Studio-v1.0-lite/models/`
- saved `.pt` voice prompt files under `Qwen3-Audiobook-Studio-v1.0-lite/voices/`
- raw voice samples under `data/voices/`

Qwen adapter responsibilities:

- load and unload Qwen models
- create reusable voice prompts from raw audio samples
- synthesize one segment at a time from IR text
- return audio data or write a take file
- report model metadata and generation parameters

Qwen adapter non-responsibilities:

- parse novels
- mutate IR text
- manage UI state
- decide approval/rejection
- assemble final audiobooks

The Gradio apps currently mix all of these concerns.

Narrare must separate them.

---

# Final Assembly

Only approved segments are assembled.

Assembly performs

- pause insertion
- concatenation

The final assembly should consume only `assembly_manifest.json`.

It should not read the original novel.

It should not call an LLM.

It should not call TTS.

It should fail if any required segment lacks an approved take.

Future

- loudness normalization
- denoise
- ambience matching
- chapter music
- mastering

---

# Future Features

## Expressive TTS

Use emotion metadata.

Examples

happy

sad

whisper

shouting

---

## Chapter Atmosphere

Generate chapter-level summaries.

Example

Winter night.

Quiet.

Melancholic.

Sparse piano.

Low strings.

This prompt can drive music generation.

---

## Background Music

Chapter

↓

Prompt

↓

Music Generator

↓

Loop Generation

↓

Background Mixing

---

## Audio Normalization

Different voice samples produce different acoustic signatures.

Normalize

- noise floor
- loudness
- EQ

before mixing.

---

# Implementation Plan

## Phase 0 — Repository Cleanup and Boundaries

Goal:

Make the project boundaries explicit without deleting experimental material.

Tasks:

- create the target package directories
- keep downloaded Qwen projects isolated as third-party references
- document which existing folders are source data, generated data, experiments, and external code
- add centralized configuration for LLM provider/model settings
- add Python project metadata
- add Ruff, Black, and basic test configuration

Expected result:

The repository has a clean Narrare source tree and a single configuration layer while preserving current experiments.

---

## Phase 1 — Core Data Models

Goal:

Define the objects that every future stage will share.

Initial Pydantic models:

- `SourceDocument`
- `SourceSpan`
- `Chunk`
- `IRSegment`
- `Character`
- `VoiceProfile`
- `VoiceAssignment`
- `GenerationRequest`
- `AudioTake`
- `SegmentReview`
- `AssemblyManifest`
- `LLMConfig`
- `TTSConfig`

Important rules:

- `IRSegment.text` must match the source span after voice-content normalization for Stage 2 script conversion.
- metadata must be editable separately from text.
- segment IDs must be stable.
- every artifact must be JSON serializable except binary audio files.
- provider and model names must be configuration data, not business logic.

Expected result:

The project has a typed IR contract before any model calls are added.

---

## Phase 2 — TXT Loader and Integrity Validation

Goal:

Load TXT input and prove that generated segment text still reconstructs the original.

Tasks:

- load `data/raw/两百岁的寿星1.txt`
- detect encoding explicitly
- store source hash
- create a simple initial segmentation strategy
- validate `concatenate(segment.text) == source_text`
- write artifacts into `data/interim/<project_id>/`

Expected result:

Narrare can create and validate a minimal IR without LLM or TTS.

---

## Phase 3 — Manual IR Review Baseline

Goal:

Support human-in-the-loop work before full automation.

Tasks:

- create a reviewable JSON format for segments
- allow manual edits to speaker, emotion, pause, and confidence
- forbid edits to original segment text
- produce a regeneration-safe `review_state.json`

Expected result:

A user can inspect and correct metadata while the source text remains immutable.

---

## Phase 4 — LLM Service Abstraction

Goal:

Create the shared LLM entry point used by every LLM pipeline stage.

Tasks:

- define an `LLMService` interface
- define request and response models for structured JSON output
- define an `OpenRouterAdapter`
- load provider and model from centralized configuration
- set OpenRouter as the MVP provider
- set `openai/gpt-5-mini` as the default model
- ensure the chunk context and character profiler, script converter, tone annotator, pronunciation hint generator, and script scrutinizer use the shared service
- forbid direct provider calls in pipeline stages

Expected result:

All LLM stages can use OpenRouter through one abstraction, and changing models is a configuration-only operation.

---

## Phase 5 — TTS Adapter Interface

Goal:

Abstract TTS engines before integrating Qwen.

Tasks:

- define a `TTSAdapter` protocol
- define `synthesize_segment(request) -> AudioTake`
- define voice profile loading
- define generation parameter models
- add a dummy TTS adapter for tests

Expected result:

Core generation workflow can be tested without Qwen, torch, or model weights.

---

## Phase 6 — Qwen Adapter Extraction

Goal:

Wrap Qwen TTS functionality in Narrare's adapter interface.

Tasks:

- load local Qwen model paths from configuration
- load saved `.pt` voice prompts
- generate one audio take for one IR segment
- save take files under the Narrare artifact structure
- record model path, voice profile, parameters, and seed
- avoid importing Gradio app code

Expected result:

Narrare can generate segment audio through Qwen while core logic remains model-agnostic.

---

## Phase 7 — Regeneration Queue

Goal:

Replace the notebook missing-file workflow with explicit review state.

Tasks:

- mark segment takes as approved or rejected
- create regeneration requests for rejected segments
- preserve rejected takes
- generate new takes without overwriting old ones
- select one active take per approved segment

Expected result:

Manual deletion of bad WAV files is no longer part of the workflow.

---

## Phase 8 — Final Assembly

Goal:

Build the final audiobook from approved takes only.

Tasks:

- read `assembly_manifest.json`
- verify every segment has an approved active take
- insert pause durations
- concatenate WAV files
- write output to `data/processed/<project_id>/`
- write assembly logs

Expected result:

The final output is reproducible from stored artifacts.

---

## Phase 9 — UI

Goal:

Create the human review surface after the data and workflow are stable.

Initial UI should show:

- original text segment
- metadata fields
- assigned voice
- generated takes
- audio playback
- approve/reject controls
- regenerate action

Strict boundary:

UI calls Narrare services.

UI does not call Qwen, LLMs, or audio assembly directly.

---

# Future Compatibility

## Multi-language

Chinese

English

Future:

Japanese

---

## Supported Formats

TXT

↓

EPUB

↓

MOBI

↓

HTML

↓

Markdown

---

# Non-goals

Narrare does not aim to

- train foundation models
- become another TTS framework
- replace human narration

It aims to become the best human-AI audiobook production workflow.

---

# Guiding Principle

AI models will continue to evolve.

The workflow should remain stable.

The workflow is the product.
