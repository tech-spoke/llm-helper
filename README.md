# Code Intelligence MCP Server

> **Current Version: v1.3**

An MCP server that provides Cursor IDE-like code intelligence capabilities using open source tools.

## Overview

Even with the same Opus 4.5 model, behavior differs depending on the caller:

| Caller | Behavior |
|--------|----------|
| **Cursor** | Modifies code after understanding the entire codebase |
| **Claude Code** | Tends to modify only the specific location |

This MCP server provides mechanisms to make Claude Code "understand the codebase".

---

## Design Philosophy

```
Don't let the LLM decide. Design so it can't proceed without compliance.
And have a mechanism to learn from failures.
```

| Principle | Implementation |
|-----------|----------------|
| Phase Enforcement | Tool usage restrictions (semantic_search forbidden in EXPLORATION, etc.) |
| Server Evaluation | Confidence calculated by server, eliminating LLM self-reporting |
| Structured Input | Hallucination prevention via Quote verification |
| Embedding Verification | Objective evaluation of NL→Symbol relevance via vector similarity |
| Write Restriction | Only explored files allowed |
| Improvement Cycle | Learning via DecisionLog + OutcomeLog + agreements |
| Automatic Failure Detection | Auto-detect and record previous failures at /code start |
| Project Isolation | Independent learning data for each project |
| Essential Context (v1.1) | Auto-provide design docs and project rules at session start |
| Impact Analysis (v1.1) | Enforce impact verification before READY phase |
| Garbage Isolation (v1.2) | Isolate changes with OverlayFS + Git branch, bulk discard with --clean |
| Document Research (v1.3) | Sub-agent research of design docs for task-specific rules |
| Markup Cross-Reference (v1.3) | Lightweight CSS/HTML/JS cross-reference analysis |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Clients (Claude Code)                 │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
                    ┌─────────────────┐
                    │   code-intel    │  ← Unified MCP Server
                    │  (orchestrator) │
                    └─────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
        │ ChromaDB    │ │ ripgrep     │ │ ctags       │
        │ (map/forest)│ │ (search)    │ │ (symbols)   │
        └─────────────┘ └─────────────┘ └─────────────┘
               │
               ▼
    ┌───────────────────┐
    │ Project/.code-intel│  ← Project-specific
    │ ├─ config.json     │
    │ ├─ chroma/         │  ← ChromaDB data
    │ ├─ agreements/     │
    │ ├─ logs/           │  ← DecisionLog, OutcomeLog
    │ └─ sync_state.json │
    └───────────────────┘
```

### Forest and Map

| Name | Collection | Role | Data Nature |
|------|------------|------|-------------|
| **Forest** | forest | Semantic search of entire source code | Raw data / HYPOTHESIS |
| **Map** | map | Past success pairs / agreements | Confirmed data / FACT |

**Short-circuit Logic**: Map score ≥ 0.7 → Skip Forest exploration

---

## Phase Gates

### Complete Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Flag Check → Failure Check → Intent → Session Start → QueryFrame           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  EXPLORATION → Symbol Validation → SEMANTIC* → VERIFICATION* → IMPACT       │
│       ↓              ↓                ↓             ↓            ↓          │
│  code-intel     Embedding         semantic     code verify   analyze_impact │
│   tools         (NL→Symbol)        search      (hypo→fact)   (impact)       │
│                                   (hypothesis)                              │
│                                                                             │
│  * SEMANTIC/VERIFICATION only when confidence=low                           │
│  ← Skip this entire block with --quick / -g=n                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  READY (impl) → POST_IMPLEMENTATION_VERIFICATION → PRE_COMMIT → Merge       │
│       ↓                    ↓                           ↓           ↓        │
│  Edit/Write           Run verifier prompt          Review changes  Merge    │
│  (explored files only)  (Playwright/pytest)        Discard garbage to main  │
│                                                                             │
│  ← Skip VERIFICATION with --no-verify                                       │
│  ← Loop back to READY on verification failure                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Tool Permissions by Phase

| Phase | Allowed | Forbidden |
|-------|---------|-----------|
| EXPLORATION | code-intel tools (query, find_definitions, find_references, search_text) | semantic_search |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel tools | semantic_search |
| IMPACT_ANALYSIS | analyze_impact, code-intel | semantic_search |
| READY | Edit, Write (explored files only) | - |
| POST_IMPL_VERIFY | Verification tools (Playwright, pytest, etc.) | - |
| PRE_COMMIT | review_changes, finalize_changes | - |

---

## Tool List

### Code Intelligence

| Tool | Purpose |
|------|---------|
| `query` | Intelligent query in natural language |
| `find_definitions` | Symbol definition search (ctags) |
| `find_references` | Symbol reference search (ripgrep) |
| `search_text` | Text search (ripgrep) |
| `search_files` | File pattern search (glob) |
| `analyze_structure` | Code structure analysis (tree-sitter) |
| `get_symbols` | Get symbol list |
| `get_function_at_line` | Get function at specific line |
| `sync_index` | Index source code to ChromaDB |
| `semantic_search` | Unified vector search of map/forest |
| `analyze_impact` | Analyze impact of changes (v1.1) |

### Session Management

| Tool | Purpose |
|------|---------|
| `start_session` | Start session |
| `set_query_frame` | Set QueryFrame (Quote verification) |
| `get_session_status` | Check current phase/status |
| `submit_understanding` | Complete EXPLORATION |
| `validate_symbol_relevance` | Embedding verification |
| `confirm_symbol_relevance` | Confirm symbol validation results |
| `submit_semantic` | Complete SEMANTIC |
| `submit_verification` | Complete VERIFICATION |
| `submit_impact_analysis` | Complete IMPACT_ANALYSIS (v1.1) |
| `check_write_target` | Check write permission |
| `add_explored_files` | Add explored files |
| `revert_to_exploration` | Return to EXPLORATION |
| `update_context` | Update context summaries (v1.1) |

### Garbage Detection (v1.2)

| Tool | Purpose |
|------|---------|
| `submit_for_review` | Transition to PRE_COMMIT phase |
| `review_changes` | Show all file changes |
| `finalize_changes` | Keep/discard files and commit |
| `merge_to_main` | Merge task branch to main |
| `cleanup_stale_overlays` | Clean up interrupted sessions |

### Improvement Cycle

| Tool | Purpose |
|------|---------|
| `record_outcome` | Record outcome (auto/manual) |
| `get_outcome_stats` | Get statistics |

---

## Setup

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
    ├── context.yml       ← Essential context (v1.1, auto-generated)
    ├── chroma/           ← ChromaDB data (auto-generated)
    ├── agreements/       ← Agreements directory
    ├── logs/             ← DecisionLog, OutcomeLog
    ├── verifiers/        ← Verification prompts
    └── doc_research/     ← Document research prompts (v1.3)
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

You can edit `.code-intel/context.yml` to add `extra_notes` (implicit knowledge not in source docs):

```yaml
essential_docs:
  source: "docs/architecture"
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: "..."                     # ← Auto-generated by LLM
      extra_notes: |                     # ← Optional: add your implicit knowledge
        - Exception: Simple CRUD can bypass Service layer

project_rules:
  source: "CLAUDE.md"
  summary: "..."                         # ← Auto-generated by LLM
  extra_notes: |                         # ← Optional: add your implicit knowledge
    - Legacy code in /old can ignore these rules
```

| Field | Description |
|-------|-------------|
| `source` | Auto-detected source path |
| `summary` | Auto-generated by LLM |
| `extra_notes` | Manual addition (preserved on regeneration) |
| `content_hash` | Auto-generated for change detection |

---

## Upgrade (v1.0 → v1.1 → v1.2 → v1.3)

Steps to upgrade existing projects:

### Step 1: Update llm-helper Server

```bash
cd /path/to/llm-helper
git pull
./setup.sh  # Update dependencies
```

### Step 2: Update Skills (if copied to project)

```bash
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 3: Add New Directories (v1.3)

Create new directories and copy templates:

```bash
# Create new directories
mkdir -p /path/to/your-project/.code-intel/logs
mkdir -p /path/to/your-project/.code-intel/verifiers
mkdir -p /path/to/your-project/.code-intel/doc_research

# Copy verifier templates
cp /path/to/llm-helper/.code-intel/verifiers/*.md /path/to/your-project/.code-intel/verifiers/

# Copy doc_research prompts
cp /path/to/llm-helper/.code-intel/doc_research/*.md /path/to/your-project/.code-intel/doc_research/
```

### Step 4: Update context.yml (v1.3)

Add `doc_research` section to your `.code-intel/context.yml`:

```yaml
# Document research settings (v1.3)
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"
```

### Step 5: Restart Claude Code

Restart to reload the MCP server.

### What Changes

| Item | v1.0 | v1.1 | v1.2 | v1.3 |
|------|------|------|------|------|
| Phases | 4 | 5 (IMPACT_ANALYSIS added) | 6 (PRE_COMMIT added) | 6 (DOCUMENT_RESEARCH step added) |
| context.yml | None | Auto-generated | Auto-generated | doc_research added |
| Design docs summary | None | Auto-provided at session start | Same | Sub-agent research |
| Project rules | Manual CLAUDE.md reference | Auto-provided at session start | Same | Two-layer context |
| Garbage isolation | None | None | OverlayFS + Git branch | Same |
| Markup analysis | None | Relaxation only | Same | Cross-reference detection |
| verifiers/ | None | None | None | Verification prompts |
| doc_research/ | None | None | None | Research prompts |

### No Changes Required

- `.code-intel/config.json` - Compatible, no changes needed
- `.code-intel/chroma/` - Existing index continues to work
- `.mcp.json` - No changes needed

The `context.yml` file will be automatically created on next session start.

---

## Usage

### Using /code skill (recommended)

```
/code Fix the bug in AuthService's login function where no error is shown when password is empty
```

### Command Options

| Long | Short | Description |
|------|-------|-------------|
| `--no-verify` | - | Skip verification |
| `--only-verify` | `-v` | Run verification only (skip implementation) |
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: h(igh), m(iddle), l(ow), a(uto), n(one) |
| `--quick` | `-q` | Skip exploration phases (= `-g=n`) |
| `--doc-research=PROMPTS` | - | Specify document research prompts (v1.3) |
| `--no-doc-research` | - | Skip document research (v1.3) |
| `--clean` | `-c` | Cleanup stale overlays |
| `--rebuild` | `-r` | Force full re-index |

**Default behavior:** gate=high + implementation + verification (full mode)

#### Examples

```bash
# Full mode (default): gate=high + doc-research + impl + verify
/code add login feature

# Skip verification
/code --no-verify fix this bug

# Verification only (check existing implementation)
/code -v sample/hello.html

# Quick mode (skip exploration, impl + verify only)
/code -q change the button color to blue

# Set gate level explicitly
/code -g=m add password validation

# Document research with specific prompts (v1.3)
/code --doc-research=security add authentication

# Skip document research (v1.3)
/code --no-doc-research fix typo

# Cleanup stale overlays
/code -c

# Force full re-index
/code -r
```

#### --clean Option (v1.2)

To discard files created in the previous session and start over:

```
/code -c
```

With `-c` / `--clean`:
- Discards changes in current OverlayFS session
- Deletes Git branches (`llm_task_*`)
- Starts a new session from clean state

**Note**: OverlayFS features are disabled if `fuse-overlayfs` is not installed.

#### Normal Execution Flow

The skill automatically:
1. Flag check
2. Failure check (auto-detect and record previous failures)
3. Intent determination
4. Session start (auto-sync, essential context)
5. DOCUMENT_RESEARCH (v1.3 - sub-agent doc research) ← skip with `--no-doc-research`
6. QueryFrame extraction and verification
7. EXPLORATION (find_definitions, find_references, etc.) ← skip with `--quick` / `-g=n`
8. Symbol verification (Embedding) ← skip with `--quick` / `-g=n`
9. SEMANTIC if needed ← skip with `--quick` / `-g=n`
10. VERIFICATION (hypothesis verification) ← skip with `--quick` / `-g=n`
11. IMPACT ANALYSIS (v1.1 - analyze affected files) ← skip with `--quick` / `-g=n`
12. READY (implementation)
13. POST_IMPLEMENTATION_VERIFICATION ← skip with `--no-verify`

### Direct tool invocation

```
# Text search
Search for pattern "Router" with mcp__code-intel__search_text

# Definition search
Find definition of "SessionState" with mcp__code-intel__find_definitions

# Semantic search
Search for query "login functionality" with mcp__code-intel__semantic_search
```

---

## Improvement Cycle

### Two Logs

| Log | File | Trigger |
|-----|------|---------|
| DecisionLog | `.code-intel/logs/decisions.jsonl` | On query execution (auto) |
| OutcomeLog | `.code-intel/logs/outcomes.jsonl` | On failure detection (auto) or manual |

### Automatic Failure Detection

At `/code` start, automatically determines if current request indicates "previous failure":
- Detects patterns like "redo", "doesn't work", "wrong", etc.
- Automatically records failure to OutcomeLog
- No need to manually call `/outcome`

---

## Dependencies

### System Tools

| Tool | Required | Purpose |
|------|----------|---------|
| ripgrep (rg) | Yes | search_text, find_references |
| universal-ctags | Yes | find_definitions, get_symbols |
| Python 3.10+ | Yes | Server |
| fuse-overlayfs | No | Garbage isolation (v1.2, Linux only) |

### Python Packages

```
mcp>=1.0.0
chromadb>=1.0.0
tree-sitter>=0.21.0
tree-sitter-languages>=1.10.0
sentence-transformers>=2.2.0
scikit-learn>=1.0.0
PyYAML>=6.0.0
pytest>=7.0.0
```

---

## Project Structure

### MCP Server (llm-helper/)

```
llm-helper/
├── code_intel_server.py    ← MCP server main
├── tools/                  ← Tool implementations
│   ├── session.py          ← Session management
│   ├── query_frame.py      ← QueryFrame
│   ├── router.py           ← Query routing
│   ├── chromadb_manager.py ← ChromaDB management
│   ├── ast_chunker.py      ← AST chunking
│   ├── sync_state.py       ← Sync state management
│   ├── outcome_log.py      ← Improvement cycle log
│   ├── context_provider.py ← Essential context (v1.1)
│   ├── impact_analyzer.py  ← Impact analysis (v1.1)
│   ├── overlay_manager.py  ← OverlayFS garbage isolation (v1.2)
│   └── ...
├── setup.sh                ← Server setup
├── init-project.sh         ← Project initialization
└── .claude/commands/       ← Skill definitions
    └── code.md
```

### Target Project

```
your-project/
├── .mcp.json               ← MCP config (manual setup)
├── .code-intel/            ← Code Intel data (auto-generated)
│   ├── config.json
│   ├── context.yml         ← Essential context config (v1.1)
│   ├── chroma/             ← ChromaDB data
│   ├── agreements/         ← Success pairs
│   ├── logs/               ← DecisionLog, OutcomeLog
│   ├── verifiers/          ← Verification prompts
│   ├── doc_research/       ← Document research prompts (v1.3)
│   └── sync_state.json
├── .claude/commands/       ← Skills (optional copy)
└── src/                    ← Your source code
```

---

## Documentation

| Document | Content |
|----------|---------|
| [DESIGN.md](docs/DESIGN.md) | Overall design (English) |
| [DESIGN_ja.md](docs/DESIGN_ja.md) | Overall design (Japanese) |
| [DOCUMENTATION_RULES.md](docs/DOCUMENTATION_RULES.md) | Documentation management rules |

---

## CHANGELOG

For version history and detailed changes, see:

| Version | Description | Link |
|---------|-------------|------|
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](docs/updates/v1.3.md) |
| v1.2 | OverlayFS, Gate Levels | [v1.2](docs/updates/v1.2.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](docs/updates/v1.1.md) |

---

## License

MIT
