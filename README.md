# Code Intelligence MCP Server

> **Current Version: v1.10**

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
| Exploration-Only Mode (v1.8) | Intent-based auto-detection (INVESTIGATE/QUESTION) + --only-explore flag, no branch creation |
| Individual Phase Checks (v1.10) | Individual necessity checks before each phase, VERIFICATION/IMPACT separation, gate_level redesign saves 20-60s |

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
│  2. Phase Gates (Server enforced) v1.10: Individual Check Approach         │
│     EXPLORATION → Q1 Check → SEMANTIC* → Q2 Check → VERIFICATION*          │
│     → Q3 Check → IMPACT_ANALYSIS*                                          │
│     → [--only-explore: end here] or [READY → POST_IMPL_VERIFY → PRE_COMMIT]│
│     → QUALITY_REVIEW                                                       │
│     ← --quick skips exploration, --no-verify/--no-quality skip each phase  │
│     ← --gate=full ignores all checks, --gate=auto checks each (default)   │
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

### 1.5. Phase Gate Start (v1.6, v1.11)

After preparation, `begin_phase_gate` starts phase gates (branch creation deferred to READY transition).

**Stale Branch Detection:**
- If `llm_task_*` branches exist while not on a task branch, user intervention is required
- Three options: Delete, Merge, or Continue as-is

**Branch Creation (v1.11):**
- Branch is created when transitioning to READY phase (not at begin_phase_gate)
- Allows exploration to complete before committing to implementation

### 2. Phase Gates (Server enforced)

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

### Tool Permissions by Phase (v1.10: Individual Check Approach)

| Phase | Allowed | Forbidden |
|-------|---------|-----------|
| EXPLORATION | code-intel tools (query, find_definitions, find_references, search_text) | semantic_search |
| Q1/Q2/Q3 Checks | check_phase_necessity | - |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel tools | semantic_search |
| IMPACT_ANALYSIS | analyze_impact, code-intel | semantic_search |
| READY | Edit, Write, Bash (explored files only) | - |
| POST_IMPL_VERIFY | Verification tools (Playwright, pytest, etc.) | - |
| PRE_COMMIT | review_changes, finalize_changes | - |
| QUALITY_REVIEW | submit_quality_review (Edit/Write forbidden) | - |

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
| `check_phase_necessity` | Check phase necessity before each phase (Q1/Q2/Q3) (v1.10) |
| `validate_symbol_relevance` | Embedding verification |
| `confirm_symbol_relevance` | Confirm symbol validation results |
| `submit_semantic` | Complete SEMANTIC |
| `submit_verification` | Complete VERIFICATION |
| `submit_exploration` | Complete EXPLORATION |
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

### Branch Lifecycle (v1.6, v1.11)

| Tool | Purpose |
|------|---------|
| `begin_phase_gate` | Start phase gates (stale check). v1.11: branch creation deferred to READY |
| `cleanup_stale_branches` | Checkout to base branch, delete all `llm_task_*` branches |

### Intervention System (v1.4)

| Tool | Purpose |
|------|---------|
| `record_verification_failure` | Record verification failure |
| `get_intervention_status` | Determine if intervention is needed |
| `record_intervention_used` | Record which intervention prompt was used |

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
├── .claude/
│   ├── CLAUDE.md         ← Project rules for LLM (auto-generated)
│   ├── PARALLEL_GUIDE.md ← Efficiency guide (v1.7)
│   └── commands/         ← Skill files: /code, /exp, etc. (auto-generated)
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

**Important files created:**
- `.claude/CLAUDE.md` - Project-specific rules that LLM must follow
- `.claude/PARALLEL_GUIDE.md` - Parallel execution efficiency guide (v1.7)
- `.claude/commands/` - Skill definitions (`/code`, `/exp`, etc.)

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

### Step 4: Understanding Project Rules (Important)

The `init-project.sh` automatically creates `.claude/CLAUDE.md` with essential rules:

```markdown
# your-project

## Core Rules

1. **Always use parallel execution** when making multiple tool calls
2. **Use `/exp`** for quick tasks and parallel execution (exploration + implementation)

See [PARALLEL_GUIDE.md](PARALLEL_GUIDE.md) for details.
```

**Key Points:**
- **Parallel execution** (v1.7): When calling the same tool multiple times (Read, Grep, search_text), call them in a **single message** to save 27-35 seconds
- **`/exp` command**: Fast parallel execution tool for quick fixes, exploration, and lightweight implementation - no Phase Gates, direct execution

Example:
```
✅ CORRECT (parallel):
<Read file_path="file1.py" />
<Read file_path="file2.py" />
<Read file_path="file3.py" />

❌ WRONG (sequential):
<Read file_path="file1.py" />
[wait]
<Read file_path="file2.py" />
```

### Step 5: Restart Claude Code

Restart to load the MCP server.

### Step 6: Verify Skills

Check that skills are available:
```bash
# In Claude Code
/code --help
/exp Find all authentication code
```

### Step 7: Customize context.yml (optional)

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

### Step 3: Add Missing Directories

If directories added in v1.3+ are missing, create them and copy templates:

```bash
cd /path/to/your-project

# Create missing directories (skipped if already exist)
mkdir -p .code-intel/logs
mkdir -p .code-intel/verifiers
mkdir -p .code-intel/doc_research
mkdir -p .code-intel/interventions
mkdir -p .code-intel/review_prompts

# Copy templates (existing files will not be overwritten)
cp -n /path/to/llm-helper/.code-intel/verifiers/*.md .code-intel/verifiers/
cp -n /path/to/llm-helper/.code-intel/doc_research/*.md .code-intel/doc_research/
cp -n /path/to/llm-helper/.code-intel/interventions/*.md .code-intel/interventions/
cp -n /path/to/llm-helper/.code-intel/review_prompts/*.md .code-intel/review_prompts/
```

### Step 4: Restart Claude Code

Restart to reload the MCP server.

### No Changes Required

- `.code-intel/config.json` - Compatible, no changes needed
- `.code-intel/context.yml` - Auto-updated
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
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: f(ull), a(uto) [default: auto] (v1.10) |
| `--no-verify` | - | Skip verification (and intervention) |
| `--no-quality` | - | Skip quality review (v1.5) |
| `--only-verify` | `-v` | Run verification only (skip implementation) |
| `--only-explore` | `-e` | Run exploration only (skip implementation) (v1.8) |
| `--fast` | `-f` | Fast mode: skip exploration, with branch |
| `--quick` | `-q` | Minimal mode: skip exploration, no branch |
| `--doc-research=PROMPTS` | - | Specify research prompts (v1.3) |
| `--no-doc-research` | - | Skip document research (v1.3) |
| `--no-intervention` | `-ni` | Skip intervention system (v1.4) |
| `--clean` | `-c` | Checkout to base branch, delete stale `llm_task_*` branches |
| `--rebuild` | `-r` | Force full re-index |

**gate_level options (v1.10):**
- `--gate=full` or `-g=f`: Ignore all checks and execute all phases
- `--gate=auto` or `-g=a`: Check before each phase (default)

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

# Exploration only (skip implementation, for investigation)
/code -e Investigate issues in the codebase

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
8. Q1 Check (v1.10 - SEMANTIC necessity) ← ignored with `--gate=full`
9. SEMANTIC (only if Q1=YES) ← skip with `--quick`
10. Q2 Check (v1.10 - VERIFICATION necessity) ← ignored with `--gate=full`
11. VERIFICATION (only if Q2=YES) ← skip with `--quick`
12. Q3 Check (v1.10 - IMPACT_ANALYSIS necessity) ← ignored with `--gate=full`
13. IMPACT ANALYSIS (only if Q3=YES) ← skip with `--quick`
14. READY (implementation)
15. POST_IMPLEMENTATION_VERIFICATION ← skip with `--no-verify`
16. INTERVENTION (v1.4) ← triggered on 3 consecutive verify failures
17. GARBAGE DETECTION ← skip with `--quick`
18. QUALITY REVIEW (v1.5) ← skip with `--no-quality` / `--fast` / `--quick`
19. Finalize & Merge

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
│   ├── interventions/      ← Intervention prompts (v1.4)
│   ├── review_prompts/     ← Quality review prompts (v1.5)
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
| v1.10 | Individual Phase Checks (individual necessity checks before each phase, VERIFICATION/IMPACT separation, gate_level redesign - saves 20-60s) | [v1.10](docs/updates/v1.10_ja.md) |
| v1.9 | Performance Optimization (sync_index batch, VERIFICATION+IMPACT integration - saves 15-20s) | [v1.9](docs/updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode (Intent-based + --only-explore, no branch creation) | [v1.8](docs/updates/v1.8_ja.md) |
| v1.7 | Parallel Execution (search_text, Read, Grep - saves 27-35s) | [v1.7](docs/updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle (stale warning, begin_phase_gate) | [v1.6](docs/updates/v1.6_ja.md) |
| v1.5 | Quality Review (revert-to-READY loop) | [v1.5](docs/updates/v1.5_ja.md) |
| v1.4 | Intervention System | [v1.4](docs/updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](docs/updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](docs/updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](docs/updates/v1.1_ja.md) |

---

## License

MIT
