# AGENTS.md

## Project Philosophy

Narrare is a workflow-centric audiobook production system.

The objective is NOT to build the best LLM, the best Text-to-Speech model, or the most automated pipeline.

The objective is to build a reliable, reproducible, human-in-the-loop production workflow for turning novels into high-quality audiobooks.

Every AI component should be considered replaceable.

The workflow is the product.

---

## Core Principles

### 1. Original text is immutable.

The original novel is the single source of truth.

No AI stage may modify, rewrite, summarize, normalize, or otherwise alter the original text.

Metadata must always be stored separately.

---

### 2. Human review is first-class.

The project intentionally favors human validation over full automation.

Every AI-generated output should be reviewable.

Every important stage should be interruptible.

Nothing should assume AI is always correct.

---

### 3. Reproducibility

Every stage should be deterministic whenever possible.

All intermediate artifacts should be stored.

The pipeline should be restartable from any stage.

---

### 4. Modular AI

LLMs, TTS engines, music generators, and post-processing models are plugins.

Business logic must never depend on one specific model.

---

### 5. Structured Intermediate Representation (IR)

The structured script generated from the novel is the central artifact of the project.

Everything else consumes this IR.

Future modules should never parse the original novel again.

---

## Engineering Guidelines

Prefer:

- small modules
- explicit interfaces
- JSON serializable objects
- reproducible outputs
- incremental processing

Avoid:

- giant monolithic pipelines
- hidden global state
- tightly coupled model APIs
- modifying original text

---

## Development Priorities

Priority 1

Correctness.

Priority 2

Human usability.

Priority 3

Extensibility.

Priority 4

Performance.

---

## Coding Style

- Python 3.12+
- Type hints everywhere
- Pydantic models where appropriate
- Black formatting
- Ruff linting

---

## Directory Philosophy

/core

Business logic.

No UI.

No model-specific code.

---

/llm

LLM adapters.

---

/tts

TTS adapters.

---

/ui

Human review interface.

---

/storage

Intermediate artifacts.

---

/docs

Documentation.

---

Never allow UI logic inside pipeline modules.

Never allow AI model calls inside GUI components.

Everything should communicate through well-defined data models.

---

## Current Development Scope

Ignore the webapp for now.

Do not work on `/ui/web`, FastAPI routes, browser templates, frontend assets, or local dashboard behavior unless the user explicitly asks for webapp work.

When making pipeline changes, first identify the pipeline stage being discussed. State the stage in your working notes or final response, then inspect and modify only code, prompts, tests, and documentation that are directly relevant to that stage.

Whenever implementing or changing a pipeline stage, update `README.md` with the latest terminal command that runs the implemented stage or stages. The README command should match the current CLI/module entry point and required arguments.

Avoid broad cross-stage refactors. If a requested change appears to touch multiple stages, separate the stage-specific work and explain the boundary before editing.

---

## Long-term Vision

Narrare should eventually become a general AI-assisted audiobook production platform capable of integrating different LLMs, TTS systems, music generators, and post-processing tools while preserving a single human-centered workflow.
