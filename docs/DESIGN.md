# Code Intelligence MCP Server - Design Document v1.16

> This document describes the complete system specification as of v1.16.
> For version history, see [CHANGELOG](#changelog).

---

## Table of Contents

1. [Overview](#overview)
2. [Design Philosophy](#design-philosophy)
3. [Architecture](#architecture)
4. [Phase Contract](#phase-contract)
5. [Complete Flow (19 Steps)](#complete-flow-19-steps)
6. [Session Management and Recovery](#session-management-and-recovery)
7. [Phase Matrix](#phase-matrix)
8. [submit_phase API](#submit_phase-api)
9. [Task Orchestration](#task-orchestration)
10. [Safety Valves (Loop Prevention)](#safety-valves-loop-prevention)
11. [2-Layer Context](#2-layer-context)
12. [Tool Reference](#tool-reference)
13. [Project Structure](#project-structure)
14. [Configuration](#configuration)
15. [Internal Reference](#internal-reference)
16. [CHANGELOG](#changelog)

---

## Overview

### Goal

Force LLM agents to follow a workflow where they "understand the codebase accurately before implementing."
By controlling phase transitions, validation, and recovery on the server side rather than leaving decisions to the LLM, we ensure implementation quality and reproducibility.

### What It Achieves

| Feature | Description |
|---------|-------------|
| **Phase Enforcement** | Server enforces exploration → verification → implementation → review order. No shortcuts |
| **Phase Contract** | Defines instruction, expected payload, and required tools for each phase. Contract violations are rejected |
| **Code Exploration** | Structured exploration using ctags / ripgrep / tree-sitter / ChromaDB |
| **Session Persistence** | Recovery from checkpoint possible after CLI restart |
| **Compaction Resilience** | Detects LLM context loss and auto-restores summaries |
| **Write Restrictions** | Only explored files can be modified |

### Technical Overview

```
┌─────────────────────────────────────────────────────┐
│            LLM Agent (Claude Code / Codex)          │
│                     /code skill                      │
└─────────────────────┬───────────────────────────────┘
                      │ MCP Protocol
                      ▼
┌─────────────────────────────────────────────────────┐
│             Code Intelligence Server                 │
│  ・Phase Contract (phase_contract.yml)              │
│  ・Checkpoint Persistence (sessions/*.json)         │
│  ・ChromaDB (Forest/Map 2-layer vector search)      │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│                  Tool Layer                          │
│  ctags │ ripgrep │ tree-sitter │ AST Chunker        │
└─────────────────────────────────────────────────────┘
```

| Component | Role |
|-----------|------|
| **MCP Server** | Phase transitions, payload validation, checkpoint saving |
| **Phase Contract** | Defines instruction / expected_payload / required_tools per phase |
| **ChromaDB** | 2-layer vector search with Forest (full code) and Map (success patterns) |
| **Tool Layer** | ctags (symbol definitions), ripgrep (search), tree-sitter (structure analysis) |

### Supported Clients

| Client | Support Status |
|--------|---------------|
| **Claude Code** | Available via `/code` skill |
| **Codex** | Available via `/code` skill |

---

## Design Philosophy

```
Don't let the LLM decide. Design so it can't proceed without compliance.
And have a mechanism to learn from failures.
```

### Core Principles

| Principle | Implementation |
|-----------|---------------|
| **Phase Enforcement** | Tool restrictions per phase (no shortcuts) |
| **Server Evaluation** | Server calculates confidence (not LLM self-report) |
| **Quote Verification** | Compare LLM-extracted quotes against original query |
| **Embedding Verification** | Objective NL→Symbol relevance evaluation via vector similarity |
| **Write Restrictions** | Only explored files can be modified |
| **Parallel Execution Guard** | Block other /code calls during session (1 project = 1 session) |
| **Improvement Cycle** | Learn from failures via DecisionLog + OutcomeLog |
| **2-Layer Context** | Static project rules + dynamic task-specific rules |
| **submit_phase Unification** | Unify all phase exits to single tool (compaction resilience) |
| **Server-Enforced Task Management** | Server verifies task completion (prevent LLM skipping) |
| **Safety Valves** | Prevent infinite loops with server-side counters |
| **Implementation Checklist (v1.16)** | Verify all checklist items with evidence/reason on task completion, detect empty implementations |

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
│  ┌─────────────┐  ┌─────────────────────────────────┐│
│  │  Phase      │  │         Checkpoint              ││
│  │  Contract   │  │  sessions/{session_id}.json    ││
│  │             │  │                                  ││
│  └─────────────┘  └─────────────────────────────────┘│
│  ┌─────────────────────────────────────────────────┐│
│  │              ChromaDB Manager                   ││
│  │  ┌─────────────────┐  ┌─────────────────────┐  ││
│  │  │  Forest          │  │  Map                 │  ││
│  │  │  All code chunks │  │  Success patterns    │  ││
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

### Responsibility Separation: Orchestrator → LLM → submit_phase

- **Orchestrator (Server)**: Identifies current phase, returns `instruction` + `expected_payload`
- **LLM**: Executes instructions, calls **only `submit_phase`** with `expected_payload` format
- **Server**: Validates payload, determines next phase

Phase contract (`instruction` / `expected_payload` / required tools) is consolidated in
`.code-intel/phase_contract.yml`, and **submissions not following the contract are rejected**.
Minimum contract unit is **summary + tools_used** (with some exceptions like READY completion / MERGE).

See [Phase Contract](#phase-contract) section for details.

### Forest/Map 2-Layer Structure

| Name | Purpose | Contents | Update Timing |
|------|---------|----------|---------------|
| **Forest** | Search entire codebase | AST-chunked code fragments | Incremental sync (SHA256) |
| **Map** | Reuse success patterns | NL→Symbol pairs, agreements | On `/outcome success` |

**Shortcut Logic**: Map score ≥ 0.7 → Skip Forest exploration

---

## Phase Contract

Phase contract is a mechanism to formalize how the flow is implemented.

### 3-Layer Contract Structure

```
┌─────────────────────────────────────────────────────────────────┐
│  ①Orchestrator                                                   │
│    ↓ instruction + expected_payload + required_tools            │
├─────────────────────────────────────────────────────────────────┤
│  ②LLM (Work Execution)                                          │
│    ↓ submit_phase(data={summary, tools_used, ...})              │
├─────────────────────────────────────────────────────────────────┤
│  ③Server (Validation)                                            │
│    → Payload validation → Determine next phase → Save checkpoint │
└─────────────────────────────────────────────────────────────────┘
```

### Contract Definition Location

Consolidated in `.code-intel/phase_contract.yml`:

| Element | Description |
|---------|-------------|
| `instruction` | Instructions for work LLM should execute |
| `expected_payload` | Payload structure to send to submit_phase (pseudo-schema) |
| `required_tools` | Required tools (rejected if unused) |

### Minimum Contract Unit

| Phase Type | Minimum Payload |
|------------|-----------------|
| Normal phases | `summary` + `tools_used` |
| Completion phases (READY completion / MERGE) | `summary` only |

### Server-Side Validation

1. **Required tool usage confirmation**: Are required tools included in `tools_used`?
2. **Summary existence confirmation**: summary required for all phases
3. **On contract violation**: Re-instruction with `payload_mismatch` error

### Contract Response Format

```json
{
  "phase": "EXPLORATION",
  "step": 5,
  "instruction": "Explore using at least 2 types of code-intel tools...",
  "expected_payload": {
    "explored_files": "list[str]",
    "findings": "list[str]",
    "tools_used": "list[str] (at least 2 types)",
    "summary": "str"
  },
  "call": "submit_phase"
}
```

See [templates/code-intel/phase_contract.yml](../templates/code-intel/phase_contract.yml) for details.

---

## Complete Flow (19 Steps)

LLM-side preprocessing (no server calls):
- Flag Check: Command option parsing
- Intent Classification: IMPLEMENT / MODIFY / INVESTIGATE / QUESTION

All step exits use `submit_phase` (Step 1 only uses `start_session`).

### Session Start

| Step | Phase | LLM Processing | Notes |
|------|-------|----------------|-------|
| 1 | — | Intent determination (implement/investigate) → Initialize with `start_session` | |
| 2 | BRANCH_INTERVENTION | Present choices to user | Only when stale branch detected |
| 3 | DOCUMENT_RESEARCH | Sub-agent investigates design documents | |
| 4 | QUERY_FRAME | NL → Structured slot extraction | |

### Exploration Phases

| Step | Phase | LLM Processing | Notes |
|------|-------|----------------|-------|
| 5 | EXPLORATION | Explore with code-intel tools | |
| 6 | Q1 | Evaluate SEMANTIC necessity | Server may skip 7 |
| 7 | SEMANTIC | Additional info gathering with semantic_search | Skip if Q1=false |
| 8 | Q2 | Evaluate VERIFICATION necessity | Server may skip 9 |
| 9 | VERIFICATION | Hypothesis verification | Skip if Q2=false |
| 10 | Q3 | Evaluate IMPACT_ANALYSIS necessity | Server may skip 11 |
| 11 | IMPACT_ANALYSIS | Impact analysis | Skip if Q3=false / Investigation: SESSION_COMPLETE |

### Implementation Phases

| Step | Phase | LLM Processing | Notes |
|------|-------|----------------|-------|
| 12 | READY | Task decomposition/planning | Branch creation |
| 13 | READY | Implement with Edit/Write | Repeat for task count (×N) |
| 14 | READY | Confirm all tasks complete | Block if incomplete |
| 15 | POST_IMPL_VERIFY | Execute verifier prompts | Revert to Step 12 on fail |
| 16 | VERIFY_INTERVENTION | Read/execute intervention prompts | Only when task failure_count ≥ 3 |
| 17 | PRE_COMMIT | Review with review_changes | |
| 18 | QUALITY_REVIEW | Quality check | quality_revert_count ≥ 3 → forced_completion |
| 19 | MERGE | — | SESSION_COMPLETE |

---

## Session Management and Recovery

### Challenges

| Scenario | MCP Process | SessionState | Challenge |
|----------|------------|--------------|-----------|
| Auto-compaction (same session) | Alive | Retained | LLM context loss |
| `/clear` (same CLI) | Alive | Retained | LLM context loss |
| CLI termination → `--continue` | Restarted | **Lost** | Server state also lost |
| Context exhaustion → new conversation | Restarted | **Lost** | Server state also lost |

```
LLM Conversation  ← Subject to compaction (conversation history summarized)
   ↕ MCP Protocol
MCP Server        ← In-memory (lost on CLI termination)
```

### Solution Overview

| Solution | Target Scenario | Mechanism |
|----------|----------------|-----------|
| **Checkpoint Persistence** | Cross-CLI | File save → restore |
| **compaction_count Method** | Same-session compaction | Detect via value mismatch → restore summaries |
| **get_session_status** | Any state loss | Recovery API |

### Checkpoint Persistence

```
submit_phase success
  → Serialize with SessionState.to_dict()
  → Save to .code-intel/sessions/{session_id}.json

CLI restart → /code --resume
  → get_session_status detects checkpoint
  → Restore with SessionState.from_dict()
```

| Item | Content |
|------|---------|
| **Save Location** | `.code-intel/sessions/{session_id}.json` |
| **Save Timing** | After `submit_phase` success (every phase) |
| **Recovery Method** | `/code --resume` → `get_session_status` |
| **On Completion** | Delete checkpoint on MERGE (SESSION_COMPLETE) |
| **Cleanup** | `--clean` deletes stale branches + all checkpoints |

### Two Recovery Paths

| Path | Trigger | Behavior |
|------|---------|----------|
| **Explicit Resume** | `/code --resume` | Load code.md + restore with get_session_status |
| **Detection During Normal Execution** | `/code <query>` | start_session returns `recovery_available`, present choice to user |

> Note: Restoring with `get_session_status` alone leaves LLM-side context insufficient.
> In principle, use `/code --resume` to secure **both LLM context + server state**.

### compaction_count Method

Mechanism to detect compaction within same session and auto-restore summaries.

**Principle**: Server and LLM share `compaction_count` (integer), detect compaction via **value mismatch**.

```
1. Session start: compaction_count = 0
2. Normal submit: LLM echoes back count → matches server → normal response
3. Compaction occurs: LLM detects context loss → sends count +1
4. Server: Detects mismatch → attaches phase_summaries to response + updates count
5. LLM: Reads summaries to restore context
```

Response example (when mismatch detected):
```json
{
  "success": true,
  "phase": "Q1",
  "step": 6,
  "compaction_count": 1,
  "phase_summaries": {
    "step_03_DOCUMENT_RESEARCH": "Brief summary of key points",
    "step_05_EXPLORATION": "Exploration results summary"
  }
}
```

### 4-Layer Compaction Resilience Structure

| Layer | Measure | Effect |
|-------|---------|--------|
| Tool Design | Single `submit_phase` | Never forget "what to call" |
| Response Design | Include `expected_payload` | Never forget "what to return" |
| Recovery | `get_session_status` | Full recovery means |
| Defense | Payload mismatch detection | Prevent silent failures |

**Payload Mismatch Detection**: When LLM sends payload unrelated to phase, server returns re-instruction for current phase.

### Persistence Granularity

| Category | Persistence Target |
|----------|-------------------|
| Orchestrator State | session_id, flags, phase_state, counters, tasks |
| LLM-facing Summary | Each phase's `summary` field only |

**Policy**: Don't save detailed info; compress to `summary` and persist. Allow compression to summary on resumption.

---

## Phase Matrix

Server determines next phase in each submit_phase response. LLM doesn't need to know flags.

| Step | Phase | Impl | Investigate | --no-verify | --no-quality | --fast | --quick | --no-doc | -ni | Notes |
|------|-------|:----:|:-----------:|:-----------:|:------------:|:------:|:-------:|:--------:|:---:|-------|
| 1 | start_session | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 2 | BRANCH_INTERVENTION | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Only when stale detected |
| 3 | DOCUMENT_RESEARCH | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | |
| 4 | QUERY_FRAME | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 5 | EXPLORATION | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 6 | Q1 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 7 | SEMANTIC | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Skip if Q1=false |
| 8 | Q2 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 9 | VERIFICATION | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Skip if Q2=false |
| 10 | Q3 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 11 | IMPACT_ANALYSIS | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Skip if Q3=false |
| 12 | READY (planning) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Branch creation |
| 13 | READY (implementation) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ×N |
| 14 | READY (completion) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Block if incomplete |
| 15 | POST_IMPL_VERIFY | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | fail→Revert to Step 12 |
| 16 | VERIFY_INTERVENTION | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | Only when task failure_count ≥ 3 |
| 17 | PRE_COMMIT | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | |
| 18 | QUALITY_REVIEW | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | |
| 19 | MERGE | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | |

**Legend**:
- **Impl**: IMPLEMENT / MODIFY intent (default)
- **Investigate**: INVESTIGATE / QUESTION intent (equivalent to `--only-explore`, SESSION_COMPLETE on exploration complete)
- **-ni**: `--no-intervention` (Skip Step 16 VERIFY_INTERVENTION)
- Flag columns modify implementation intent. Steps 12+ don't exist for investigation intent.

### Command Options

| Long | Short | Description |
|------|-------|-------------|
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: f(ull), a(uto) [default: auto] |
| `--no-verify` | — | Skip POST_IMPL_VERIFY (Step 15) |
| `--no-quality` | — | Skip QUALITY_REVIEW (Step 18) |
| `--only-verify` | `-v` | Run verification only |
| `--only-explore` | `-e` | Run exploration only (SESSION_COMPLETE after Step 11) |
| `--fast` | `-f` | Skip exploration (Steps 5-11), with branch |
| `--quick` | `-q` | Skip exploration (Steps 5-11), no branch, also skip PRE_COMMIT/QUALITY_REVIEW/MERGE |
| `--no-doc-research` | — | Skip DOCUMENT_RESEARCH (Step 3) |
| `--no-intervention` | `-ni` | Skip VERIFY_INTERVENTION (Step 16) |
| `--resume` | `-r` | Resume session from checkpoint |
| `--clean` | `-c` | Delete stale branches + all checkpoints |
| `--rebuild` | — | Force rebuild all indexes |

---

## submit_phase API

### Overview

```
start_session → submit_phase (×N) → SESSION_COMPLETE
                ↑                    ↑
                get_session_status   (recovery at any point)
```

- LLM only calls `submit_phase` for phase transitions
- Server identifies current phase from `SessionState.phase` and validates payload
- Server determines next phase and returns response
- Following `phase_contract.yml` contract, **requires tool usage evidence**

### Response Format (Self-Contained)

Common response structure for all phases:

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

LLM follows response `instruction`, then calls `submit_phase` with `expected_payload` format.

### Payloads by Phase

| Step | Phase | LLM → Server (`data`) | Server Decision |
|------|-------|----------------------|-----------------|
| 2 | BRANCH_INTERVENTION | `{choice: "delete" \| "merge" \| "continue", tools_used, summary}` | Execute → Next phase |
| 3 | DOCUMENT_RESEARCH | `{documents_reviewed: [...], tools_used, summary}` | → Next phase |
| 4 | QUERY_FRAME | `{action_type, target_symbols, scope, constraints, tools_used, summary}` | → Next phase |
| 5 | EXPLORATION | `{explored_files: [...], findings: [...], tools_used, summary}` | → Next phase |
| 6 | Q1 | `{needs_more_information: bool, reason}` | true→SEMANTIC / false→Q2 |
| 7 | SEMANTIC | `{search_results: [...], tools_used, summary}` | → Next phase |
| 8 | Q2 | `{has_unverified_hypotheses: bool, reason}` | true→VERIFICATION / false→Q3 |
| 9 | VERIFICATION | `{hypotheses_verified: [...], tools_used, summary}` | → Next phase |
| 10 | Q3 | `{needs_impact_analysis: bool, reason}` | true→IMPACT / false(impl)→READY / false(investigate)→SESSION_COMPLETE |
| 11 | IMPACT_ANALYSIS | `{impact_summary: {...}, tools_used, summary}` | Impl→READY / Investigate→SESSION_COMPLETE |
| 12 | READY (planning) | `{tasks: [{id, description, status, checklist: [{item, status}]}], tools_used, summary}` | Register tasks (idempotent) |
| 13 | READY (implementation) | `{task_id, checklist: [{item, status, evidence?, reason?}], summary, tools_used}` | Checklist validation, ×N |
| 14 | READY (completion) | `{summary}` | All tasks complete check → Next phase |
| 15 | POST_IMPL_VERIFY | `{passed: bool, failed_tasks?, details, tools_used, summary}` | pass→17 / fail→12 |
| 16 | VERIFY_INTERVENTION | `{prompt_used, action_taken, tools_used, summary}` | failure_count reset → 12 |
| 17 | PRE_COMMIT | `{reviewed_files: [...], commit_message, tools_used, summary}` | Commit → Next phase |
| 18 | QUALITY_REVIEW | `{quality_score, issues: [...], tools_used, summary}` | No issues→MERGE / Issues→12 |
| 19 | MERGE | `{summary}` | Merge → SESSION_COMPLETE |

**Note**: When `gate_level="full"`, server ignores Q1/Q2/Q3 evaluations and forces all phase execution.

---

## Task Orchestration

READY phase internally has 3 substeps (planning → implementation → completion).
All sent via `submit_phase`, server distinguishes by internal state.

### Step 12: Task Planning

Task model: `{id, description, status, checklist, failure_count?, revert_reason?}`

Checklist item: `{item, status, evidence?, reason?}`

| status | evidence | reason | Description |
|--------|----------|--------|-------------|
| `pending` | - | - | Initial state |
| `done` | **Required** | - | Implementation complete (provide evidence as `file:line`) |
| `skipped` | - | **Required** | Not implementing (explain reason, min 10 chars) |

```python
# Initial
submit_phase(data={
  "tasks": [
    {
      "id": "task_1",
      "description": "Implement authentication",
      "status": "pending",
      "checklist": [
        {"item": "Add login() method to auth.py", "status": "pending"},
        {"item": "Add logout() method to auth.py", "status": "pending"},
        {"item": "Add validate_password() to auth.py", "status": "pending"}
      ]
    }
  ]
})

# On revert (complete list of completed + fix tasks)
submit_phase(data={
  "tasks": [
    {"id": "task_1", "description": "...", "status": "completed", "checklist": [...]},
    {"id": "fix_1", "description": "Fix test failure", "status": "pending",
     "checklist": [...], "failure_count": 1, "revert_reason": "Test X failed"}
  ]
})
```

**Idempotent Design**: Step 12 always receives complete task list. Same result for same payload resent.

### Step 13: Task Completion Report (×N)

```python
submit_phase(data={
  "task_id": "task_1",
  "summary": "Authentication implementation complete",
  "checklist": [
    {"item": "Add login() method to auth.py", "status": "done", "evidence": "auth.py:42-58"},
    {"item": "Add logout() method to auth.py", "status": "done", "evidence": "auth.py:60-75"},
    {"item": "Add validate_password() to auth.py", "status": "skipped",
     "reason": "Reusing existing PasswordValidator class"}
  ]
})
```

**Validation Logic (Server-side)**:

1. **All items reported check**: Do reported items match registered checklist items?
2. **Status check**: All items must be `done` or `skipped` (no `pending` remaining)
3. **Evidence validation** (for `done`):
   - Format: `file.py:42` or `file.py:42-58`
   - File existence check
   - Line number range check
   - Empty implementation detection (error if only pass/TODO/NotImplementedError)
4. **Reason validation** (for `skipped`): Requires reason with minimum 10 characters

Report completion in registration order. Server tracks progress and returns next task.

### Step 14: Implementation Complete

```python
submit_phase(data={})
```

Server verifies all tasks complete. Block if incomplete.

### Revert Behavior

When reverted from POST_IMPL_VERIFY / VERIFY_INTERVENTION / QUALITY_REVIEW to Step 12:

1. Server: Include revert reason + existing task list in instruction
2. LLM: Send complete list of existing tasks (completed) + fix tasks (pending) via submit_phase
3. LLM: Implement pending tasks only (Step 13 ×N)
4. → Re-progress to Step 15 POST_IMPL_VERIFY

---

## Safety Valves (Loop Prevention)

LLM cannot recognize loops after compaction, so all loop limits are enforced server-side.

### Server-Managed Counters

```python
class SessionState:
    intervention_count: int = 0      # VERIFY_INTERVENTION execution count
    quality_revert_count: int = 0    # QUALITY_REVIEW revert count
    # failure_count is per-task (Task.failure_count)
```

### Loop Paths and Limits

| Loop Path | Trigger | Limit | Action on Exceed |
|-----------|---------|-------|------------------|
| POST_IMPL_VERIFY → Step 12 | Verification failure | Task failure_count ≥ 3 | → VERIFY_INTERVENTION |
| VERIFY_INTERVENTION → Step 12 | After intervention | intervention_count ≥ 2 | → user_escalation (AskUserQuestion) |
| QUALITY_REVIEW → Step 12 | Quality issues exist | quality_revert_count ≥ 3 | → forced_completion (MERGE with warning) |

---

## 2-Layer Context

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: project_rules (at session start)                       │
│  └── Baseline rules always needed (lightweight, cached)          │
│      Source: CLAUDE.md / Content: DO/DON'T list                 │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: mandatory_rules (DOCUMENT_RESEARCH phase)              │
│  └── Task-specific detailed rules (dynamic, per-task)            │
│      Source: docs/**/*.md (sub-agent investigation)             │
└─────────────────────────────────────────────────────────────────┘
```

| Aspect | project_rules | mandatory_rules |
|--------|---------------|-----------------|
| Source | CLAUDE.md | docs/**/*.md |
| Timing | Session start | DOCUMENT_RESEARCH (Step 3) |
| Content | Generic DO/DON'T | Task-specific constraints |
| Skippable | No | Yes (`--no-doc`) |

---

## Tool Reference

### Session Management

| Tool | Description |
|------|-------------|
| `start_session` | Start session with intent, query, flags |
| `submit_phase` | **Only exit for all phases** — Server determines next phase |
| `get_session_status` | Get current phase + instruction + expected_payload (for recovery) |

### Code Exploration

| Tool | Description |
|------|-------------|
| `search_text` | Text pattern search (parallel support) |
| `find_definitions` | Symbol definition search (ctags) |
| `find_references` | Reference search (ripgrep) |
| `analyze_structure` | Code structure analysis (tree-sitter) |
| `get_symbols` | Get symbol list for file |
| `search_files` | Filename pattern search |
| `semantic_search` | Forest/Map vector search (SEMANTIC phase) |
| `query` | General natural language query |

### Implementation Control

| Tool | Description |
|------|-------------|
| `check_write_target` | Verify if file can be modified |
| `add_explored_files` | Add files to explored list |
| `review_changes` | Show all file changes in PRE_COMMIT |

### Branch Management

| Tool | Description |
|------|-------------|
| `cleanup_stale_branches` | Delete all `llm_task_*` branches |

### Index

| Tool | Description |
|------|-------------|
| `sync_index` | Sync ChromaDB index |
| `update_context` | Update context.yml summary |

---

## Project Structure

Project structure after running `init-project.sh`:

```
your-project/
├── .claude/
│   ├── CLAUDE.md              (Project rules template)
│   └── commands/              (Skills: code.md, etc.)
└── .code-intel/
    ├── config.json            (Index configuration)
    ├── context.yml            (Context & doc_research settings)
    ├── phase_contract.yml     (Phase contract definitions)
    ├── task_planning.md       (Task division guide)
    ├── user_escalation.md     (User escalation procedure)
    ├── agreements/            (NL→Symbol pairs)
    ├── chroma/                (ChromaDB)
    ├── logs/                  (DecisionLog, OutcomeLog)
    ├── sessions/              (Checkpoint save location)
    ├── verifiers/             (Verification prompts)
    ├── doc_research/          (Document research prompts)
    ├── review_prompts/        (Garbage detection, quality review)
    └── interventions/         (Intervention prompts)
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
    - Business logic in Service layer
    DON'T:
    - Complex logic in Controller

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
    # Flags
    fast_mode: bool
    quick_mode: bool
    no_doc: bool
    no_verify: bool
    no_quality: bool
    no_intervention: bool
    # Task management
    tasks: list[TaskModel]
    # Safety valve counters
    intervention_count: int
    quality_revert_count: int

@dataclass
class ChecklistItem:
    item: str
    status: str                    # pending/done/skipped
    evidence: str | None = None    # Required for done (file.py:42 format)
    reason: str | None = None      # Required for skipped (min 10 chars)

@dataclass
class TaskModel:
    id: str
    description: str
    status: str                    # pending/completed
    checklist: list[ChecklistItem] # Implementation items list
    failure_count: int = 0
    revert_reason: str = ""
```

---

## CHANGELOG

| Version | Description | Link |
|---------|-------------|------|
| v1.16 | Task implementation checklist (evidence validation, empty implementation detection, reason requirement) | [v1.16](updates/v1.16.md) |
| v1.14 | Distribution structure reorganization (private/public 2-repo system, template consolidation) | [v1.14](updates/v1.14.md) |
| v1.13-1 | Phase failure error handling audit (contract violation detection enhancement) | [v1.13-1](updates/v1.13-1_phase_failure_handling_audit.md) |
| v1.13 | Phase contract formalization (instruction/expected_payload/tools_used clarification) | [v1.13](updates/v1.13.md) |
| v1.12 | Checkpoint persistence & legacy tool removal | [v1.12](updates/v1.12.md) |
| v1.11 | submit_phase unification & task orchestration (17 tools→1, compaction resilience, server-enforced task management) | [v1.11](updates/v1.11.md) |
| v1.10 | Individual Phase Checks (per-phase checks, VERIFICATION/IMPACT separation, gate_level restructuring) | [v1.10](updates/v1.10.md) |
| v1.9 | Performance Optimization (sync_index batch processing - 15-20s reduction) | [v1.9](updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode (--only-explore) | [v1.8](updates/v1.8_ja.md) |
| v1.7 | Parallel Execution (27-35s reduction) | [v1.7](updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle (stale warning, begin_phase_gate) | [v1.6](updates/v1.6_ja.md) |
| v1.5 | Quality Review | [v1.5](updates/v1.5_ja.md) |
| v1.4 | Intervention System | [v1.4](updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1_ja.md) |

For documentation management rules, see [DOCUMENTATION_RULES.md](DOCUMENTATION_RULES.md).
