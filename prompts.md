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
- Never change voice-bearing characters or wording.
- Whitespace, indentation, line breaks, and punctuation may be omitted in Stage 2 script output because they are not voice-bearing content.
- Every response must be valid JSON.
- Do not wrap JSON in Markdown.
- If uncertain, lower the confidence score instead of hallucinating.
- All inferred information must be represented as metadata.
- Include confidence scores and review notes whenever appropriate.
- Prefer explicit `null` or empty arrays over invented values.
- Deterministic validation results must be respected when provided.

---

# Stage 1 — Chunk Context & Character Profiler

## Purpose

Long novels exceed LLM context windows.

Before processing a chunk, Narrare generates a concise context summary so later stages can reason about the current scene.

While reasoning about the chunk at this higher level, the same pass also identifies active characters, observed aliases, lightweight character traits, and evidence-backed registry updates.

This stage exists only to improve reasoning.

Its output never becomes part of the final audiobook.

Its character observations are metadata used to update the character registry and to help later stages choose speaker labels.

The source text remains immutable.

## Input

- previous chunk summary
- previous overlap
- current chunk
- next overlap
- existing character registry

## Output

Context output:

- current scene summary
- active characters
- aliases observed
- current emotional state
- unresolved pronouns
- important context
- confidence
- review notes

Character registry update output:

- `character_id` or `proposed_character_id`
- `canonical_name`
- `aliases`
- `alias_evidence`
- `persona_summary`
- `speaking_style`
- `age_impression`
- `voice_variant_notes`
- `confidence`
- `review_notes`

Alias evidence should be structured enough for deterministic code to generate a dictionary like:

```json
{
  "character_001": {
    "canonical_name": "Harry Potter",
    "aliases": ["Harry Potter", "Harry", "Potter", "The Boy Who Lived"]
  }
}
```

Do not use this stage to attach canonical speaker IDs to script segments. Segment-level speaker attribution belongs to Stage 2.

TODO: detect age or time-span voice variants during this stage. If the same story identity appears at meaningfully different ages or life phases that require different voices, the profiler should flag voice-split candidates such as `character`, `character_kid`, and `character_old`. These variants are still linked to the same story identity, but they may need separate audiobook character records for voice assignment.

## Prompt Draft

```text
You are processing one chunk of a novel for an audiobook production pipeline.

Summarize only the information required for understanding the current chunk.

Do not rewrite the novel.
Do not invent plot details.
Do not add information that is not supported by the provided text or existing registry.

While summarizing the chunk, also maintain character metadata.

Focus on:

- active scene
- current speakers
- active characters
- aliases observed in this chunk
- alias-to-character evidence
- unresolved references
- relationships
- emotional state
- concise persona and speaking-style evidence
- possible age or life-phase voice variants

Merge aliases only when strongly supported by the provided text, overlap context, or existing registry.
Do not merge uncertain identities.
If one story identity appears across significantly different ages or life phases, flag this as a voice-variant candidate instead of forcing one voice profile.
If uncertain, keep aliases separate, lower confidence, and add a review note.
Return structured character observations that deterministic code can convert into a character-to-alias dictionary.
Return valid JSON only.
```

---

# Stage 2 — Script Converter

## Purpose

Convert raw novel text into structured script segments.

This is the most important LLM stage.

The generated segment text must preserve the original chunk after voice-content normalization.

Stage 2 runs directly on one deterministic chunk.

Chunking groups natural paragraphs into LLM-sized request units before Stage 2.

Narrare derives `segment_id` and `source_span` deterministically after the LLM returns ordered script text.

For the MVP, the output script does not distinguish dialogue from internal monologue.

Each output-bearing script segment is represented as a single speaker-keyed object:

```json
{"character": "content"}
```

Narration is always represented with the reserved speaker key `narrator`:

```json
{"narrator": "content"}
```

Short narration and attribution text must still be preserved as its own script content.

For example, `他说` must appear as narrator content and must not be dropped, merged into metadata, or rewritten.

## Hard Constraints

- Concatenating the single content value from every generated `script` object must reproduce the original chunk content after voice-content normalization.
- No voice-bearing characters may be changed.
- Whitespace, indentation, line breaks, and punctuation may be omitted or changed because they are not voice-bearing content.
- Every original voice-bearing character must appear in exactly one output-bearing script segment.
- Do not omit short narration, speech tags, or attribution phrases.
- Whitespace-only and punctuation-only spans may be omitted.
- Do not ask the LLM to return numeric spans.

## Input

- current chunk
- known character roster from prior validated chunks
- last 3 validated script segments
- context summary, when available

## Output

Each segment contains:

- `script`
- `confidence`
- `review_notes`

`script` must be an object with exactly one key.

The key is either:

- `narrator`
- `unknown_speaker`
- the raw character or speaker label inferred from the text

The value is original voice-bearing text copied from the chunk.

Code will assign `segment_id` and `source_span` after validation.

After content-match validation passes, Narrare deterministically merges consecutive segments with the same speaker key by concatenating their text values.

The merged result is validated again while ignoring whitespace and punctuation.

Example:

```json
{
  "segments": [
    {
      "script": {"narrator": "安德鲁·马丁说，"},
      "confidence": 0.98,
      "review_notes": []
    },
    {
      "script": {"安德鲁·马丁": "“谢谢，”"},
      "confidence": 0.95,
      "review_notes": []
    }
  ]
}
```

## Prompt Draft

```text
Convert the script chunk into structured script segments.

Preserve original content exactly.

Each output-bearing segment must contain a `script` object with exactly one key and one value.

If the text is narration, use the reserved key `narrator`.

If the text is spoken or thought by a character, use the best raw character or speaker label as the key.

If the speaker is uncertain, use the reserved key `unknown_speaker`.

Infer the most likely speaker.

If uncertain, choose the best candidate while lowering confidence and adding a review note.

Preserve even very short narration, including speech tags such as `他说`.

Whitespace before dialogue, including blank lines and full-width indentation, is optional in Stage 2 output.

Punctuation is also optional in Stage 2 output.

Never use attribution phrases such as `他说` or `她问道` as speaker keys.

Do not normalize names.
Do not rewrite text.
Do not change voice-bearing text.

Return valid JSON only.
```

## Deterministic Post-processing

After Stage 2 output passes content reconstruction validation:

- merge adjacent segments with the same `script` key;
- concatenate their script text values without adding or removing voice-bearing characters;
- expand the merged `source_span` to cover the original adjacent spans;
- set merged confidence to the minimum confidence of the merged segments;
- preserve review notes;
- validate content reconstruction again while ignoring whitespace and punctuation.

---

# Deterministic Step — Speaker Key Normalizer

## Purpose

After Stage 1 and Stage 2 are complete, Narrare should deterministically unify script speaker keys for aliases that refer to the same character.

Stage 2 may output raw speaker keys such as:

```json
{"Harry": "text"}
{"Potter": "text"}
{"The Boy Who Lived": "text"}
```

If Stage 1 produced evidence-backed registry data proving these labels refer to the same character, this deterministic step renames the script keys to one canonical key before later stages consume the script.

This is not an LLM stage.

## Input

- Stage 1 character registry updates
- existing character registry
- Stage 2 script segments

## Output

- script segments with normalized speaker keys
- alias-to-canonical rename report
- review notes for unresolved or ambiguous aliases

## Hard Constraints

- Only the `script` object key may be renamed.
- The `script` object value must remain byte-for-byte identical.
- `source_span` must remain unchanged.
- `segment_id` must remain unchanged.
- `narrator` must never be renamed.
- Ambiguous aliases must not be renamed automatically.
- The raw speaker key should be preserved as metadata for audit and human review.
- Deterministic text integrity validation must still pass after key normalization.

Example:

```json
{
  "segment_id": "seg_000042",
  "source_span": {"start": 120, "end": 128},
  "script": {"Harry Potter": "text"},
  "raw_script_key": "The Boy Who Lived",
  "speaker_key_normalization": {
    "from": "The Boy Who Lived",
    "to": "Harry Potter",
    "source": "character_registry",
    "confidence": 0.97
  },
  "confidence": 0.94,
  "review_notes": []
}
```

---

# Stage 3 — Tone & Pause Annotator

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

# Stage 4 — Pronunciation & TTS Hint Generator

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

# Stage 5 — Script Scrutinizer

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
Chunk Context & Character Profiler
  |
Script Converter
  |
Speaker Key Normalizer
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
- age and life-phase voice variant detection
- character relationship graph builder
- story timeline extraction
- chapter recap generation
- audio quality evaluator
- automatic TTS benchmarker
- multi-model ensemble validation

Each new stage should consume the Intermediate Representation rather than reparsing the original novel.
