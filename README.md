# Code Intelligence MCP Server

> **Current Version: v1.16**

An MCP server that enforces LLMs like Claude and Codex to implement code exactly as specified.

## Overview

### Problems Solved

LLM-driven code implementation has three key problems:

| Problem | Symptom |
|---------|---------|
| **Insufficient Exploration** | Only looks at the modification target, ignores related code |
| **Rule Ignorance** | Doesn't read design documents or coding standards |
| **Incomplete Implementation** | Skips tasks, uses mocks, returns empty implementations |

### Our Solution

```
Don't let LLMs make decisions. Design gates they cannot bypass.
```

| Problem | Solution | Implementation |
|---------|----------|----------------|
| Insufficient Exploration | **Forced Exploration** | code-intel tool usage required in EXPLORATION phase |
| Rule Ignorance | **Forced Documentation** | Design document reading required in DOCUMENT_RESEARCH phase |
| Incomplete Implementation | **Forced Verification** | checklist + evidence required, empty implementation detection (pass/TODO/NotImplementedError) |

### Positioning

```
┌─────────────────────────────────────────────────────────────┐
│  Upper Layer: Spec-Driven Development Workflow               │
│  Define "what to build" (spec creation, task decomposition)  │
└─────────────────────────────────────────────────────────────┘
                         ↓ Pass specs/tasks
┌─────────────────────────────────────────────────────────────┐
│  This Tool: Code Intelligence MCP Server                     │
│  Enforce "build exactly as specified"                        │
│  - Force 19-step execution: explore → verify rules → impl    │
│  - Server controls phase transitions (LLM cannot skip)       │
└─────────────────────────────────────────────────────────────┘
```

This tool functions as a **lower layer** of the spec-driven development workflow, solving the problem of "specs are correct but LLM doesn't implement correctly."

---

## Design Philosophy

| Principle | Description |
|-----------|-------------|
| **Phase Enforcement** | Cannot proceed to implementation without exploration. Server controls transitions |
| **Server Evaluation** | Confidence scores calculated by server. LLM self-reporting eliminated |
| **Write Restriction** | Only explored files can be modified. Writes to unexplored files are blocked |
| **Safety Valve** | Server-side counter prevents infinite loops. Intervention triggers after 3 verification failures |
| **Improvement Cycle** | DecisionLog + OutcomeLog for learning from failure patterns |

See [DESIGN.md](docs/DESIGN.md) for details.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Clients (Claude Code)                 │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
                    ┌─────────────────┐
                    │   code-intel    │  ← Unified MCP server
                    │ (orchestrator)  │
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

### Semantic Search (Forest/Map)

**Purpose**: Find code from natural language queries (e.g., "authentication logic" → `AuthService.login()`)

| Layer | Search Target | Effect |
|-------|--------------|--------|
| **Map** | Term→Symbol mappings | Instantly identify symbols for learned terms |
| **Forest** | Entire source code | Find relevant code even for new terms |

**Behavior**: High Map score (≥0.7) → Skip Forest for faster results

---

## Processing Flow (19 Steps)

Each phase is gate-managed, with the orchestrator controlling transitions so LLMs cannot skip phases.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Session Start                                                              │
│  Step 1: start_session (Intent detection → Initialization)                  │
│  Step 2: BRANCH_INTERVENTION (only when stale detected)                     │
│  Step 3: DOCUMENT_RESEARCH ← Skip with --no-doc                            │
│  Step 4: QUERY_FRAME                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  Exploration Phase ← Skip with --fast/--quick                               │
│  Step 5:  EXPLORATION                                                       │
│  Step 6:  Q1 (SEMANTIC necessity check)                                     │
│  Step 7:  SEMANTIC (only when Q1=true)                                      │
│  Step 8:  Q2 (VERIFICATION necessity check)                                 │
│  Step 9:  VERIFICATION (only when Q2=true)                                  │
│  Step 10: Q3 (IMPACT_ANALYSIS necessity check)                              │
│  Step 11: IMPACT_ANALYSIS (only when Q3=true / research: SESSION_COMPLETE)  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  Implementation Phase                                                        │
│  Step 12: READY (task planning + branch creation)                           │
│  Step 13: READY (implementation ×N)                                         │
│  Step 14: READY (all tasks completion check)                                │
│  Step 15: POST_IMPL_VERIFY ← Skip with --no-verify / fail→Step 12          │
│  Step 16: VERIFY_INTERVENTION (only when failure_count ≥ 3)                 │
│  Step 17: PRE_COMMIT                                                        │
│  Step 18: QUALITY_REVIEW ← Skip with --no-quality                          │
│  Step 19: MERGE → SESSION_COMPLETE                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

See [DESIGN.md](docs/DESIGN.md) for details.

---

## Requirements

- Python 3.10+
- Supported OS: Linux (Ubuntu/Debian, Fedora/RHEL, Arch), macOS, Windows (WSL)

### Tools Installed by setup.sh

**Environment Setup:**
- venv — Creates Python virtual environment
- pip — Upgraded to latest version

**System Tools:**
- ripgrep (rg) — Text search
- universal-ctags — Symbol definition search

**Python Packages:**
| Package | Version | Purpose |
|---------|---------|---------|
| mcp | ≥1.0.0 | MCP server framework |
| chromadb | ≥1.0.0 | Vector search (Forest/Map) |
| tree-sitter | ≥0.21.0, <0.22.0 | AST parsing |
| tree-sitter-languages | ≥1.10.0 | Language parsers |
| sentence-transformers | ≥2.2.0 | Embedding model |
| scikit-learn | ≥1.0.0 | Similarity calculation |
| PyYAML | ≥6.0.0 | context.yml parsing |
| asyncio | — | Async processing |
| pytest | ≥7.0.0 | Testing |
| pytest-asyncio | ≥0.21.0 | Async testing |

---

## Setup

### Step 1: MCP Server Setup (One-time)

```bash
# Clone the repository
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper

# Setup server (venv, dependencies)
./setup.sh
```

### Step 2: Project Initialization (Per-project)

```bash
# Initialize target project (indexes entire project)
./init-project.sh /path/to/your-project

# Option: Index specific directories only
./init-project.sh /path/to/your-project --include=src,packages

# Option: Specify additional exclude patterns
./init-project.sh /path/to/your-project --exclude=tests,docs,*.log
```

This creates the following (◎ = must modify, ★ = can add/modify, 〇 = can modify):

```
your-project/
├── .claude/
│   ├── CLAUDE.md          〇 Project rules for LLM (can add rules)
│   └── commands/          ← Skill files (auto-generated)
└── .code-intel/
    ├── config.json           ← Configuration (auto-generated)
    ├── context.yml           ◎ Project settings (see Step 6)
    ├── phase_contract.yml    ← Phase contract definitions (v1.13)
    ├── task_planning.md      〇 Task decomposition guide (v1.11)
    ├── user_escalation.md    〇 User escalation procedures
    ├── chroma/               ← ChromaDB data (auto-managed)
    ├── sessions/             ← Checkpoints (auto-managed)
    ├── agreements/           ← Success patterns (auto-managed)
    ├── logs/                 ← DecisionLog, OutcomeLog (auto-managed)
    ├── verifiers/            ★ Verification prompts (can add/modify)
    ├── doc_research/         ★ Document research prompts (can add/modify)
    ├── interventions/        ★ Intervention prompts (can add/modify)
    └── review_prompts/       ★ Quality review prompts (can add/modify)
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

### Step 4: Restart Claude Code

Restart to load the MCP server.

### Step 5: Verify Skills

Check that skills are available:
```bash
# In Claude Code
/code --help
```

### Codex Users (Install Skills)

Codex loads skills from `$CODEX_HOME/skills`. Copy the templates:
```bash
cp -r /path/to/llm-helper/templates/skills/codex/* "$CODEX_HOME/skills/"
```

### Step 6: Customize context.yml

The `.code-intel/context.yml` file controls various behaviors. Customize as needed:

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

# v1.3: Document research settings
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

# v1.4: Intervention system settings
interventions:
  enabled: true
  prompts_dir: "interventions/"
  threshold: 3  # Failure count before intervention triggers

# Verifier settings
verifiers:
  suggest_improvements: true
```

| Section | Description |
|---------|-------------|
| `project_rules` | Project rules source file (auto-detected) |
| `document_search` | Document search patterns for impact analysis |
| `doc_research` | Document research settings (v1.3) |
| `interventions` | Intervention system settings (v1.4) |
| `verifiers` | Verifier behavior settings |

---

## Usage

### Using /code Skill

```
/code Fix the bug in AuthService's login function where no error is shown when password is empty
```

**Default Behavior:** Full mode (exploration + implementation + verification + cleanup + quality)

### Command Options

| Long | Short | Description |
|------|-------|-------------|
| `--gate=LEVEL` | `-g=LEVEL` | Gate level: f(ull), a(uto) [default: auto] (v1.10) |
| `--no-verify` | - | Skip verification (also skips intervention) |
| `--no-quality` | - | Skip quality review (v1.5) |
| `--only-verify` | `-v` | Run verification only (skip implementation) |
| `--only-explore` | `-e` | Run exploration only (skip implementation) |
| `--fast` | `-f` | Fast mode: skip exploration, with branch |
| `--quick` | `-q` | Minimal mode: skip exploration, no branch |
| `--no-doc-research` | - | Skip document research (v1.3) |
| `--no-intervention` | `-ni` | Skip intervention system (v1.4) |
| `--resume` | `-r` | Resume session from checkpoint |
| `--clean` | `-c` | Delete stale branches + all checkpoints |
| `--rebuild` | - | Force rebuild all indexes |

**gate_level Options (v1.10):**
- `--gate=full` or `-g=f`: Execute all phases ignoring Q1/Q2/Q3 checks
- `--gate=auto` or `-g=a`: LLM judges necessity before each phase (default)

#### Phase Matrix

| Option | Doc Research | Explore | Impl | Verify | Intervene | Cleanup | Quality | Branch |
|--------|:----:|:----:|:----:|:----:|:------:|:----:|:-------:|:-------:|
| (default) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--only-explore` / `-e` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `--only-verify` / `-v` | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `--no-verify` | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--no-intervention` / `-ni` | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `--no-doc` | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--fast` / `-f` | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

#### Examples

```bash
# Full mode (default): exploration + implementation + verification + cleanup + quality
/code add login feature

# Skip verification (also skips intervention)
/code --no-verify fix this bug

# Skip quality review only
/code --no-quality fix simple typo

# Verification only (check existing implementation)
/code -v sample/hello.html

# Exploration only (no implementation, for investigation)
/code -e investigate issues in the codebase

# Fast mode (skip exploration, with branch, for known fixes)
/code -f fix known issue in login validation

# Quick mode (minimal, no branch)
/code -q change the button color to blue

# Skip document research (v1.3)
/code --no-doc-research fix typo

# Resume from checkpoint
/code -r

# Clean up stale sessions
/code -c

# Force rebuild all indexes
/code --rebuild
```

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
│   ├── outcome_log.py      ← Improvement cycle logging
│   ├── context_provider.py ← Required context (v1.1)
│   ├── impact_analyzer.py  ← Impact analysis (v1.1)
│   ├── branch_manager.py   ← Git branch isolation (v1.2)
│   └── ...
├── setup.sh                ← Server setup
├── init-project.sh         ← Project initialization
└── templates/
    ├── code-intel/         ← .code-intel templates
    └── skills/
        ├── claude/         ← Claude skill templates
        └── codex/          ← Codex skill templates
```

> For target project structure, see "[Setup > Step 2](#step-2-project-initialization-per-project)"

---

## Tool List

| Category | Tools |
|----------|-------|
| Session | `start_session`, `submit_phase`, `get_session_status` |
| Exploration | `search_text`, `find_definitions`, `find_references`, `analyze_structure`, `get_symbols`, `search_files`, `semantic_search`, `query` |
| Implementation Control | `check_write_target`, `add_explored_files`, `review_changes` |
| Branch | `cleanup_stale_branches` |
| Index | `sync_index`, `update_context`, `record_outcome`, `get_outcome_stats` |

See [DESIGN.md](docs/DESIGN.md) for details.

---

## Documentation

| Document | Content |
|----------|---------|
| [DESIGN.md](docs/DESIGN.md) | Overall design (English) |
| [DESIGN_ja.md](docs/DESIGN_ja.md) | Overall design (Japanese) |
| [DOCUMENTATION_RULES.md](docs/DOCUMENTATION_RULES.md) | Documentation management rules |

---

## CHANGELOG

Version history and detailed changes:

| Version | Description | Link |
|---------|-------------|------|
| v1.11 | submit_phase unification & task orchestration (17 tools→1, compaction resilience, server-enforced task management) | [v1.11](docs/updates/v1.11_ja.md) |
| v1.10 | Individual Phase Checks (per-phase checks, VERIFICATION/IMPACT separation, gate_level reorganization - 20-60s reduction) | [v1.10](docs/updates/v1.10_ja.md) |
| v1.9 | sync_index batch processing, VERIFICATION+IMPACT_ANALYSIS integration (15-20s reduction) | [v1.9](docs/updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode (Intent auto-detection + --only-explore, no branch creation) | [v1.8](docs/updates/v1.8_ja.md) |
| v1.7 | Parallel Execution (27-35s reduction) | [v1.7](docs/updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle (stale warning, begin_phase_gate) | [v1.6](docs/updates/v1.6_ja.md) |
| v1.5 | Quality Review | [v1.5](docs/updates/v1.5_ja.md) |
| v1.4 | Intervention System | [v1.4](docs/updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](docs/updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](docs/updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](docs/updates/v1.1_ja.md) |

---

## License

MIT
