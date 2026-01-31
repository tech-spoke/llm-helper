# Code Intelligence MCP Server - Design Document v1.11

> This document describes the complete system specification as of v1.11.
> For version history, see [CHANGELOG](#changelog).

---

## Table of Contents

1. [Overview](#overview)
2. [Design Philosophy](#design-philosophy)
3. [Architecture](#architecture)
4. [Complete Flow (19 Steps)](#complete-flow-19-steps)
5. [Phase Matrix](#phase-matrix)
6. [submit_phase API](#submit_phase-api)
7. [Task Orchestration](#task-orchestration)
8. [Safety Valves (Loop Prevention)](#safety-valves-loop-prevention)
9. [Compaction Resilience Design](#compaction-resilience-design)
10. [Two-Layer Context](#two-layer-context)
11. [Tools Reference](#tools-reference)
12. [Setup Guide](#setup-guide)
13. [Configuration](#configuration)
14. [Internal Reference](#internal-reference)
15. [CHANGELOG](#changelog)

---

## Overview

Code Intelligence MCP Server provides guardrails for LLMs to accurately understand codebases and safely implement changes.

| Caller | Default Behavior |
|--------|------------------|
| **Cursor** | Understands entire codebase before modifying |
| **Claude Code** | Tends to modify only specific locations |

This server makes Claude Code behave more like Cursor by enforcing structured exploration before implementation.

### Key Changes in v1.11

| Change | Description |
|--------|-------------|
| **Unified submit_phase** | 17 submit_* tools → single `submit_phase` |
| **Self-describing responses** | Every response includes `instruction` + `expected_payload` |
| **Task orchestration** | Server-enforced task management (LLM cannot skip) |
| **Compaction resilience** | 4-layer resilience design (tool, response, recovery, defense) |

---

## Design Philosophy

```
Don't let the LLM decide. Design so it can't proceed without compliance.
And have a mechanism to learn from failures.
```

### Core Principles

| Principle | Implementation |
|-----------|----------------|
| **Phase enforcement** | Tool restrictions per phase (no shortcuts) |
| **Server evaluation** | Server calculates confidence (not LLM self-report) |
| **Quote verification** | Validates LLM-extracted quotes against original query |
| **Embedding verification** | Objective NL→Symbol relevance via vector similarity |
| **Write restriction** | Only explored files can be modified |
| **Parallel execution guard** | Blocks other /code calls during active session (1 project = 1 session) |
| **Improvement cycle** | Learn from failures via DecisionLog + OutcomeLog |
| **Two-layer context** | Static project rules + dynamic task-specific rules |
| **Unified submit_phase** | Single tool for all phase exits (compaction resilience) |
| **Server-enforced task management** | Server verifies task completion (prevents LLM skipping) |
| **Safety valves** | Server-side counters prevent infinite loops |

---

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────┐
│                    LLM Agent                         │
│                   (/code skill)                      │
└─────────────────────┬───────────────────────────────┘
                      │ MCP Protocol
                      ▼
┌─────────────────────────────────────────────────────┐
│             Code Intelligence Server                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │   Router    │  │   Session   │  │ QueryFrame  │ │
│  │             │  │   Manager   │  │ Decomposer  │ │
│  └─────────────┘  └─────────────┘  └─────────────┘ │
│  ┌─────────────────────────────────────────────────┐│
│  │              ChromaDB Manager                   ││
│  │  ┌─────────────────┐  ┌─────────────────────┐  ││
│  │  │  Forest          │  │  Map                 │  ││
│  │  │  All code chunks │  │  Successful patterns │  ││
│  │  └─────────────────┘  └─────────────────────┘  ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│                  Tool Layer                          │
│  ctags │ ripgrep │ tree-sitter │ AST Chunker        │
└─────────────────────────────────────────────────────┘
```

### Forest/Map Two-Layer Structure

| Name | Purpose | Contents | Update Timing |
|------|---------|----------|---------------|
| **Forest** | Search entire codebase | AST-chunked code fragments | Incremental sync (SHA256) |
| **Map** | Reuse successful patterns | NL→Symbol pairs, agreements | On `/outcome success` |

**Short-circuit Logic**: Map score ≥ 0.7 → Skip Forest exploration

---

## Complete Flow (19 Steps)

LLM-side preprocessing (no server calls):
- Flag Check: Parse command options
- Intent Classification: IMPLEMENT / MODIFY / INVESTIGATE / QUESTION

All step exits use `submit_phase` (Step 1 only uses `start_session`).

### Session Start

| Step | Phase | LLM Action | Notes |
|------|-------|------------|-------|
| 1 | — | Determine intent (implement/investigate) → initialize with `start_session` | |
| 2 | BRANCH_INTERVENTION | Present choices to user | Only when stale branches detected |
| 3 | DOCUMENT_RESEARCH | Research design docs via sub-agent | |
| 4 | QUERY_FRAME | NL → structured slot extraction | |

### Exploration Phase

| Step | Phase | LLM Action | Notes |
|------|-------|------------|-------|
| 5 | EXPLORATION | Explore with code-intel tools | |
| 6 | Q1 | Assess SEMANTIC necessity | Server may skip Step 7 |
| 7 | SEMANTIC | Gather additional info via semantic_search | Skipped when Q1=false |
| 8 | Q2 | Assess VERIFICATION necessity | Server may skip Step 9 |
| 9 | VERIFICATION | Verify hypotheses | Skipped when Q2=false |
| 10 | Q3 | Assess IMPACT_ANALYSIS necessity | Server may skip Step 11 |
| 11 | IMPACT_ANALYSIS | Impact analysis | Skipped when Q3=false / Investigation: SESSION_COMPLETE |

### Implementation Phase

| Step | Phase | LLM Action | Notes |
|------|-------|------------|-------|
| 12 | READY | Task decomposition & planning | Branch created |
| 13 | READY | Implement via Edit/Write | Repeats per task (×N) |
| 14 | READY | Confirm all tasks complete | Blocks if incomplete |
| 15 | POST_IMPL_VERIFY | Execute verifier prompts | On failure: revert to Step 12 |
| 16 | VERIFY_INTERVENTION | Read & execute intervention prompt | Only when task failure_count ≥ 3 |
| 17 | PRE_COMMIT | Review via review_changes | |
| 18 | QUALITY_REVIEW | Quality check | quality_revert_count ≥ 3 → forced_completion |
| 19 | MERGE | — | SESSION_COMPLETE |

---

## Phase Matrix

The server determines the next phase in each `submit_phase` response. The LLM does not need to know about flags.

| Step | Phase | Implement | Investigate | --no-verify | --no-quality | --fast | --quick | --no-doc | -ni | Notes |
|------|-------|:---------:|:-----------:|:-----------:|:------------:|:------:|:-------:|:--------:|:---:|-------|
| 1 | start_session | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 2 | BRANCH_INTERVENTION | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Only when stale detected |
| 3 | DOCUMENT_RESEARCH | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | |
| 4 | QUERY_FRAME | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 5 | EXPLORATION | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 6 | Q1 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 7 | SEMANTIC | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Skipped when Q1=false |
| 8 | Q2 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 9 | VERIFICATION | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Skipped when Q2=false |
| 10 | Q3 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 11 | IMPACT_ANALYSIS | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Skipped when Q3=false |
| 12 | READY (planning) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Branch created |
| 13 | READY (implementation) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ×N |
| 14 | READY (completion) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Blocks if incomplete |
| 15 | POST_IMPL_VERIFY | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | On failure → revert to Step 12 |
| 16 | VERIFY_INTERVENTION | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | Only when task failure_count ≥ 3 |
| 17 | PRE_COMMIT | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | |
| 18 | QUALITY_REVIEW | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | |
| 19 | MERGE | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | |

**Legend**:
- **Implement**: IMPLEMENT / MODIFY intent (default)
- **Investigate**: INVESTIGATE / QUESTION intent (equivalent to `--only-explore`, SESSION_COMPLETE after exploration)
- **-ni**: `--no-intervention` (skips Step 16 VERIFY_INTERVENTION)
- Flag columns modify the Implement intent. For Investigate intent, Steps 12 onward do not exist.

### Command Options

| Long | Short | Description |
|------|-------|-------------|
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: f(ull), a(uto) [default: auto] |
| `--no-verify` | — | Skip POST_IMPL_VERIFY (Step 15) |
| `--no-quality` | — | Skip QUALITY_REVIEW (Step 18) |
| `--only-verify` | `-v` | Run verification only |
| `--only-explore` | `-e` | Run exploration only (SESSION_COMPLETE after Step 11) |
| `--fast` | `-f` | Skip exploration (Steps 5-11), with branch |
| `--quick` | `-q` | Skip exploration (Steps 5-11), no branch, also skips PRE_COMMIT/QUALITY_REVIEW/MERGE |
| `--no-doc-research` | — | Skip DOCUMENT_RESEARCH (Step 3) |
| `--no-intervention` | `-ni` | Skip VERIFY_INTERVENTION (Step 16) |
| `--clean` | `-c` | Delete stale branches + reset SessionState |
| `--rebuild` | `-r` | Force full index rebuild |

---

## submit_phase API

### Overview

```
start_session → submit_phase (×N) → SESSION_COMPLETE
                ↑                    ↑
                get_session_status   (recovery at any point)
```

- The only phase transition tool the LLM calls is `submit_phase`
- Server identifies the current phase from `SessionState.phase` and validates the payload
- Server determines the next phase and returns a response

### Response Format (Self-Describing)

Common response structure across all phases:

```json
{
  "phase": "EXPLORATION",
  "step": 5,
  "instruction": "Explore the codebase using code-intel tools",
  "expected_payload": {
    "explored_files": "list[str]",
    "findings": "list[str]"
  },
  "call": "submit_phase"
}
```

The LLM follows the `instruction` in the response, performs the work, and calls `submit_phase` with the `expected_payload` format.

### Per-Phase Payloads

| Step | Phase | LLM → Server (`data`) | Server Decision |
|------|-------|----------------------|-----------------|
| 2 | BRANCH_INTERVENTION | `{choice: "delete" \| "merge" \| "continue"}` | Execute action → next phase |
| 3 | DOCUMENT_RESEARCH | `{documents_reviewed: [...]}` | → next phase |
| 4 | QUERY_FRAME | `{action_type, target_symbols, scope, constraints}` | → next phase |
| 5 | EXPLORATION | `{explored_files: [...], findings: [...]}` | → next phase |
| 6 | Q1 | `{needs_more_information: bool, reason}` | true→SEMANTIC / false→Q2 |
| 7 | SEMANTIC | `{search_results: [...]}` | → next phase |
| 8 | Q2 | `{has_unverified_hypotheses: bool, reason}` | true→VERIFICATION / false→Q3 |
| 9 | VERIFICATION | `{hypotheses_verified: [...]}` | → next phase |
| 10 | Q3 | `{needs_impact_analysis: bool, reason}` | true→IMPACT / false(implement)→READY / false(investigate)→SESSION_COMPLETE |
| 11 | IMPACT_ANALYSIS | `{impact_summary: {...}}` | Implement→READY / Investigate→SESSION_COMPLETE |
| 12 | READY (planning) | `{tasks: [{id, description, status, ...}]}` | Register tasks (idempotent) |
| 13 | READY (implementation) | `{task_id, summary}` | Order check, ×N |
| 14 | READY (completion) | `{}` | All-tasks-complete check → next phase |
| 15 | POST_IMPL_VERIFY | `{passed: bool, failed_tasks?, details}` | pass→17 / fail→12 |
| 16 | VERIFY_INTERVENTION | `{prompt_used, action_taken}` | Reset failure_count → 12 |
| 17 | PRE_COMMIT | `{reviewed_files: [...], commit_message}` | Commit → next phase |
| 18 | QUALITY_REVIEW | `{quality_score, issues: [...]}` | No issues→MERGE / Issues→12 |
| 19 | MERGE | `{}` | Merge → SESSION_COMPLETE |

**Note**: When `gate_level="full"`, the server ignores Q1/Q2/Q3 assessments and forces all phases to execute.

---

## Task Orchestration

The READY phase internally has 3 sub-steps (planning → implementation → completion).
All are submitted via `submit_phase`, and the server distinguishes them by internal state.

### Step 12: Task Planning

Task model: `{id, description, status, failure_count?, revert_reason?}`

```python
# Initial submission
submit_phase(data={
  "tasks": [
    {"id": "task_1", "description": "Extract CSS component utilities", "status": "pending"},
    {"id": "task_2", "description": "Tokenize @theme values", "status": "pending"}
  ]
})

# After revert (complete list with completed + fix tasks)
submit_phase(data={
  "tasks": [
    {"id": "task_1", "description": "...", "status": "completed"},
    {"id": "task_2", "description": "...", "status": "completed"},
    {"id": "fix_1", "description": "Fix test failure", "status": "pending",
     "failure_count": 1, "revert_reason": "Test X failed"}
  ]
})
```

**Idempotent design**: Step 12 always receives the complete task list. Resending the same payload produces the same result.

### Step 13: Task Completion Report (×N)

```python
submit_phase(data={"task_id": "task_1", "summary": "Conversion complete"})
```

Report completions in registration order. The server tracks progress and returns the next task.

### Step 14: Implementation Complete

```python
submit_phase(data={})
```

The server verifies all tasks are complete. Blocks if any remain incomplete.

### Revert Behavior

When reverted to Step 12 from POST_IMPL_VERIFY / VERIFY_INTERVENTION / QUALITY_REVIEW:

1. Server: includes revert reason + existing task list in `instruction`
2. LLM: submits complete list of existing tasks (completed) + fix tasks (pending) via `submit_phase`
3. LLM: implements only pending tasks (Step 13 ×N)
4. → Re-advances to Step 15 POST_IMPL_VERIFY

---

## Safety Valves (Loop Prevention)

The LLM cannot recognize loops after compaction, so all loop limits are enforced server-side.

### Server-Managed Counters

```python
class SessionState:
    intervention_count: int = 0      # Number of VERIFY_INTERVENTION executions
    quality_revert_count: int = 0    # Number of reverts from QUALITY_REVIEW
    # failure_count is per-task (Task.failure_count)
```

### Loop Paths and Limits

| Loop Path | Trigger | Limit | Action on Exceeded |
|-----------|---------|-------|--------------------|
| POST_IMPL_VERIFY → Step 12 | Verification failure | Task failure_count ≥ 3 | → VERIFY_INTERVENTION |
| VERIFY_INTERVENTION → Step 12 | After intervention | intervention_count ≥ 2 | → user_escalation (AskUserQuestion) |
| QUALITY_REVIEW → Step 12 | Quality issues found | quality_revert_count ≥ 3 | → forced_completion (MERGE with warning) |

---

## Compaction Resilience Design

### Premise

```
LLM conversation  ← Subject to compaction (conversation history gets summarized)
   ↕ MCP Protocol
MCP Server         ← Not subject to compaction (SessionState held in memory)
```

### 4-Layer Resilience

| Layer | Measure | Effect |
|-------|---------|--------|
| Tool design | Single `submit_phase` | Never forgets "what to call" |
| Response design | Includes `expected_payload` | Never forgets "what to return" |
| Recovery | `get_session_status` | Full state recovery mechanism |
| Defense | Payload mismatch detection | Prevents silent failures |

### Recovery via get_session_status

When the LLM loses state after compaction:

```json
{
  "session_id": "...",
  "phase": "VERIFICATION",
  "step": 9,
  "completed_steps": [1, 2, 3, 4, 5, 6, 7, 8],
  "instruction": "Verify hypotheses and call submit_phase",
  "expected_payload": {
    "hypotheses_verified": "list[{hypothesis, result, evidence}]"
  }
}
```

### Payload Mismatch Detection

When the LLM sends a payload unrelated to the current phase, the server returns re-instructions for the current phase.

---

## Two-Layer Context

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: project_rules (at session start)                       │
│  └── Always-needed baseline rules (lightweight, cached)          │
│      Source: CLAUDE.md / Content: DO/DON'T list                  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: mandatory_rules (DOCUMENT_RESEARCH phase)              │
│  └── Task-specific detailed rules (dynamic, per-task)           │
│      Source: docs/**/*.md (sub-agent research)                   │
└─────────────────────────────────────────────────────────────────┘
```

| Aspect | project_rules | mandatory_rules |
|--------|---------------|-----------------|
| Source | CLAUDE.md | docs/**/*.md |
| Timing | Session start | DOCUMENT_RESEARCH (Step 3) |
| Content | Generic DO/DON'T | Task-specific constraints |
| Skippable | No | Yes (`--no-doc`) |

---

## Tools Reference

### Session Management

| Tool | Description |
|------|-------------|
| `start_session` | Start session with intent, query, and flags |
| `submit_phase` | **Single exit point for all phases** — server determines next phase |
| `get_session_status` | Get current phase + instruction + expected_payload (for recovery) |

### Code Exploration

| Tool | Description |
|------|-------------|
| `search_text` | Text pattern search (parallel-capable) |
| `find_definitions` | Symbol definition search (ctags) |
| `find_references` | Reference search (ripgrep) |
| `analyze_structure` | Code structure analysis (tree-sitter) |
| `get_symbols` | Get symbol list for a file |
| `search_files` | File name pattern search |
| `semantic_search` | Vector search in Forest/Map (SEMANTIC phase) |
| `query` | General natural language query |

### Implementation Control

| Tool | Description |
|------|-------------|
| `check_write_target` | Verify file can be modified |
| `add_explored_files` | Add files to explored list |
| `revert_to_exploration` | Return to EXPLORATION phase |
| `review_changes` | Show all file changes in PRE_COMMIT |

### Branch Management

| Tool | Description |
|------|-------------|
| `cleanup_stale_branches` | Delete all `llm_task_*` branches |

### Index & Learning

| Tool | Description |
|------|-------------|
| `sync_index` | Sync ChromaDB index |
| `update_context` | Update context.yml summaries |
| `record_outcome` | Record success/failure |
| `get_outcome_stats` | Get learning statistics |

---

## Setup Guide

### Prerequisites

| Tool | Required | Purpose |
|------|----------|---------|
| Python 3.10+ | Yes | Server runtime |
| Universal Ctags | Yes | Symbol definitions |
| ripgrep | Yes | Code search |
| tree-sitter | Yes | Structure analysis |
| git | Yes | Branch isolation |

### Step 1: Server Setup (once)

```bash
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper
./setup.sh
```

### Step 2: Project Initialization (per project)

```bash
./init-project.sh /path/to/your-project
```

Creates:
```
your-project/
├── .claude/
│   ├── CLAUDE.md              (project rules template)
│   ├── PARALLEL_GUIDE.md      (parallel execution guide)
│   └── commands/              (skills: code.md, exp.md, etc.)
└── .code-intel/
    ├── config.json            (indexing configuration)
    ├── context.yml            (context & doc_research settings)
    ├── task_planning.md       (task decomposition guide, v1.11)
    ├── agreements/            (NL→Symbol pairs)
    ├── chroma/                (ChromaDB)
    ├── logs/                  (DecisionLog, OutcomeLog)
    ├── verifiers/             (verification prompts)
    ├── doc_research/          (document research prompts)
    ├── review_prompts/        (garbage detection, quality review)
    └── interventions/         (intervention prompts)
```

### Step 3: Configure .mcp.json

```json
{
  "mcpServers": {
    "code-intel": {
      "type": "stdio",
      "command": "/path/to/llm-helper/venv/bin/python",
      "args": ["/path/to/llm-helper/code_intel_server.py"],
      "env": {"PYTHONPATH": "/path/to/llm-helper"}
    }
  }
}
```

### Step 4: Restart Claude Code

`init-project.sh` automatically copies skill files to `.claude/commands/`, so manual copying is not required.
To update skills only for an existing project:

```bash
cp /path/to/llm-helper/.claude/commands/code.md /path/to/your-project/.claude/commands/
```

---

## Configuration

### config.json

```json
{
  "version": "1.0",
  "embedding_model": "multilingual-e5-small",
  "source_dirs": ["src", "lib"],
  "exclude_patterns": ["**/node_modules/**", "**/__pycache__/**"],
  "chunk_strategy": "ast",
  "chunk_max_tokens": 512,
  "sync_ttl_hours": 1,
  "sync_on_start": true
}
```

### context.yml

```yaml
project_rules:
  source: "CLAUDE.md"
  summary: |
    DO:
    - Use Service layer for business logic
    DON'T:
    - Write complex logic in Controllers

doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

document_search:
  include_patterns:
    - "**/*.md"
  exclude_patterns:
    - "node_modules/**"
```

---

## Internal Reference

### Core Modules

| Module | File | Responsibility |
|--------|------|----------------|
| SessionState | `tools/session.py` | Session state, phase transitions, task management |
| QueryFrame | `tools/query_frame.py` | NL → structured query |
| ChromaDB Manager | `tools/chromadb_manager.py` | Forest/Map management |
| ImpactAnalyzer | `tools/impact_analyzer.py` | Change impact analysis |
| ContextProvider | `tools/context_provider.py` | Project rules & document research |
| BranchManager | `tools/branch_manager.py` | Git branch isolation |

### Key Data Structures

```python
class Phase(Enum):
    BRANCH_INTERVENTION = auto()   # Step 2
    DOCUMENT_RESEARCH = auto()     # Step 3
    QUERY_FRAME = auto()           # Step 4
    EXPLORATION = auto()           # Step 5
    Q1 = auto()                    # Step 6
    SEMANTIC = auto()              # Step 7
    Q2 = auto()                    # Step 8
    VERIFICATION = auto()          # Step 9
    Q3 = auto()                    # Step 10
    IMPACT_ANALYSIS = auto()       # Step 11
    READY = auto()                 # Steps 12-14
    POST_IMPL_VERIFY = auto()      # Step 15
    VERIFY_INTERVENTION = auto()   # Step 16
    PRE_COMMIT = auto()            # Step 17
    QUALITY_REVIEW = auto()        # Step 18
    MERGE = auto()                 # Step 19

class SessionState:
    session_id: str
    intent: str                    # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    phase: Phase                   # Current phase
    query_frame: QueryFrame
    gate_level: str                # full/auto
    # v1.11: flags
    fast_mode: bool
    quick_mode: bool
    no_doc: bool
    no_verify: bool
    no_quality: bool
    no_intervention: bool
    # v1.11: task management
    tasks: list[TaskModel]
    # v1.11: safety valve counters
    intervention_count: int
    quality_revert_count: int

@dataclass
class TaskModel:
    id: str
    description: str
    status: str                    # pending/completed
    failure_count: int = 0
    revert_reason: str = ""
```

---

## CHANGELOG

| Version | Description | Link |
|---------|-------------|------|
| v1.11 | Unified submit_phase & task orchestration (17 tools → 1, compaction resilience, server-enforced task management) | [v1.11](updates/v1.11_ja.md) |
| v1.10 | Individual Phase Checks (per-phase necessity checks, VERIFICATION/IMPACT separation, gate_level redesign) | [v1.10](updates/v1.10_ja.md) |
| v1.9 | Performance Optimization (sync_index batch processing - saves 15-20s) | [v1.9](updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode (--only-explore) | [v1.8](updates/v1.8_ja.md) |
| v1.7 | Parallel Execution (search_text, Read, Grep - saves 27-35s) | [v1.7](updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle (stale warning, begin_phase_gate) | [v1.6](updates/v1.6_ja.md) |
| v1.5 | Quality Review | [v1.5](updates/v1.5_ja.md) |
| v1.4 | Intervention System | [v1.4](updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1_ja.md) |

For documentation management rules, see [DOCUMENTATION_RULES.md](DOCUMENTATION_RULES.md).
