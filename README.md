# Code Intelligence MCP Server

> **Current Version: v1.7**

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
| Garbage Isolation (v1.2) | Isolate changes with Git branch, bulk discard with --clean |
| Document Research (v1.3) | Sub-agent research of design docs for task-specific rules |
| Markup Cross-Reference (v1.3) | Lightweight CSS/HTML/JS cross-reference analysis |
| Intervention System (v1.4) | Retry-based intervention for stuck verification loops |
| Quality Review (v1.5) | Post-implementation quality check with retry loop |
| Branch Lifecycle (v1.6) | Stale branch warnings, auto-deletion on failure, begin_phase_gate separation |
| Parallel Execution (v1.7) | search_text multi-pattern support, Read/Grep parallel execution saves 27-35s |

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

## Processing Flow

Processing consists of 3 layers:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. Preparation (Skill controlled)                                          │
│     Flag Check → Failure Check → Intent → Session Start                    │
│     → DOCUMENT_RESEARCH → QueryFrame                                       │
│     ← skip DOCUMENT_RESEARCH with --no-doc-research                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  1.5. Phase Gate Start (v1.6)                                               │
│     begin_phase_gate → [Stale branches?] → [User intervention] → Continue  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  2. Phase Gates (Server enforced)                                           │
│     EXPLORATION → SEMANTIC* → VERIFICATION* → IMPACT_ANALYSIS → READY      │
│     → POST_IMPL_VERIFY → PRE_COMMIT → QUALITY_REVIEW                       │
│     ← --quick skips exploration, --no-verify/--no-quality skip each phase  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  3. Completion                                                              │
│     Finalize & Merge                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. Preparation (Skill controlled)

Controlled by skill prompt (code.md). Server not involved.

| Step | Description | Skip |
|------|-------------|------|
| Flag Check | Parse command options (`--quick`, etc.) | - |
| Failure Check | Auto-detect previous failure, record to OutcomeLog | - |
| Intent | Classify as IMPLEMENT / MODIFY / INVESTIGATE / QUESTION | - |
| Session Start | Start session, get project_rules (no branch yet) | - |
| **DOCUMENT_RESEARCH** | Sub-agent researches design docs, extracts mandatory_rules | `--no-doc-research` |
| QueryFrame | Decompose request into structured slots with Quote verification | - |

### 1.5. Phase Gate Start (v1.6)

After preparation, `begin_phase_gate` creates the task branch and starts phase gates.

**Stale Branch Detection:**
- If `llm_task_*` branches exist while not on a task branch, user intervention is required
- Three options: Delete, Merge, or Continue as-is

### 2. Phase Gates (Server enforced)

MCP server enforces phase transitions. LLM cannot skip arbitrarily.

#### Phase Matrix

| Option | Explore | Implement | Verify | Intervene | Garbage | Quality | Branch |
|--------|:-------:|:---------:|:------:|:---------:|:-------:|:-------:|:------:|
| (default) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--no-verify` | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--fast` / `-f` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

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
| `search_text` | Text search (ripgrep), multi-pattern parallel search (v1.7) |
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

### Garbage Detection & Quality Review (v1.2, v1.5)

| Tool | Purpose |
|------|---------|
| `submit_for_review` | Transition to PRE_COMMIT phase |
| `review_changes` | Show all file changes |
| `finalize_changes` | Keep/discard files and commit |
| `submit_quality_review` | Submit quality review result (v1.5) |
| `merge_to_base` | Merge task branch to base branch |
| `cleanup_stale_sessions` | Clean up interrupted sessions |

### Branch Lifecycle (v1.6)

| Tool | Purpose |
|------|---------|
| `begin_phase_gate` | Start phase gates, create branch (with stale check) |
| `cleanup_stale_sessions` | Delete stale branches |

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
    ├── context.yml       ← Project rules / doc research settings (auto-generated)
    ├── chroma/           ← ChromaDB data (auto-generated)
    ├── agreements/       ← Success pattern storage
    ├── logs/             ← DecisionLog, OutcomeLog
    ├── verifiers/        ← Verification prompts (backend.md, html_css.md, etc.)
    ├── doc_research/     ← Document research prompts (v1.3)
    ├── interventions/    ← Intervention prompts (v1.4)
    └── review_prompts/   ← Quality review prompts (v1.5)
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

Restart to load the MCP server.

### Step 6: Customize context.yml (optional)

The `.code-intel/context.yml` file controls various behaviors. You can customize it as needed:

```yaml
# Project rules (auto-detected: CLAUDE.md, .claude/CLAUDE.md, CONTRIBUTING.md)
project_rules:
  source: "CLAUDE.md"

# Document search settings for analyze_impact
document_search:
  include_patterns:
    - "**/*.md"
    - "**/README*"
    - "**/docs/**/*"
  exclude_patterns:
    - "node_modules/**"
    - "vendor/**"
    - ".git/**"

# v1.3: Document Research Configuration
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

# v1.4: Intervention Settings
interventions:
  enabled: true
  prompts_dir: "interventions/"
  threshold: 3  # Number of failures before intervention

# Verifier settings
verifiers:
  suggest_improvements: true
```

| Section | Description |
|---------|-------------|
| `project_rules` | Source file for project rules (auto-detected) |
| `document_search` | Patterns for impact analysis document search |
| `doc_research` | Document research settings (v1.3) |
| `interventions` | Intervention system settings (v1.4) |
| `verifiers` | Verifier behavior settings |

---

## Upgrade (for existing v1.2 or earlier users)

**Note:** If you performed a fresh setup with v1.3 or later, this section is not needed. `init-project.sh` creates all directories automatically.

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

| Item | v1.0 | v1.1 | v1.2 | v1.3 | v1.4 | v1.5 | v1.6 |
|------|------|------|------|------|------|------|------|
| Phases | 4 | 5 | 6 | 6 | 6 | 7 (QUALITY_REVIEW) | 7 |
| context.yml | None | Auto-generated | Auto-generated | doc_research added | Same | Same | Same |
| Design docs summary | None | Auto-provided | Same | Sub-agent research | Same | Same | Same |
| Garbage isolation | None | None | Git branch | Same | Same | Same | Same |
| Intervention | None | None | None | None | Retry-based | Same | Same |
| Quality review | None | None | None | None | None | Retry loop | Same |
| Branch lifecycle | None | None | None | None | None | None | Stale warning |
| verifiers/ | None | None | None | Verification prompts | Same | Same | Same |
| interventions/ | None | None | None | None | Intervention prompts | Same | Same |
| review_prompts/ | None | None | None | None | None | Quality prompts | Same |

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
| `--no-verify` | - | Skip verification (and intervention) |
| `--no-quality` | - | Skip quality review (v1.5) |
| `--only-verify` | `-v` | Run verification only (skip implementation) |
| `--fast` | `-f` | Fast mode: skip exploration with branch (garbage + verify) |
| `--quick` | `-q` | Minimal mode: skip exploration without branch |
| `--doc-research=PROMPTS` | - | Specify document research prompts (v1.3) |
| `--no-doc-research` | - | Skip document research (v1.3) |
| `--clean` | `-c` | Cleanup stale sessions |
| `--rebuild` | `-r` | Force full re-index |

**Default behavior:** full mode (explore + implement + verify + garbage + quality)

#### Examples

```bash
# Full mode (default): explore + implement + verify + garbage + quality
/code add login feature

# Skip verification (and intervention)
/code --no-verify fix this bug

# Skip quality review only
/code --no-quality fix simple typo

# Verification only (check existing implementation)
/code -v sample/hello.html

# Fast mode: skip exploration with branch (for known fixes)
/code -f fix known issue in login validation

# Quick mode: minimal mode (no branch, no garbage, no quality)
/code -q change the button color to blue

# Document research with specific prompts (v1.3)
/code --doc-research=security add authentication

# Skip document research (v1.3)
/code --no-doc-research fix typo

# Cleanup stale sessions
/code -c

# Force full re-index
/code -r
```

#### --clean Option (v1.2)

Clean up stale task branches from interrupted sessions:

```
/code -c
```

With `-c` / `--clean`:
- If currently on a `llm_task_*` branch, checks out to the base branch first
  - Base branch is extracted from branch name: `llm_task_{session}_from_{base}` → `{base}`
- Deletes all `llm_task_*` branches
- Use after session interruption (Ctrl+C, crash, etc.) to start fresh

#### Normal Execution Flow

The skill automatically:
1. Flag check
2. Failure check (auto-detect and record previous failures)
3. Intent determination
4. Session start (auto-sync, essential context)
5. DOCUMENT_RESEARCH (v1.3) ← skip with `--no-doc-research`
6. QueryFrame extraction and verification
7. EXPLORATION ← skip with `--quick`
8. Symbol verification (Embedding) ← skip with `--quick`
9. SEMANTIC if needed ← skip with `--quick`
10. VERIFICATION (hypothesis verification) ← skip with `--quick`
11. IMPACT ANALYSIS ← skip with `--quick`
12. READY (implementation)
13. POST_IMPLEMENTATION_VERIFICATION ← skip with `--no-verify`
14. INTERVENTION (v1.4) ← triggered on 3 consecutive verify failures
15. GARBAGE DETECTION ← skip with `--quick`
16. QUALITY REVIEW (v1.5) ← skip with `--no-quality` or `--quick`
17. Finalize & Merge

### Direct tool invocation

```
# Text search (single pattern)
Search for pattern "Router" with mcp__code-intel__search_text

# Text search (multi-pattern parallel, v1.7)
Search for patterns ["Router", "SessionState", "QueryFrame"] in parallel with mcp__code-intel__search_text

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
│   ├── branch_manager.py   ← Git branch isolation (v1.2)
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
| v1.6 | Branch Lifecycle (stale warning, begin_phase_gate) | [v1.6](docs/updates/v1.6.md) |
| v1.5 | Quality Review with retry loop | [v1.5](docs/updates/v1.5.md) |
| v1.4 | Intervention System | [v1.4](docs/updates/v1.4.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](docs/updates/v1.3.md) |
| v1.2 | Git Branch Isolation | [v1.2](docs/updates/v1.2.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](docs/updates/v1.1.md) |

---

## License

MIT
