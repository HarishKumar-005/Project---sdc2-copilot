# Antigravity Project Setup — SCD2 Copilot

This repository uses Antigravity as a development agent, not as part of the production application architecture.

## Project-level setup

Open the SCD2 Copilot repository as an Antigravity Project.

Keep the Project filesystem scope limited to the repository and any explicitly required development folders.

## Repository customization

The repository contains:

```text
AGENTS.md
.agents/
├── rules/
│   ├── architecture.md
│   ├── testing.md
│   ├── polars.md
│   └── ai-boundary.md
└── skills/
    ├── repository-audit/
    │   └── SKILL.md
    ├── scd2-correctness-audit/
    │   └── SKILL.md
    └── polars-performance/
        └── SKILL.md
```

Antigravity Skills should live under `.agents/skills/<skill-name>/SKILL.md`.

## Recommended workflow

Use:

```text
Explore
  ↓
Plan
  ↓
Review
  ↓
Execute
  ↓
Verify
  ↓
Inspect diff
```

For large refactors, use an isolated Git worktree.

## Permissions

Prefer review-gated execution for architectural work.

Do not grant unrestricted host access merely for convenience.

Use sandboxed execution where it is practical and compatible with the command being run.

Do not store secrets, API keys, credentials, or machine-specific permission configuration in this repository.

## When to use subagents

Subagents are optional development helpers.

Use them for genuinely separable investigations such as:

```text
correctness audit
performance audit
test audit
```

Do not turn SCD2 Copilot itself into a multi-agent product architecture.

## Features deliberately not configured here

The repository does not currently require:

- MCP configuration
- custom plugins
- sidecars
- global workflows
- custom hooks
- teamwork agent teams

Add them only when an actual engineering requirement appears.

## Skills vs workflows

Prefer Skills for reusable agent workflows. Do not add new legacy workflow files unless there is a specific compatibility reason.
