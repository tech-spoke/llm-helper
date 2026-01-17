# Code Intelligence MCP Server v1.0 Design Document

> **For v1.1 additions, see the [v1.1 Additions](#v11-additions) section.**
> **For v1.2 additions, see the [v1.2 Additions](#v12-additions) section.**

## Overview

Code Intelligence MCP Server provides guardrails for LLMs (Large Language Models) to accurately understand codebases and safely implement changes.

### Design Philosophy

1. **Phase-Gated Execution**: Enforces the order of exploration → verification → implementation, preventing "implement without investigating"
2. **Forest/Map Architecture**: Two-layer structure of entire codebase (forest) and success patterns (map)
3. **Improvement Cycle**: Automatically records failures and uses them for system improvement
4. **LLM Delegation + Server Verification**: Hybrid approach where server verifies LLM decisions

---

## Architecture

### Forest/Map Two-Layer Structure

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
│  │  │  All code chunks │  │  Successful agreements│  ││
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

### Forest

- **Purpose**: Vectorize entire project code for searchability
- **Contents**: AST-chunked code fragments
- **Sync**: Incremental sync via SHA256 fingerprinting
- **Use**: Semantic search, code understanding

### Map

- **Purpose**: Remember and reuse successful patterns
- **Contents**: Successful NL→Symbol pairs, agreements
- **Update**: Automatically added on `/outcome success`
- **Use**: Exploration shortcuts, reliable suggestions

---

## Phase Gates

Divides the LLM's implementation process into 5 phases, with server verification of each phase completion.

```
EXPLORATION → SEMANTIC → VERIFICATION → IMPACT_ANALYSIS → READY
    │            │            │               │              │
    │            │            │               │              └─ Implementation allowed
    │            │            │               └─ v1.1: Impact verification
    │            │            └─ Hypothesis verification
    │            └─ Semantic search
    └─ Code exploration
```

### Phase Details

| Phase | Purpose | Allowed Tools |
|-------|---------|---------------|
| EXPLORATION | Understand codebase | query, find_definitions, find_references, search_text, analyze_structure |
| SEMANTIC | Supplement missing info | semantic_search (ChromaDB) |
| VERIFICATION | Verify hypotheses | All exploration tools |
| IMPACT_ANALYSIS | v1.1: Verify change impact | analyze_impact, submit_impact_analysis |
| READY | Implementation allowed | check_write_target, add_explored_files |

### Phase Transition Conditions

```
EXPLORATION → SEMANTIC
  Condition: Server evaluation is "low" or consistency error

EXPLORATION → READY
  Condition: Server evaluation is "high" and consistency OK

SEMANTIC → VERIFICATION
  Condition: submit_semantic completed

VERIFICATION → READY
  Condition: submit_verification completed
```

---

## QueryFrame

Structures natural language requests and clarifies "what is missing".

### Slot Structure

| Slot | Description | Example |
|------|-------------|---------|
| target_feature | Target feature | "Login functionality" |
| trigger_condition | Trigger condition | "When password is empty" |
| observed_issue | Observed problem | "No error displayed" |
| desired_action | Expected behavior | "Add validation" |

### Quote Verification

Server verifies that the `quote` extracted by LLM exists in the original query. This prevents hallucination.

```json
{
  "target_feature": {
    "value": "Login functionality",
    "quote": "login functionality"  // Must exist in original query
  }
}
```

### Risk Level

| Level | Condition | Exploration Requirements |
|-------|-----------|-------------------------|
| HIGH | MODIFY + issue unknown | Strict: all slots required |
| MEDIUM | IMPLEMENT or partial unknown | Standard requirements |
| LOW | INVESTIGATE or all info available | Minimum OK |

---

## Improvement Cycle

A mechanism to automatically record failures and use them for system improvement.

### Two Logs

| Log | File | Trigger |
|-----|------|---------|
| DecisionLog | `.code-intel/logs/decisions.jsonl` | On query tool execution (automatic) |
| OutcomeLog | `.code-intel/logs/outcomes.jsonl` | On failure detection (automatic) or /outcome (manual) |

### Automatic Failure Detection

At `/code` skill start, automatically determines if the current request indicates "previous failure".

**Detection Patterns:**
- Redo requests: "redo", "again", "retry"
- Negation/dissatisfaction: "wrong", "not that", "incorrect"
- Malfunction: "doesn't work", "error occurs", "crashes"
- Bug reports: "there's a bug", "something's wrong"

### Analysis Functions

```python
get_session_analysis(session_id)      # Combine decision + outcome
get_improvement_insights(limit=100)   # Failure pattern analysis
```

---

## /code Skill Flow

```
Step 0: Failure Check
    ├─ Check previous session
    └─ Detect failure pattern → Auto-record

Step 1: Intent Determination
    └─ IMPLEMENT / MODIFY / INVESTIGATE / QUESTION

Step 2: Session Start
    └─ start_session

Step 3: QueryFrame Setup
    └─ set_query_frame

Step 4: EXPLORATION
    ├─ find_definitions (required)
    ├─ find_references (required)
    └─ submit_understanding

Step 5: Symbol Verification
    └─ validate_symbol_relevance

Step 6: SEMANTIC (if needed)
    └─ semantic_search → submit_semantic

Step 7: VERIFICATION (if needed)
    └─ Verify hypotheses → submit_verification

Step 8: READY
    ├─ check_write_target
    └─ Implementation (Edit/Write)
```

---

## MCP Tool List

### Session Management

| Tool | Description |
|------|-------------|
| start_session | Start session |
| get_session_status | Get current status |
| set_query_frame | Set QueryFrame |

### Exploration Tools

| Tool | Description |
|------|-------------|
| query | General query (via Router) |
| find_definitions | Symbol definition search (ctags) |
| find_references | Reference search (ripgrep) |
| search_text | Text search |
| analyze_structure | Structure analysis (tree-sitter) |
| get_symbols | Get symbol list |

### Phase Completion

| Tool | Description |
|------|-------------|
| submit_understanding | Complete EXPLORATION |
| submit_semantic | Complete SEMANTIC |
| submit_verification | Complete VERIFICATION |
| submit_impact_analysis | v1.1: Complete IMPACT_ANALYSIS |

### Verification & Control

| Tool | Description |
|------|-------------|
| validate_symbol_relevance | Verify symbol relevance |
| check_write_target | Verify write target |
| add_explored_files | Add explored files |
| revert_to_exploration | Return to EXPLORATION |

### Improvement Cycle

| Tool | Description |
|------|-------------|
| record_outcome | Record outcome |
| get_outcome_stats | Get statistics |

### ChromaDB

| Tool | Description |
|------|-------------|
| sync_index | Sync index |
| semantic_search | Semantic search |

---

## Project Structure

```
llm-helper/
├── code_intel_server.py      # MCP server main
├── tools/
│   ├── session.py            # Session management
│   ├── query_frame.py        # QueryFrame processing
│   ├── router.py             # Query routing
│   ├── chromadb_manager.py   # ChromaDB management
│   ├── ast_chunker.py        # AST chunking
│   ├── sync_state.py         # Sync state management
│   ├── ctags_tool.py         # ctags wrapper
│   ├── ripgrep_tool.py       # ripgrep wrapper
│   ├── treesitter_tool.py    # tree-sitter wrapper
│   ├── embedding.py          # Embedding calculation
│   ├── learned_pairs.py      # Learned pairs cache
│   ├── agreements.py         # Agreement management
│   └── outcome_log.py        # Improvement cycle log
├── .claude/
│   └── commands/
│       └── code.md           # /code skill definition
├── .code-intel/              # Project-specific data
│   ├── chroma/               # ChromaDB data
│   ├── agreements/           # Agreements (.md)
│   ├── logs/                 # DecisionLog, OutcomeLog
│   ├── config.json           # Configuration
│   └── sync_state.json       # Sync state
└── docs/
    ├── ja/
    │   ├── DESIGN_v1.0.md    # Overall design (Japanese)
    │   └── INTERNALS_v1.0.md # Internal details (Japanese)
    └── en/
        ├── DESIGN_v1.0.md    # Overall design (this document)
        └── INTERNALS_v1.0.md # Internal details (English)
```

---

## Setup

### Required External Tools

- Universal Ctags
- ripgrep
- Python 3.10+
- fuse-overlayfs (optional, for v1.2 garbage detection)

### Step 1: MCP Server Setup (once only)

```bash
# Clone repository
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper

# Setup server (venv, dependencies)
./setup.sh
```

### Step 2: Project Initialization (per project)

```bash
# Initialize target project (index entire project)
./init-project.sh /path/to/your-project

# Option: Index only specific directories
./init-project.sh /path/to/your-project --include=src,packages

# Option: Specify additional exclude patterns
./init-project.sh /path/to/your-project --exclude=tests,docs,*.log
```

This creates:

```
your-project/
└── .code-intel/
    ├── config.json       ← Configuration
    ├── chroma/           ← ChromaDB data (auto-generated)
    ├── agreements/       ← Agreements directory
    └── logs/             ← DecisionLog, OutcomeLog
```

### Step 3: Configure .mcp.json

Add the configuration output by `init-project.sh` to `.mcp.json`:

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

### Step 4: Setup Skills (optional)

```bash
mkdir -p /path/to/your-project/.claude/commands
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 5: Restart Claude Code

Restart to load the MCP server. Index is automatically built on first session start.

### Step 6: Essential Context (v1.1, automatic)

Design docs and project rules are **automatically detected and summarized**.

**How it works:**
1. On first `sync_index`, server auto-detects design docs and project rules
2. Creates `.code-intel/context.yml` with detected sources
3. Returns document content + prompt for LLM to generate summaries
4. LLM generates summaries and saves via `update_context` tool
5. On subsequent syncs, detects changes and regenerates summaries as needed

**Auto-detected locations:**
- Design docs: `docs/architecture/`, `docs/design/`, `docs/`
- Project rules: `CLAUDE.md`, `.claude/CLAUDE.md`, `CONTRIBUTING.md`

**Manual customization (optional):**

Edit `.code-intel/context.yml` to add `extra_notes`:

```yaml
essential_docs:
  source: "docs/architecture"
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: "..."                     # ← Auto-generated by LLM
      extra_notes: |                     # ← Optional: your implicit knowledge
        - Exception: Simple CRUD can bypass Service layer
```

---

## Configuration File

`.code-intel/config.json`:

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

---

## v1.1 Additions

v1.1 introduces the following features.

### Essential Context Auto-Provision

At session start, automatically provides design documents and project rules to the LLM.

**Purpose:**
- Solve the problem of LLMs skipping CLAUDE.md rules
- Prevent ignoring design documentation

**Configuration File:**

```yaml
# .code-intel/context.yml

# Design documents - summaries are auto-provided at session start
essential_docs:
  source: "docs/architecture"  # Directory containing design docs
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: |
        3-layer architecture (Controller/Service/Repository).
        Business logic must be in Service layer.
      content_hash: "abc123..."  # Auto-generated, used for change detection
      extra_notes: |
        # Manual notes (optional - supplement auto-generated summary)
        - Exception: Simple CRUD can bypass Service layer

# Project rules - DO/DON'T rules from CLAUDE.md or similar
project_rules:
  source: "CLAUDE.md"  # Source file for rules
  summary: |
    DO:
    - Use Service layer for business logic
    - Write tests for all features
    - Follow existing naming conventions

    DON'T:
    - Write complex logic in Controllers
    - Skip code review
    - Commit directly to main branch
  content_hash: "def456..."
  extra_notes: ""

last_synced: "2025-01-14T10:00:00"  # Auto-updated
```

**Key points:**
- `summary` can be manually written or LLM-generated
- `extra_notes` allows adding implicit knowledge not in the source doc
- `content_hash` enables change detection via `sync_index`
- At session start, `essential_context` is returned with these summaries

**Auto-detection:** If `context.yml` doesn't exist, the server detects common patterns:
- Design docs: `docs/architecture/`, `docs/design/`, `docs/`
- Project rules: `CLAUDE.md`, `.claude/CLAUDE.md`, `CONTRIBUTING.md`

**start_session Response:**

```json
{
  "success": true,
  "session_id": "abc123",
  "essential_context": {
    "design_docs": {
      "source": "docs/architecture",
      "summaries": [...]
    },
    "project_rules": {
      "source": "CLAUDE.md",
      "summary": "DO:\n- ...\nDON'T:\n- ..."
    }
  }
}
```

### Impact Analysis (IMPACT ANALYSIS Phase)

Before transitioning to READY phase, analyzes the impact of changes and enforces verification.

**Additional Phase:**

```
EXPLORATION → SEMANTIC → VERIFICATION → IMPACT ANALYSIS → READY
                                              ↑
                                        Added in v1.1
```

**New Tool `analyze_impact`:**

```
mcp__code-intel__analyze_impact
  target_files: ["app/Models/Product.php"]
  change_description: "Change price field type"
```

**Response:**

```json
{
  "impact_analysis": {
    "mode": "standard",
    "depth": "direct_only",
    "static_references": {
      "callers": [
        {"file": "app/Services/CartService.php", "line": 45}
      ]
    },
    "naming_convention_matches": {
      "tests": ["tests/Feature/ProductTest.php"],
      "factories": ["database/factories/ProductFactory.php"]
    },
    "document_mentions": {
      "files": [
        {
          "file": "docs/API-spec.md",
          "match_count": 5,
          "keywords": ["price"],
          "sample_lines": [
            {"line": 45, "content": "price field is decimal(10,2) type", "keyword": "price"}
          ]
        }
      ],
      "keywords_searched": ["price", "ProductPrice"]
    }
  },
  "confirmation_required": {
    "must_verify": ["app/Services/CartService.php"],
    "should_verify": ["tests/Feature/ProductTest.php", "docs/API-spec.md"]
  }
}
```

**LLM Response Obligation:**

```json
{
  "verified_files": [
    {"file": "...", "status": "will_modify | no_change_needed | not_affected", "reason": "..."}
  ]
}
```

### Markup Relaxation

When targeting only pure markup files (.html, .css, .md), impact analysis is relaxed.

| Extension | Relaxation |
|-----------|------------|
| `.html`, `.htm`, `.css`, `.scss`, `.md` | ✅ Relaxation applied |
| `.blade.php`, `.vue`, `.jsx`, `.tsx` | ❌ No relaxation (logic coupled) |

### Indirect Reference Handling

- Tool detects **direct references only** (1 level)
- Indirect references (2+ levels) are left to LLM judgment
- LLM can use `find_references` for additional investigation if needed

**Design Rationale:**
- Recursive full exploration creates too much noise
- After checking direct references, LLM can judge if additional investigation is needed

### Document Keyword Search (v1.1.1)

Automatically extracts keywords from `change_description` and `target_files`, then searches within documentation.

**Purpose:** Prevent documentation update omissions

**Search Targets (default):**
- `**/*.md` - Markdown files
- `**/README*` - README files
- `**/docs/**/*` - Files under docs directory

**Exclude Patterns (default):**
- `node_modules/**`, `vendor/**`, `.git/**`, `.venv/**`, `__pycache__/**`

**Configuration Customization:**

Customize search targets and exclude patterns via `document_search` section in `context.yml`:

```yaml
# .code-intel/context.yml
document_search:
  include_patterns:
    - "**/*.md"
    - "**/README*"
    - "**/docs/**/*"
  exclude_patterns:
    - "node_modules/**"
    - "vendor/**"
    - ".git/**"
    - "CHANGELOG*.md"      # Project-specific exclusion
    - "docs/archive/**"
```

**Keyword Extraction (by priority):**
1. **High**: Quoted strings (`"auto_billing"` - explicitly specified)
2. **Medium**: CamelCase/snake_case technical terms (`ProductPrice`, `user_account`)
3. **Low**: File names (often too generic, may cause noise)

**Limits:**
- Keywords: max 10
- Files: max 20
- Sample lines per file: max 3

**Output Format:** Aggregated by file (helps LLM decide "should I read this file?")

---

## v1.2 Additions

The following features were added in v1.2.

### OverlayFS-based Garbage Detection

Mount OverlayFS before implementation to capture all changes. In the PRE_COMMIT phase, LLM reviews and can discard unnecessary files (debug logs, commented code, etc.).

**Additional Phase:**

```
EXPLORATION → ... → READY → PRE_COMMIT → Finalize & Merge
                              ↑
                         Added in v1.2
```

**Required Tool:**
```bash
sudo apt-get install -y fuse-overlayfs
```

**Workflow:**

1. Specify `enable_overlay: true` in `start_session`
2. Create Git branch `llm_task_{session_id}`
3. Mount OverlayFS (changes captured in `.overlay/upper/`)
4. Implement in READY phase (work under `merged_path`)
5. `submit_for_review` → transition to PRE_COMMIT phase
6. `review_changes` to see diffs
7. `finalize_changes` to discard garbage, commit only needed files
8. `merge_to_main` to merge to main branch

**Examples of Garbage:**
- Debug `console.log` / `print()` statements
- Commented out code
- Test files not requested
- Unrelated modifications

**Concurrent Session Support:**

When multiple LLM sessions start simultaneously, an exclusive lock is held only during `checkout` + `mount`. After mounting, each session works independently.

```
Session A: [Lock] checkout → mount [Unlock] → work → finalize
Session B:        (wait)   [Lock] checkout → mount [Unlock] → work → ...
```

### /code --clean Flag

Flag to clean up remnants from interrupted sessions.

```bash
/code --clean
```

**Cleanup Targets:**
- Stale overlay mounts
- Directories under `.overlay/`
- `llm_task_*` branches
- Lock files
