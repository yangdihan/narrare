# Narrare Development Plan

## Summary

Build Narrare as a Python local-first audiobook workflow with strict separation between core workflow logic, LLM adapters, TTS adapters, storage, and UI.

Use the existing `.venv` whenever possible. The current environment is Python 3.11 and already includes the runtime libraries needed for the first milestone.

The implementation should follow:

```text
TXT Novel
-> Source Manifest
-> Chunks
-> Multi-pass LLM IR
-> Speaker Key Normalization
-> Deterministic Validation
-> Human Metadata Review
-> Voice Assignment
-> Segment TTS Takes
-> Human Audio Review
-> Regeneration Queue
-> Assembly Manifest
-> Final Audiobook
```

The first implementation milestone is chunking `data/raw/两百岁的寿星1.txt` into `data/interim/bicentennial_man/` for manual inspection.

## Module And Stage Map

```text
config/
  default.yaml
  loader.py
  models.py

core/
  models/
    source.py
    chunk.py
    ir.py
    character.py
    voice.py
    generation.py
    review.py
    assembly.py
    validation.py
  document/
    txt_loader.py
    manifest.py
  chunking/
    chunker.py
  pipeline/
    annotated_script.py
    speaker_key_normalization.py
    voice_assignment.py
    segment_generation.py
    regeneration.py
    final_assembly.py
  validation/
    text_integrity.py
    artifact_integrity.py
  review/
    metadata_review.py
    audio_review.py
  audio/
    wav_io.py
    silence.py
    concatenate.py

llm/
  service.py
  schemas.py
  prompts/
    chunk_context_profiler.py
    script_converter.py
    tone_pause.py
    pronunciation.py
    script_scrutinizer.py
  adapters/
    openrouter.py

tts/
  service.py
  schemas.py
  adapters/
    dummy.py
    qwen.py

storage/
  workspace.py
  json_store.py
  artifact_paths.py

cli/
  main.py
```

## First Milestone: Chunking

Command:

```bash
.venv/bin/python -m cli.main chunk data/raw/两百岁的寿星1.txt --project-id bicentennial_man
```

Outputs:

```text
data/interim/bicentennial_man/source_manifest.json
data/interim/bicentennial_man/chunks.json
data/interim/bicentennial_man/validation_report.json
data/interim/bicentennial_man/chunks/chunk_0001.txt
```

Chunking defaults:

```yaml
llm:
  provider: openrouter
  model: openai/gpt-5-mini
  context_window_tokens: 128000
  chunking:
    target_chunk_tokens: 3000
    min_chunk_chars: 750
    target_chunk_chars: 1500
    max_chunk_chars: 2000
    overlap_tokens: 500
    reserved_prompt_tokens: 12000
    reserved_output_tokens: 24000
    reserved_registry_tokens: 8000
    contingency_ratio: 0.25
```

The current chunk text is the only output-bearing source text. Previous and next overlaps are reasoning context only and must never be duplicated in reconstruction.

## Test Plan

Run:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m cli.main chunk data/raw/两百岁的寿星1.txt --project-id bicentennial_man
```

Test:

- TXT loader preserves exact text.
- Source manifest hash matches loaded text.
- Estimated token count is deterministic.
- Chunk source spans are contiguous and non-overlapping.
- Context spans stay within source bounds.
- `''.join(chunk.text for chunk in chunks) == source_text`.
- CLI writes expected files under `data/interim/bicentennial_man/`.

## Assumptions

- Stay CLI-first until workflow services are stable.
- Do not implement LLM calls in the chunking milestone.
- Do not implement TTS in the chunking milestone.
- Do not import notebook code.
- Do not import Qwen GUI code.
- Keep `data/` ignored by Git; generated milestone files are local inspection artifacts.
- Use Python 3.11 from the existing `.venv` for now, despite Python 3.12+ as the long-term target.
