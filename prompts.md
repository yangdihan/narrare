# Prompt Design

This file defines the detailed prompt-stage contract for the multi-pass LLM pipeline described in `design.md`.

`design.md` owns the architecture-level idea.

`prompts.md` owns the detailed stage responsibilities, inputs, outputs, and prompt drafts.

---

# MVP Model Decision

For the MVP, every LLM stage uses the same configured model.

- Provider: OpenRouter
- Model: `openai/gpt-5-mini`

This model choice must come from the centralized configuration layer.

No prompt, pipeline stage, UI component, or storage format should hard-code the provider or model.

The implementation should be equivalent to:

```yaml
llm:
  provider: openrouter
  model: openai/gpt-5-mini
```

The objective is to complete a reliable end-to-end audiobook production workflow before optimizing individual stages with different models.

Model benchmarking is explicitly deferred until after the first fully working pipeline is complete.

---

# Pipeline Philosophy

The LLM pipeline is a sequence of specialized reasoning stages rather than a single monolithic prompt.

Each stage has one clear responsibility.

Each stage produces structured JSON outputs that become inputs to subsequent stages.

The original novel is immutable throughout the pipeline.

Every inference produced by an LLM is stored as metadata alongside the original text rather than modifying it.

---

# Shared Rules

These rules apply to every prompt.

- The original novel is the single source of truth.
- Never rewrite, summarize, normalize, translate, correct, or otherwise alter original text.
- Never change punctuation, whitespace, line breaks, quotation marks, or wording.
- Every response must be valid JSON.
- Do not wrap JSON in Markdown.
- If uncertain, lower the confidence score instead of hallucinating.
- All inferred information must be represented as metadata.
- Include confidence scores and review notes whenever appropriate.
- Prefer explicit `null` or empty arrays over invented values.
- Deterministic validation results must be respected when provided.

---

# Stage 1 — Chunk Context Summarizer

## Purpose

Long novels exceed LLM context windows.

Before processing a chunk, Narrare generates a concise context summary so later stages can reason about the current scene.

This stage exists only to improve reasoning.

Its output never becomes part of the final audiobook.

## Input

- previous chunk summary
- previous overlap
- current chunk
- next overlap
- existing character registry

## Output

The stage should generate:

- current scene summary
- active characters
- aliases observed
- current emotional state
- unresolved pronouns
- important context
- confidence
- review notes

## Prompt Draft

```text
You are processing one chunk of a novel for an audiobook production pipeline.

Summarize only the information required for understanding the current chunk.

Do not rewrite the novel.
Do not invent plot details.
Do not add information that is not supported by the provided text or existing registry.

Focus on:

- active scene
- current speakers
- known aliases
- unresolved references
- relationships
- emotional state

Return valid JSON only.
```

---

# Stage 2 — Script Converter

## Purpose

Convert raw novel text into structured script segments.

This is the most important LLM stage.

The generated segment text must preserve the original chunk exactly.

## Hard Constraints

- Concatenating every generated `original_text` value must reproduce the original chunk exactly.
- No characters may be changed.
- No punctuation may be changed.
- No whitespace may be changed.
- No line breaks may be changed.

## Input

- current chunk
- chunk start offset
- context summary
- character registry

## Output

Each segment contains:

- `segment_id`
- `source_span`
- `segment_type`
- `speaker_raw`
- `original_text`
- `confidence`
- `review_notes`

Segment types:

- `narration`
- `dialogue`
- `internal_monologue`

## Prompt Draft

```text
Convert the novel chunk into structured script segments.

Preserve every character exactly.

Assign each segment to one of:

- narration
- dialogue
- internal_monologue

Infer the most likely speaker.

If uncertain, choose the best candidate while lowering confidence and adding a review note.

Do not normalize names.
Do not rewrite dialogue.
Do not trim or alter whitespace.

Return valid JSON only.
```

---

# Stage 3 — Character Canonicalizer & Profiler

## Purpose

Normalize character references and maintain a global character registry.

This stage prevents the same character from appearing under multiple names.

Example:

```text
Harry Potter
Harry
Potter
The Boy Who Lived
```

may all map to:

```text
character_001
```

when evidence strongly supports the merge.

This stage also builds lightweight character profiles.

## Input

- script segments
- existing character registry
- context summary

## Output

Character registry updates:

- `character_id`
- `canonical_name`
- `aliases`
- `persona_summary`
- `speaking_style`
- `age_impression`
- `confidence`
- `review_notes`

Segment updates:

- `segment_id`
- `canonical_speaker_id`
- `confidence`
- `review_notes`

## Prompt Draft

```text
Maintain the character registry for this audiobook script.

Merge aliases only when strongly supported by the provided text, existing registry, or context summary.

Do not merge uncertain identities.

Generate concise persona and speaking-style summaries based only on evidence already seen.

For uncertain speaker identities, keep the original raw speaker metadata and add a review note.

Return valid JSON only.
```

---

# Stage 4 — Tone & Pause Annotator

## Purpose

Generate metadata that improves TTS quality.

This stage never changes text.

## Input

- script segments
- context summary
- character registry

## Output

For every segment:

- `segment_id`
- `emotion`
- `delivery_note`
- `pause_after_ms`
- `speaking_rate`
- `intensity`
- `confidence`
- `review_notes`

## Pause Guidelines

- 100-200 ms: small continuation
- 300-500 ms: normal sentence break
- 600-900 ms: paragraph break or emotional beat
- 1000+ ms: scene transition or dramatic pause

## Prompt Draft

```text
Annotate every segment for audiobook narration.

Infer:

- emotion
- delivery note
- pause duration
- speaking rate
- vocal intensity

Keep labels simple and practical for TTS.

Never modify the text.

If the emotional state is ambiguous, choose a neutral or low-intensity label and lower confidence.

Return valid JSON only.
```

---

# Stage 5 — Pronunciation & TTS Hint Generator

## Purpose

Generate optional pronunciation metadata for TTS.

This is especially useful for:

- Chinese polyphonic characters
- foreign names
- fantasy terminology
- abbreviations
- numbers
- dates
- invented words

This stage never changes script text.

## Input

- script segments
- character registry
- existing glossary
- language profile

## Output

Pronunciation hints:

- `term`
- `pronunciation`
- `language`
- `applicable_segment_ids`
- `confidence`
- `review_notes`

Glossary updates:

- `term`
- `pronunciation`
- `description`
- `confidence`
- `review_notes`

## Prompt Draft

```text
Generate pronunciation hints only when necessary.

Do not rewrite the script.

Identify words likely to be pronounced incorrectly by a TTS model.

For Chinese, generate pinyin when helpful.

For foreign names, provide a practical pronunciation hint.

If uncertain, add a review note rather than guessing.

Return valid JSON only.
```

---

# Stage 6 — Script Scrutinizer

## Purpose

Audit the generated Intermediate Representation.

This stage validates semantic consistency rather than performing deterministic checks.

Exact text equality should be verified by conventional code.

## Input

- original chunk
- script IR
- character registry
- tone annotations
- pronunciation hints
- deterministic validation report

## Output

- `overall_status`
- `issues`
- `warnings`
- `suggested_fixes`
- `human_review_requirements`

## Prompt Draft

```text
Review the generated audiobook script.

Do not modify anything.

Inspect:

- speaker attribution
- character consistency
- alias consistency
- pause durations
- emotion labels
- pronunciation hints

Use deterministic validation results when available.

Flag suspicious cases.

When suggesting a fix, describe the metadata field that should be reviewed.
Do not rewrite original text.

Return valid JSON only.
```

---

# Pipeline Overview

```text
Novel
  |
Chunk Context Summarizer
  |
Script Converter
  |
Character Canonicalizer & Profiler
  |
Tone & Pause Annotator
  |
Pronunciation & TTS Hint Generator
  |
Deterministic Integrity Validation
  |
Script Scrutinizer
  |
Human Review
  |
Annotated Intermediate Representation (IR)
  |
Voice Assignment
  |
Segment TTS Generation
  |
Human Audio Review
  |
Regeneration Loop
  |
Final Audio Assembly
```

---

# Future Extensions

The pipeline is intentionally designed so additional stages can be inserted without affecting earlier ones.

Potential future stages:

- chapter atmosphere summarizer
- background music prompt generator
- voice casting recommendation
- character relationship graph builder
- story timeline extraction
- chapter recap generation
- audio quality evaluator
- automatic TTS benchmarker
- multi-model ensemble validation

Each new stage should consume the Intermediate Representation rather than reparsing the original novel.
