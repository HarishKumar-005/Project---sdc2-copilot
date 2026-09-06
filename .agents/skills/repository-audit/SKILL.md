---
name: repository-audit
description: Audit the existing SCD2 Copilot repository before making architectural or refactoring changes. Use at the beginning of a milestone, when supplied project context may be stale, or when asked to establish the current implementation baseline.
---

# Repository Audit Skill

## Purpose

Build a verified picture of the repository before changing code.

This is an investigation skill, not an implementation skill.

## Required behavior

Do not modify application code during the audit unless explicitly requested.

## Audit sequence

### 1. Map the repository

Inspect:

- top-level files
- source packages
- application entry points
- tests
- benchmark files
- configuration
- dependencies
- documentation
- deployment files
- CI configuration

### 2. Identify actual runtime paths

Trace:

- Streamlit entry point
- application service calls
- Prefect flow/task definitions
- deterministic engine
- validation
- AI provider selection
- persistence
- exports

Determine what code is actually executed.

### 3. Compare claims with reality

For each claimed feature classify:

- IMPLEMENTED
- PARTIALLY IMPLEMENTED
- MISSING
- BROKEN
- STALE / UNUSED

Always cite the relevant files and symbols in the report.

### 4. Establish test baseline

Run the repository's documented routine test command.

Do not include large performance benchmarks in the baseline unless the repository explicitly requires them.

Record:

- Python version
- package/tool versions
- collected tests
- passing/failing count
- runtime
- notable warnings/errors

### 5. Establish performance baseline

Only if a benchmark suite exists and the task includes baseline measurement:

- identify benchmark commands
- avoid modifying benchmark semantics
- record measured results

### 6. Produce an audit report

Summarize:

- actual architecture
- actual execution path
- implemented features
- verified bugs
- unused/dead components
- testing baseline
- performance baseline
- recommended next task

## Rules

Do not infer hidden capabilities.

Do not invent files.

Do not rewrite code because you found a better architecture.

Do not use the supplied project-context documents as proof when repository evidence is available.

The repository is the source of truth.
