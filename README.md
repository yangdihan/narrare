# Narrare

Narrare is a local-first audiobook production pipeline.

Unlike traditional one-click audiobook generators, Narrare focuses on producing high-quality audiobooks through AI-assisted workflows and human review.

The goal is to let AI perform repetitive work while humans remain responsible for creative judgment.

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
