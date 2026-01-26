# Code Intelligence MCP Server - Design Document v1.10

> This document describes the complete system specification as of v1.10.
> For version history, see [CHANGELOG](#changelog).

---

## Table of Contents

1. [Overview](#overview)
2. [Design Philosophy](#design-philosophy)
3. [Architecture](#architecture)
4. [Phase Gates](#phase-gates)
5. [Two-Layer Context](#two-layer-context)
6. [Tools Reference](#tools-reference)
7. [/code Skill Flow](#code-skill-flow)
8. [Setup Guide](#setup-guide)
9. [Configuration](#configuration)
10. [Internal Reference](#internal-reference)
11. [CHANGELOG](#changelog)

---

## Overview

Code Intelligence MCP Server provides guardrails for LLMs to accurately understand codebases and safely implement changes. It bridges the gap between Cursor-like IDE behavior and Claude Code's default approach.

| Caller | Default Behavior |
|--------|------------------|
| **Cursor** | Understands entire codebase before modifying |
| **Claude Code** | Tends to modify only specific locations |

This server makes Claude Code behave more like Cursor by enforcing structured exploration before implementation.

---

## Design Philosophy

```
Don't let the LLM decide. Design so it can't proceed without compliance.
And have a mechanism to learn from failures.
```

### Core Principles

| Principle | Implementation |
|-----------|----------------|
| **Phase Enforcement** | Tool restrictions by phase (no shortcuts) |
| **Server Evaluation** | Server calculates confidence (not LLM self-report) |
| **Quote Verification** | Validates LLM-extracted quotes against original query |
| **Embedding Verification** | Objective NL→Symbol relevance via vector similarity |
| **Write Restriction** | Only explored files can be modified |
| **Parallel Execution Guard** | Blocks other /code calls during active session (1 project = 1 session) |
| **Improvement Cycle** | Learn from failures via DecisionLog + OutcomeLog |
| **Project Isolation** | Independent learning data per project |
| **Two-Layer Context** | Static project rules + dynamic task-specific rules |
| **Garbage Detection** | Git branch isolates changes for review before commit |
| **Intervention System** | Retry-based intervention for stuck verification loops (v1.4) |
| **Quality Review** | Post-implementation quality check with revert-to-READY loop (v1.5) |
| **Branch Lifecycle** | Stale branch warnings, auto-deletion on failure (v1.6) |

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

## Processing Flow

Processing consists of 4 layers (v1.10):

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Preparation Phase (Skill controlled - code.md)                             │
└─────────────────────────────────────────────────────────────────────────────┘
Step -1:  Flag Check              Parse command options
Step 1:   Intent Classification   Classify as IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
Step 2:   Session Start           Start session, get project_rules (no branch yet)
Step 2.5: DOCUMENT_RESEARCH       Document research (sub-agent) ← skip with --no-doc-research
Step 3:   QueryFrame Setup        Decompose request into structured slots
Step 3.5: begin_phase_gate        Start phase gates, create branch, stale warning

┌─────────────────────────────────────────────────────────────────────────────┐
│  Exploration Phase (Server enforced)                                        │
└─────────────────────────────────────────────────────────────────────────────┘
Step 4:   EXPLORATION             Source investigation (find_definitions, find_references, search_text)

          ★ v1.10: Individual phase check approach
          ↓
Step 4.5: Q1 Check                Is additional information collection needed?
          ├─ YES → Execute SEMANTIC
          └─ NO → Skip SEMANTIC
          ↓
Step 5:   SEMANTIC                Semantic search (only if Q1=YES)
          ↓
Step 5.5: Q2 Check                Are there hypotheses that need verification?
          ├─ YES → Execute VERIFICATION
          └─ NO → Skip VERIFICATION
          ↓
Step 6:   VERIFICATION            Hypothesis verification (only if Q2=YES)
          ↓
Step 6.5: Q3 Check                Is impact range confirmation needed?
          ├─ YES → Execute IMPACT_ANALYSIS
          └─ NO → Skip IMPACT_ANALYSIS
          ↓
Step 7:   IMPACT_ANALYSIS         Impact range analysis (only if Q3=YES)
          ↓
          [If --only-explore: End here, report findings to user]

┌─────────────────────────────────────────────────────────────────────────────┐
│  Implementation & Verification Phase (Server enforced)                      │
└─────────────────────────────────────────────────────────────────────────────┘
Step 8:   READY                   Implementation (Edit/Write/Bash allowed)
Step 8.5: POST_IMPL_VERIFY        Post-implementation verification (verifier prompts)
                                  ← skip with --no-verify
                                  On failure, loop back to Step 8 (max 3 times)

┌─────────────────────────────────────────────────────────────────────────────┐
│  Commit & Quality Phase (Server enforced)                                   │
└─────────────────────────────────────────────────────────────────────────────┘
Step 9:   PRE_COMMIT              Pre-commit review
          ├─ review_changes       Garbage detection (garbage_detection.md)
          └─ finalize_changes     Keep/discard decision + commit preparation

Step 9.5: QUALITY_REVIEW          Quality review ← skip with --no-quality
          ├─ quality_review.md    Checklist review
          └─ submit_quality_review
              ├─ Issues found → Revert to READY (fix → POST_IMPL_VERIFY → PRE_COMMIT → QUALITY_REVIEW)
              └─ No issues → ★Commit execution here → Next

┌─────────────────────────────────────────────────────────────────────────────┐
│  Completion                                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
Step 10:  merge_to_base           Merge task branch to original branch
                                  Session complete, report results to user
```

### 1. Preparation (Skill controlled)

Controlled by skill prompt (code.md). Server not involved.

| Step | Description | Skip |
|------|-------------|------|
| Step -1: Flag Check | Parse command options (`--quick`, `--only-explore`, etc.) | - |
| Step 1: Intent | Classify as IMPLEMENT / MODIFY / INVESTIGATE / QUESTION | - |
| Step 2: Session Start | Start session, get project_rules (no branch yet) | - |
| Step 2.5: **DOCUMENT_RESEARCH** | Sub-agent researches design docs, extracts mandatory_rules | `--no-doc-research` |
| Step 3: QueryFrame | Decompose request into structured slots with Quote verification | - |

**DOCUMENT_RESEARCH details:**
- Uses Claude Code Task tool (Explore agent)
- Researches documents in `docs/` directory
- Extracts task-specific rules, constraints, and dependencies
- Referenced as `mandatory_rules` in subsequent phases

**--only-explore flag (v1.8):**
- When detected in Step -1, sets `skip_implementation=true` flag
- Passes `skip_implementation` parameter to Session Start (Step 2)
- After IMPACT_ANALYSIS (Step 8) completion, skips implementation phases and exits

### 2. Phase Gate Start (v1.6)

After preparation, `begin_phase_gate` creates the task branch and starts phase gates.

**Stale Branch Detection:**
- If `llm_task_*` branches exist while not on a task branch, user intervention is required
- Three options: Delete, Merge, or Continue as-is

### 3. Phase Gates (Server enforced)

MCP server enforces phase transitions. LLM cannot skip arbitrarily.

#### Phase Matrix

| Option | Doc Research | Source Explore | Implement | Verify | Intervene | Garbage | Quality | Branch |
|--------|:-------:|:-------:|:---------:|:------:|:---------:|:-------:|:-------:|:------:|
| (default) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--only-explore` / `-e` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `--no-verify` | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--fast` / `-f` | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `--no-doc-research` | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Legend**:
- **Doc Research**: DOCUMENT_RESEARCH (Step 2.5)
- **Source Explore**: EXPLORATION, SEMANTIC, VERIFICATION, IMPACT_ANALYSIS (Steps 4-7)

#### Phase Details (v1.10: Individual Check Approach)

| Phase | Purpose | Allowed Tools | Transition Condition |
|-------|---------|---------------|---------------------|
| EXPLORATION | Understand codebase | query, find_definitions, find_references, search_text, analyze_structure | EXPLORATION completed |
| Q1 Check | Determine SEMANTIC necessity | check_phase_necessity(phase="SEMANTIC") | needs_more_information decision |
| SEMANTIC | Fill information gaps | semantic_search | submit_semantic completed |
| Q2 Check | Determine VERIFICATION necessity | check_phase_necessity(phase="VERIFICATION") | has_unverified_hypotheses decision |
| VERIFICATION | Verify hypotheses | All exploration tools | submit_verification completed |
| Q3 Check | Determine IMPACT_ANALYSIS necessity | check_phase_necessity(phase="IMPACT_ANALYSIS") | needs_impact_analysis decision |
| IMPACT_ANALYSIS | Check change impact | analyze_impact | submit_impact_analysis completed |
| READY | Implementation | Edit, Write (explored files only) | - |
| POST_IMPL_VERIFY | Run verification | Verifier prompts (Playwright, pytest) | Success (3 failures trigger intervention) |
| PRE_COMMIT | Review changes | review_changes, finalize_changes | Garbage removed |
| QUALITY_REVIEW | Quality check | submit_quality_review (Edit/Write forbidden) | No issues → complete, Issues → revert to READY |

### Markup Relaxation

When targeting only pure markup files (HTML, CSS, etc.), EXPLORATION phase requirements are relaxed.

**What relaxation means:**
- Code exploration can be mostly skipped (text search only is OK)
- Symbol definition and reference analysis not required
- Faster progression to implementation phase

| Extensions | Relaxation |
|------------|------------|
| `.html`, `.htm`, `.css`, `.scss`, `.sass`, `.less`, `.md` | ✅ Applied |
| `.blade.php`, `.vue`, `.jsx`, `.tsx`, `.svelte` | ❌ Not applied (contains logic) |

**v1.3 Addition - Cross-Reference Detection:**

Even in relaxed mode, IMPACT_ANALYSIS detects related files:
- Modifying CSS → Detects HTML files using that class/ID
- Modifying HTML → Detects CSS files with style definitions
- Modifying HTML → Detects JS files with DOM operations

These are reported as `should_verify` (recommended to check).

---

## Two-Layer Context

v1.3 introduces a clear separation between static and dynamic context.

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: project_rules (Session Start)                         │
│  └── Always-needed baseline rules (lightweight, cached)         │
│      • Source: CLAUDE.md                                        │
│      • Content: DO/DON'T list                                   │
│      • Purpose: Project-wide "common sense"                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: mandatory_rules (DOCUMENT_RESEARCH phase)             │
│  └── Task-specific detailed rules (dynamic, per-task)          │
│      • Source: docs/**/*.md via Sub-agent research              │
│      • Content: Task-specific constraints with file:line refs   │
│      • Purpose: "Rules for THIS implementation"                 │
└─────────────────────────────────────────────────────────────────┘
```

### Comparison

| Aspect | project_rules | mandatory_rules |
|--------|---------------|-----------------|
| Source | CLAUDE.md | docs/**/*.md |
| Timing | Session Start | DOCUMENT_RESEARCH phase |
| Content | Generic DO/DON'T | Task-specific constraints |
| Generation | Pre-cached summary | Sub-agent live research |
| Skippable | No | Yes (`--no-doc-research`) |

### DOCUMENT_RESEARCH Phase

Uses Claude Code's Task tool with Explore agent to research design documents:

1. Sub-agent spawned with research prompt
2. Reads relevant documents from `docs/`
3. Extracts rules, dependencies, warnings
4. Returns `mandatory_rules` with source citations

**Configuration** (`.code-intel/context.yml`):
```yaml
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"
```

---

## Tools Reference

### Session Management

| Tool | Description |
|------|-------------|
| `start_session` | Start session with intent and query |
| `get_session_status` | Get current phase and status |
| `set_query_frame` | Set structured query slots |

### Code Exploration

| Tool | Description |
|------|-------------|
| `query` | General natural language query |
| `find_definitions` | Symbol definition search (ctags) |
| `find_references` | Reference search (ripgrep) |
| `search_text` | Text pattern search |
| `analyze_structure` | Code structure analysis (tree-sitter) |
| `get_symbols` | Get symbol list for a file |
| `semantic_search` | Vector search in Forest/Map |

### Phase Control (v1.10)

| Tool | Description |
|------|-------------|
| `check_phase_necessity` | Check phase necessity before each phase (Q1/Q2/Q3) |
| `submit_semantic` | Complete SEMANTIC phase |
| `submit_verification` | Complete VERIFICATION phase |
| `submit_impact_analysis` | Complete IMPACT_ANALYSIS phase |

### Implementation Control

| Tool | Description |
|------|-------------|
| `analyze_impact` | Analyze change impact before implementation |
| `check_write_target` | Verify file can be modified |
| `add_explored_files` | Add files to explored list |
| `revert_to_exploration` | Return to EXPLORATION phase |
| `validate_symbol_relevance` | Verify symbol relevance via embedding |

### Garbage Detection & Quality Review (v1.2, v1.5)

| Tool | Description |
|------|-------------|
| `submit_for_review` | Transition to PRE_COMMIT phase |
| `review_changes` | Show all file changes |
| `finalize_changes` | Keep/discard files and commit |
| `submit_quality_review` | Report quality review result (v1.5) |
| `merge_to_base` | Merge task branch to base branch |
| `cleanup_stale_branches` | Clean up interrupted sessions |

### Branch Lifecycle (v1.6)

| Tool | Description |
|------|-------------|
| `begin_phase_gate` | Start phase gates, create branch (with stale branch check) |
| `cleanup_stale_branches` | Checkout to base branch, delete all `llm_task_*` branches |

### Index & Learning

| Tool | Description |
|------|-------------|
| `sync_index` | Sync ChromaDB index |
| `update_context` | Update context.yml summaries |
| `record_outcome` | Record success/failure |
| `get_outcome_stats` | Get learning statistics |

---

## /code Skill Flow

### Command Options

| Long | Short | Description |
|------|-------|-------------|
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: f(ull), a(uto) [default: auto] (v1.10) |
| `--no-verify` | - | Skip post-implementation verification (and intervention) |
| `--no-quality` | - | Skip quality review (v1.5) |
| `--only-verify` | `-v` | Run verification only |
| `--only-explore` | `-e` | Run exploration only (skip implementation) (v1.8) |
| `--fast` | `-f` | Skip exploration with branch |
| `--quick` | `-q` | Skip exploration, no branch |
| `--doc-research=PROMPTS` | - | Specify research prompts |
| `--no-doc-research` | - | Skip document research |
| `--no-intervention` | `-ni` | Skip intervention system (v1.4) |
| `--clean` | `-c` | Checkout to base branch, delete stale `llm_task_*` branches |
| `--rebuild` | `-r` | Force full re-index |

**gate_level options (v1.10):**
- `--gate=full` or `-g=f`: Ignore all checks and execute all phases
- `--gate=auto` or `-g=a`: Check before each phase (default)

### Execution Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ Preparation Phase (Skill controlled - code.md)                  │
└─────────────────────────────────────────────────────────────────┘
Step -1: Flag Check
    └─ Parse command options (--quick, --only-explore, --no-verify, etc.)
       When --only-explore / -e detected → set skip_implementation=true flag

Step 1: Intent Classification
    └─ Classify as IMPLEMENT / MODIFY / INVESTIGATE / QUESTION
       ★ v1.8: Intent=INVESTIGATE or QUESTION → Automatic exploration-only mode
          (skip_implementation=true, skip_branch=true)

Step 2: Session Start
    ├─ Load project_rules from context.yml
    ├─ Set skip_implementation flag:
    │  - Intent=INVESTIGATE/QUESTION → skip_implementation=true (auto)
    │  - --only-explore flag → skip_implementation=true (explicit)
    │  - Intent=IMPLEMENT/MODIFY → skip_implementation=false (default)
    └─ Sync ChromaDB (if needed)
    ※ Branch creation happens in Step 3.5 (v1.6)

Step 2.5: DOCUMENT_RESEARCH (v1.3)
    ├─ Spawn sub-agent with research prompt
    └─ Extract mandatory_rules from docs/
    ← skip with --no-doc-research

Step 3: QueryFrame Setup
    └─ Extract structured slots with quote verification

Step 3.5: begin_phase_gate (v1.6)
    ├─ Check for stale branches
    ├─ User intervention if stale branches exist (delete/merge/continue)
    └─ Determine branch creation:
       - skip_implementation=true → skip_branch=true (no branch)
       - skip_implementation=false → Create task branch (normal flow)
       ★ v1.8: Exploration-only mode skips branch creation

┌─────────────────────────────────────────────────────────────────┐
│ Exploration Phase (Server enforced)                             │
└─────────────────────────────────────────────────────────────────┘
Step 4: EXPLORATION
    ├─ Use find_definitions, find_references, search_text, etc.
    └─ Acknowledge mandatory_rules

★ v1.10: Individual phase check approach

Step 4.5: Q1 Check - Determine SEMANTIC necessity
    ├─ check_phase_necessity(phase="SEMANTIC", assessment={...})
    │  Question: Is additional information collection needed?
    │  - needs_more_information: true/false
    │  - needs_more_information_reason: "..."
    ├─ gate_level="full" → Force execute
    ├─ gate_level="auto" + needs_more_information=true → Execute SEMANTIC
    └─ gate_level="auto" + needs_more_information=false → Skip SEMANTIC

Step 5: SEMANTIC (only if Q1=YES)
    └─ semantic_search → submit_semantic

Step 5.5: Q2 Check - Determine VERIFICATION necessity
    ├─ check_phase_necessity(phase="VERIFICATION", assessment={...})
    │  Question: Are there hypotheses that need verification?
    │  - has_unverified_hypotheses: true/false
    │  - has_unverified_hypotheses_reason: "..."
    ├─ gate_level="full" → Force execute
    ├─ gate_level="auto" + has_unverified_hypotheses=true → Execute VERIFICATION
    └─ gate_level="auto" + has_unverified_hypotheses=false → Skip VERIFICATION

Step 6: VERIFICATION (only if Q2=YES)
    └─ Verify hypotheses with code → submit_verification

Step 6.5: Q3 Check - Determine IMPACT_ANALYSIS necessity
    ├─ check_phase_necessity(phase="IMPACT_ANALYSIS", assessment={...})
    │  Question: Is change impact range confirmation needed?
    │  - needs_impact_analysis: true/false
    │  - needs_impact_analysis_reason: "..."
    ├─ gate_level="full" → Force execute
    ├─ gate_level="auto" + needs_impact_analysis=true → Execute IMPACT_ANALYSIS
    └─ gate_level="auto" + needs_impact_analysis=false → Skip IMPACT_ANALYSIS

Step 7: IMPACT_ANALYSIS (only if Q3=YES)
    ├─ analyze_impact for target files
    └─ submit_impact_analysis

    ★ v1.8: If skip_implementation=true (Intent=INVESTIGATE/QUESTION or --only-explore):
       - submit_impact_analysis returns exploration_complete=true
       - Report findings to user and complete (skip Step 8 onwards)
       - No branch created, no implementation phases

┌─────────────────────────────────────────────────────────────────┐
│ Implementation & Verification Phase (Server enforced)           │
└─────────────────────────────────────────────────────────────────┘
Step 8: READY
    ├─ check_write_target before each Edit/Write
    └─ Implementation
    → submit_for_review to PRE_COMMIT

Step 8.5: POST_IMPL_VERIFY
    ├─ Select verifier based on file types
    ├─ Run verifier prompts (.code-intel/verifiers/)
    ├─ Loop back to Step 8 on failure
    └─ Intervention on 3 consecutive failures (v1.4)
    ← skip with --no-verify

┌─────────────────────────────────────────────────────────────────┐
│ Commit & Quality Phase (Server enforced)                        │
└─────────────────────────────────────────────────────────────────┘
Step 9: PRE_COMMIT (garbage detection + commit preparation)
    ├─ review_changes (review with garbage_detection.md)
    └─ finalize_changes (keep/discard decision + commit preparation)
       ★ v1.8: Prepare commit only (execution after QUALITY_REVIEW)

Step 9.5: QUALITY_REVIEW (v1.5, order changed in v1.8)
    ├─ quality_review.md checklist
    ├─ Issues found → submit_quality_review(issues_found=true)
    │                → Revert to Step 8 (READY) (discard prepared commit)
    └─ No issues → submit_quality_review(issues_found=false)
                 → ★Commit execution happens here → Step 10
    ← skip with --no-quality

┌─────────────────────────────────────────────────────────────────┐
│ Completion                                                      │
└─────────────────────────────────────────────────────────────────┘
Step 10: merge_to_base
    └─ Merge task branch to original branch
       Session complete, report results to user
```

### Verifier System

Post-implementation verification uses verifier prompts stored in `.code-intel/verifiers/`:

| Verifier | File Types | Method |
|----------|-----------|--------|
| `backend.md` | `.py`, `.js`, `.ts`, `.php` (non-UI) | pytest, npm test |
| `html_css.md` | `.html`, `.css`, `.vue`, `.jsx`, `.tsx` (UI) | Playwright |
| `generic.md` | Config, docs, other | Manual check |

**Selection logic:**
- Modified files determine verifier selection
- Mixed file types → primary category or multiple verifiers
- Verification failure → loop back to READY (max 3 attempts)

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
└── .code-intel/
    ├── config.json
    ├── context.yml
    ├── chroma/
    ├── agreements/
    └── logs/
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

### Step 4: Copy Skills (optional)

```bash
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 5: Restart Claude Code

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
# Layer 1: Project rules (always applied)
project_rules:
  source: "CLAUDE.md"
  summary: |
    DO:
    - Use Service layer for business logic
    DON'T:
    - Write complex logic in Controllers
  content_hash: "abc123..."

# Layer 2: Document research settings
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

# Impact analysis document search
document_search:
  include_patterns:
    - "**/*.md"
  exclude_patterns:
    - "node_modules/**"

last_synced: "2025-01-18T10:00:00"
```

---

## Internal Reference

### Core Modules

| Module | File | Responsibility |
|--------|------|----------------|
| SessionState | `tools/session.py` | Session state, phase transitions |
| QueryFrame | `tools/query_frame.py` | NL → structured query |
| ChromaDB Manager | `tools/chromadb_manager.py` | Forest/Map management |
| ImpactAnalyzer | `tools/impact_analyzer.py` | Change impact analysis |
| ContextProvider | `tools/context_provider.py` | Project rules & doc research |
| BranchManager | `tools/branch_manager.py` | Git branch isolation |

### Key Data Structures

```python
class SessionState:
    session_id: str
    intent: str           # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    phase: Phase          # Current phase
    query_frame: QueryFrame
    task_branch_enabled: bool
    gate_level: str       # full/auto (v1.10)
    phase_assessments: dict  # Record of each checkpoint (v1.10)

class Phase(Enum):
    EXPLORATION = "exploration"
    SEMANTIC = "semantic"
    VERIFICATION = "verification"        # v1.10: Separated
    IMPACT_ANALYSIS = "impact_analysis"  # v1.10: Separated
    READY = "ready"
    PRE_COMMIT = "pre_commit"

@dataclass
class DocResearchConfig:
    enabled: bool = True
    docs_path: list[str]
    default_prompts: list[str]
```

### Data Flow

```
[User Request]
    ↓
[/code skill] → [start_session] → [SessionState]
    ↓                                   ↓
[DOCUMENT_RESEARCH] ← [Task tool + Explore agent]
    ↓
[set_query_frame] → [QueryFrame with quote verification]
    ↓
[begin_phase_gate] → [Stale branch check] → [Branch created]
    ↓
[EXPLORATION] → [find_definitions/references] → [submit_understanding]
    ↓
[Symbol Validation] → [Embedding similarity check]
    ↓
[SEMANTIC/VERIFICATION] → (if needed)
    ↓
[IMPACT_ANALYSIS] → [analyze_impact] → [submit_impact_analysis]
    ↓
[READY] → [check_write_target] → [Edit/Write]
    ↓
[POST_IMPL_VERIFY] → [verifiers/*.md] → (3 failures → intervention)
    ↓
[PRE_COMMIT] → [review_changes] → [finalize_changes]
    ↓
[QUALITY_REVIEW] → [quality_review.md] → (revert on issues)
    ↓
[merge_to_base] (to original branch)
```

### Improvement Cycle

```python
# Automatic failure detection at /code start
record_outcome(
    session_id="...",
    outcome="failure",
    phase_at_outcome="READY",
    analysis={"root_cause": "...", "user_feedback": "..."}
)

# Failure pattern analysis
get_improvement_insights(limit=100)
# Returns: tool_failure_correlation, risk_level_correlation, etc.
```

---

## CHANGELOG

For version history and detailed changes:

| Version | Description | Link |
|---------|-------------|------|
| v1.10 | Individual Phase Checks (individual necessity checks before each phase, VERIFICATION/IMPACT separation, gate_level redesign - saves 20-60s) | [v1.10](updates/v1.10_ja.md) |
| v1.9 | Performance Optimization (sync_index batch, VERIFICATION+IMPACT integration - saves 15-20s) | [v1.9](updates/v1.9.md) |
| v1.8 | Exploration-Only Mode (--only-explore) | [v1.8](updates/v1.8.md) |
| v1.7 | Parallel Execution (search_text, Read, Grep - saves 27-35s) | [v1.7](updates/v1.7.md) |
| v1.6 | Branch Lifecycle (stale warning, begin_phase_gate) | [v1.6](updates/v1.6.md) |
| v1.5 | Quality Review (revert-to-READY loop) | [v1.5](updates/v1.5.md) |
| v1.4 | Intervention System | [v1.4](updates/v1.4.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3.md) |
| v1.2 | Git Branch Isolation | [v1.2](updates/v1.2.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1.md) |

For documentation rules, see [DOCUMENTATION_RULES.md](DOCUMENTATION_RULES.md).
