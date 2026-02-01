# Code Intelligence MCP Server

> **Current Version: v1.11**

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
| Unified submit_phase (v1.11) | 17 submit_* tools → single tool, foundation for compaction resilience |
| Task Orchestration (v1.11) | Server-enforced task management, LLM cannot skip |
| Compaction Resilience (v1.11) | 4-layer resilience design (tool, response, recovery, defense) |
| Safety Valves (v1.11) | Server-side counters prevent infinite loops |

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

## Processing Flow (19 Steps)

In v1.11, all phase exits are unified into `submit_phase`. After LLM-side preprocessing (Flag Check, Intent Classification), all steps complete via `start_session` → `submit_phase` (×N) → SESSION_COMPLETE.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Session Start                                                              │
│  Step 1: start_session (Intent determination → initialization)             │
│  Step 2: BRANCH_INTERVENTION (only when stale detected)                    │
│  Step 3: DOCUMENT_RESEARCH ← skip with --no-doc                           │
│  Step 4: QUERY_FRAME                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  Exploration Phase ← skip with --fast/--quick                              │
│  Step 5:  EXPLORATION                                                      │
│  Step 6:  Q1 (Assess SEMANTIC necessity)                                   │
│  Step 7:  SEMANTIC (only when Q1=true)                                     │
│  Step 8:  Q2 (Assess VERIFICATION necessity)                               │
│  Step 9:  VERIFICATION (only when Q2=true)                                 │
│  Step 10: Q3 (Assess IMPACT_ANALYSIS necessity)                            │
│  Step 11: IMPACT_ANALYSIS (only when Q3=true / Investigate: SESSION_COMPLETE)│
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  Implementation Phase                                                       │
│  Step 12: READY (task planning + branch creation)                          │
│  Step 13: READY (implementation ×N)                                        │
│  Step 14: READY (all-tasks-complete check)                                 │
│  Step 15: POST_IMPL_VERIFY ← skip with --no-verify / fail→Step 12        │
│  Step 16: VERIFY_INTERVENTION (only when failure_count ≥ 3)                │
│  Step 17: PRE_COMMIT                                                       │
│  Step 18: QUALITY_REVIEW ← skip with --no-quality                         │
│  Step 19: MERGE → SESSION_COMPLETE                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Session Start (Steps 1-4)

| Step | Phase | Description | Skip |
|------|-------|-------------|------|
| 1 | — | Determine intent → initialize with `start_session` | - |
| 2 | BRANCH_INTERVENTION | Present choices to user when stale branches detected | Stale only |
| 3 | DOCUMENT_RESEARCH | Research design docs via sub-agent | `--no-doc` |
| 4 | QUERY_FRAME | NL → structured slot extraction | - |

### Exploration Phase (Steps 5-11)

| Step | Phase | Description | Skip |
|------|-------|-------------|------|
| 5 | EXPLORATION | Explore with code-intel tools | `--fast`/`--quick` |
| 6 | Q1 | Assess SEMANTIC necessity | Server may skip Step 7 |
| 7 | SEMANTIC | Gather additional info via semantic_search | Skipped when Q1=false |
| 8 | Q2 | Assess VERIFICATION necessity | Server may skip Step 9 |
| 9 | VERIFICATION | Verify hypotheses | Skipped when Q2=false |
| 10 | Q3 | Assess IMPACT_ANALYSIS necessity | Server may skip Step 11 |
| 11 | IMPACT_ANALYSIS | Impact analysis | Skipped when Q3=false / Investigate: SESSION_COMPLETE |

### Implementation Phase (Steps 12-19)

| Step | Phase | Description | Notes |
|------|-------|-------------|-------|
| 12 | READY | Task decomposition & planning | Branch created |
| 13 | READY | Implement via Edit/Write | Repeats per task (×N) |
| 14 | READY | Confirm all tasks complete | Blocks if incomplete |
| 15 | POST_IMPL_VERIFY | Execute verifier prompts | On failure: revert to Step 12 |
| 16 | VERIFY_INTERVENTION | Read & execute intervention prompt | Only when failure_count ≥ 3 |
| 17 | PRE_COMMIT | Review via review_changes | |
| 18 | QUALITY_REVIEW | Quality check | quality_revert_count ≥ 3 → forced completion |
| 19 | MERGE | Merge → SESSION_COMPLETE | |

### Phase Matrix

The server determines the next phase in each `submit_phase` response. The LLM does not need to know about flags.

| Option | Doc Research | Explore | Implement | Verify | Intervene | Garbage | Quality | Branch |
|--------|:-------:|:-------:|:---------:|:------:|:---------:|:-------:|:-------:|:------:|
| (default) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Investigate intent | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `--no-verify` | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--fast` / `-f` | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `--no-doc` | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Legend**:
- **Doc Research**: DOCUMENT_RESEARCH (Step 3)
- **Explore**: EXPLORATION through IMPACT_ANALYSIS (Steps 5-11)
- **Investigate intent**: INVESTIGATE / QUESTION (equivalent to `--only-explore`)

---

## Tool List

### Session Management

| Tool | Purpose |
|------|---------|
| `start_session` | Start session with intent, query, and flags |
| `submit_phase` | **Single exit point for all phases** — server determines next phase (v1.11) |
| `get_session_status` | Get current phase + instruction + expected_payload (for recovery) |

### Code Exploration

| Tool | Purpose |
|------|---------|
| `search_text` | Text pattern search (parallel-capable) |
| `find_definitions` | Symbol definition search (ctags) |
| `find_references` | Reference search (ripgrep) |
| `analyze_structure` | Code structure analysis (tree-sitter) |
| `get_symbols` | Get symbol list for a file |
| `search_files` | File name pattern search |
| `semantic_search` | Vector search in Forest/Map (SEMANTIC phase) |
| `query` | General natural language query |

### Implementation Control

| Tool | Purpose |
|------|---------|
| `check_write_target` | Verify file can be modified |
| `add_explored_files` | Add files to explored list |
| `revert_to_exploration` | Return to EXPLORATION phase |
| `review_changes` | Show all file changes in PRE_COMMIT |

### Branch Management

| Tool | Purpose |
|------|---------|
| `cleanup_stale_branches` | Delete all `llm_task_*` branches |

### Index & Learning

| Tool | Purpose |
|------|---------|
| `sync_index` | Sync ChromaDB index |
| `update_context` | Update context.yml summaries |
| `record_outcome` | Record success/failure |
| `get_outcome_stats` | Get learning statistics |

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
    ├── task_planning.md  ← Task decomposition guide (v1.11)
    ├── chroma/           ← ChromaDB data (auto-generated)
    ├── agreements/       ← Success pattern storage
    ├── logs/             ← DecisionLog, OutcomeLog
    ├── verifiers/        ← Verification prompts (backend.md, html_css.md, etc.)
    ├── doc_research/     ← Document research prompts
    ├── interventions/    ← Intervention prompts
    └── review_prompts/   ← Quality review prompts
```

**Important files created:**
- `.claude/CLAUDE.md` - Project-specific rules that LLM must follow
- `.claude/PARALLEL_GUIDE.md` - Parallel execution efficiency guide (v1.7)
- `.claude/commands/` - Skill definitions (`/code`, `/exp`, etc.)
  - Copied from `llm-helper/templates/skills/claude/`

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

### Codex Users (Install Skills)

Codex loads skills from `$CODEX_HOME/skills`. Copy the templates:
```bash
cp -r /path/to/llm-helper/templates/skills/codex/* "$CODEX_HOME/skills/"
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
cp /path/to/llm-helper/templates/skills/claude/*.md /path/to/your-project/.claude/commands/
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
cp -n /path/to/llm-helper/templates/code-intel/verifiers/*.md .code-intel/verifiers/
cp -n /path/to/llm-helper/templates/code-intel/doc_research/*.md .code-intel/doc_research/
cp -n /path/to/llm-helper/templates/code-intel/interventions/*.md .code-intel/interventions/
cp -n /path/to/llm-helper/templates/code-intel/review_prompts/*.md .code-intel/review_prompts/
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

#### Normal Execution Flow (19 Steps)

In v1.11, all phase exits are unified into `submit_phase`. The skill automatically:

1. **start_session**: Determine intent → initialize session
2. **BRANCH_INTERVENTION**: User intervention when stale branches detected
3. **DOCUMENT_RESEARCH**: Research design docs ← skip with `--no-doc`
4. **QUERY_FRAME**: NL → structured slot extraction
5. **EXPLORATION**: Explore with code-intel tools ← skip with `--fast`/`--quick`
6. **Q1**: Assess SEMANTIC necessity ← ignored with `--gate=full`
7. **SEMANTIC**: Gather additional info (only when Q1=true)
8. **Q2**: Assess VERIFICATION necessity ← ignored with `--gate=full`
9. **VERIFICATION**: Verify hypotheses (only when Q2=true)
10. **Q3**: Assess IMPACT_ANALYSIS necessity ← ignored with `--gate=full`
11. **IMPACT_ANALYSIS**: Impact analysis (only when Q3=true)
12. **READY (planning)**: Task decomposition + branch creation
13. **READY (implementation)**: Implement via Edit/Write (×N)
14. **READY (completion)**: All-tasks-complete check
15. **POST_IMPL_VERIFY**: Verification ← skip with `--no-verify` / fail→Step 12
16. **VERIFY_INTERVENTION**: Intervention (only when failure_count ≥ 3)
17. **PRE_COMMIT**: Review
18. **QUALITY_REVIEW**: Quality check ← skip with `--no-quality` / `--fast` / `--quick`
19. **MERGE**: Merge → SESSION_COMPLETE

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
└── templates/
    ├── code-intel/         ← Project templates (.code-intel)
    └── skills/
        ├── claude/         ← Claude skill templates (commands)
        └── codex/          ← Codex skill templates
```

### Target Project

```
your-project/
├── .mcp.json               ← MCP config (manual setup)
├── .code-intel/            ← Code Intel data (auto-generated)
│   ├── config.json
│   ├── context.yml         ← Context config
│   ├── task_planning.md    ← Task decomposition guide (v1.11)
│   ├── chroma/             ← ChromaDB data
│   ├── agreements/         ← Success pairs
│   ├── logs/               ← DecisionLog, OutcomeLog
│   ├── verifiers/          ← Verification prompts
│   ├── doc_research/       ← Document research prompts
│   ├── interventions/      ← Intervention prompts
│   ├── review_prompts/     ← Quality review prompts
│   └── sync_state.json
├── .claude/commands/       ← Skills (auto-generated)
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
| v1.11 | Unified submit_phase & task orchestration (17 tools → 1, compaction resilience, server-enforced task management) | [v1.11](docs/updates/v1.11_ja.md) |
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
