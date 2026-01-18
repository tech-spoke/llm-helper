# Code Intelligence MCP Server - Design Document v1.3

> This document describes the complete system specification as of v1.3.
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
| **Improvement Cycle** | Learn from failures via DecisionLog + OutcomeLog |
| **Project Isolation** | Independent learning data per project |
| **Two-Layer Context** | Static project rules + dynamic task-specific rules |
| **Garbage Detection** | OverlayFS isolates changes for review before commit |

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

## Phase Gates

The system divides implementation into 6 phases with server verification at each transition.

```
EXPLORATION → SEMANTIC → VERIFICATION → IMPACT_ANALYSIS → READY → PRE_COMMIT
     │            │            │               │              │          │
     │            │            │               │              │          └─ Review & commit
     │            │            │               │              └─ Implementation allowed
     │            │            │               └─ Verify change impact
     │            │            └─ Verify hypotheses with code
     │            └─ Semantic search for missing info
     └─ Explore codebase with tools
```

### Phase Details

| Phase | Purpose | Allowed Tools | Transition Condition |
|-------|---------|---------------|---------------------|
| EXPLORATION | Understand codebase | query, find_definitions, find_references, search_text, analyze_structure | Server evaluation "high" |
| SEMANTIC | Fill information gaps | semantic_search | submit_semantic completed |
| VERIFICATION | Verify hypotheses | All exploration tools | submit_verification completed |
| IMPACT_ANALYSIS | Check change impact | analyze_impact | All must_verify files confirmed |
| READY | Implementation | Edit, Write (explored files only) | - |
| PRE_COMMIT | Review changes | review_changes, finalize_changes | Overlay enabled |

### Markup Relaxation

Pure markup files have relaxed requirements:

| Extensions | Relaxation |
|------------|------------|
| `.html`, `.htm`, `.css`, `.scss`, `.sass`, `.less`, `.md` | ✅ Applied |
| `.blade.php`, `.vue`, `.jsx`, `.tsx`, `.svelte` | ❌ Not applied (contains logic) |

**Relaxed requirements:**
- `symbols_identified`: Not required
- `find_definitions`/`find_references`: Not required
- `search_text` alone is sufficient

**v1.3 Addition**: Even in relaxed mode, cross-references are detected:
- CSS → HTML: Class/ID usage
- HTML → CSS: Style definitions
- HTML → JS: getElementById, querySelector

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

### Phase Completion

| Tool | Description |
|------|-------------|
| `submit_understanding` | Complete EXPLORATION phase |
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

### Garbage Detection (v1.2)

| Tool | Description |
|------|-------------|
| `submit_for_review` | Transition to PRE_COMMIT phase |
| `review_changes` | Show all file changes |
| `finalize_changes` | Keep/discard files and commit |
| `merge_to_main` | Merge task branch to main |
| `cleanup_stale_overlays` | Clean up interrupted sessions |

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
| `--no-verify` | - | Skip post-implementation verification |
| `--only-verify` | `-v` | Run verification only |
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: h(igh), m(iddle), l(ow), a(uto), n(one) |
| `--quick` | `-q` | Skip exploration (= `-g=n`) |
| `--doc-research=PROMPTS` | - | Specify research prompts |
| `--no-doc-research` | - | Skip document research |
| `--clean` | `-c` | Cleanup stale overlays |
| `--rebuild` | `-r` | Force full re-index |

### Execution Flow

```
Step -1: Flag Check
    └─ Parse command options

Step 0: Failure Check
    └─ Auto-detect if previous session failed

Step 1: Intent Classification
    └─ IMPLEMENT / MODIFY / INVESTIGATE / QUESTION

Step 2: Session Start
    ├─ Load project_rules from context.yml
    ├─ Setup OverlayFS (if enabled)
    └─ Sync ChromaDB (if needed)

Step 2.5: DOCUMENT_RESEARCH (v1.3)
    ├─ Spawn sub-agent with research prompt
    └─ Extract mandatory_rules from docs/

Step 3: QueryFrame Setup
    └─ Extract structured slots with quote verification

Step 4: EXPLORATION
    ├─ Use find_definitions, find_references, etc.
    ├─ Acknowledge mandatory_rules
    └─ submit_understanding

Step 5: Symbol Validation
    └─ Verify NL→Symbol relevance via embedding

Step 6: SEMANTIC (if confidence low)
    └─ semantic_search → submit_semantic

Step 7: VERIFICATION (if SEMANTIC executed)
    └─ Verify hypotheses with code → submit_verification

Step 8: IMPACT_ANALYSIS
    ├─ analyze_impact for target files
    └─ Confirm all must_verify files

Step 9: READY
    ├─ check_write_target before each Edit/Write
    └─ Implementation

Step 9.5: POST_IMPLEMENTATION_VERIFICATION
    ├─ Select verifier based on file types
    ├─ Run verifier prompts (.code-intel/verifiers/)
    └─ Loop back to Step 9 on failure (skip with --no-verify)

Step 10: PRE_COMMIT (if overlay enabled)
    ├─ review_changes
    └─ finalize_changes (keep/discard)

Step 11: Merge (optional)
    └─ merge_to_main
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
| fuse-overlayfs | No | Garbage detection (Linux) |

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
| OverlayManager | `tools/overlay_manager.py` | Garbage detection |

### Key Data Structures

```python
class SessionState:
    session_id: str
    intent: str           # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    phase: Phase          # Current phase
    query_frame: QueryFrame
    overlay_enabled: bool
    gate_level: str       # high/middle/low/auto/none

class Phase(Enum):
    EXPLORATION = "exploration"
    SEMANTIC = "semantic"
    VERIFICATION = "verification"
    IMPACT_ANALYSIS = "impact_analysis"
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
[POST_IMPL_VERIFY] → [verifiers/*.md]
    ↓
[PRE_COMMIT] → [review_changes] → [finalize_changes]
    ↓
[merge_to_main]
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
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3.md) |
| v1.2 | OverlayFS, Gate Levels | [v1.2](updates/v1.2.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1.md) |

For documentation rules, see [DOCUMENTATION_RULES.md](DOCUMENTATION_RULES.md).
