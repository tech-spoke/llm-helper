# Code Intelligence MCP Server v1.0

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

```
EXPLORATION → SEMANTIC → VERIFICATION → READY
     ↓           ↓           ↓           ↓
  code-intel  semantic    verify      implementation
   tools      search    (confirm)      allowed
             (hypothesis)
```

| Phase | Allowed | Forbidden |
|-------|---------|-----------|
| EXPLORATION | code-intel tools | semantic_search |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel tools | semantic_search |
| READY | all | - |

---

## Tool List

### Code Intelligence

| Tool | Purpose |
|------|---------|
| `query` | Intelligent query in natural language |
| `find_definitions` | Symbol definition search (ctags) |
| `find_references` | Symbol reference search (ripgrep) |
| `search_text` | Text search (ripgrep) |
| `analyze_structure` | Code structure analysis (tree-sitter) |
| `get_symbols` | Get symbol list |
| `sync_index` | Index source code to ChromaDB |
| `semantic_search` | Unified vector search of map/forest |

### Session Management

| Tool | Purpose |
|------|---------|
| `start_session` | Start session |
| `set_query_frame` | Set QueryFrame (Quote verification) |
| `get_session_status` | Check current phase/status |
| `submit_understanding` | Complete EXPLORATION |
| `validate_symbol_relevance` | Embedding verification |
| `submit_semantic` | Complete SEMANTIC |
| `submit_verification` | Complete VERIFICATION |
| `check_write_target` | Check write permission |
| `add_explored_files` | Add explored files |
| `revert_to_exploration` | Return to EXPLORATION |

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

---

## Usage

### Using /code skill (recommended)

```
/code Fix the bug in AuthService's login function where no error is shown when password is empty
```

The skill automatically:
1. Failure check (auto-detect and record previous failures)
2. Intent determination
3. Session start (auto-sync)
4. QueryFrame extraction and verification
5. EXPLORATION (find_definitions, find_references, etc.)
6. Symbol verification (Embedding)
7. SEMANTIC if needed
8. VERIFICATION (hypothesis verification)
9. READY (implementation)

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

### Python Packages

```
mcp>=1.0.0
chromadb>=1.0.0
tree-sitter>=0.21.0
tree-sitter-languages>=1.10.0
sentence-transformers>=2.2.0
scikit-learn>=1.0.0
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
│   ├── chroma/             ← ChromaDB data
│   ├── agreements/         ← Success pairs
│   ├── logs/               ← DecisionLog, OutcomeLog
│   └── sync_state.json
├── .claude/commands/       ← Skills (optional copy)
└── src/                    ← Your source code
```

---

## Documentation

| Document | Content |
|----------|---------|
| [DESIGN_v1.0.md](docs/en/DESIGN_v1.0.md) | Overall design |
| [INTERNALS_v1.0.md](docs/en/INTERNALS_v1.0.md) | Internal details |
| [DESIGN_v1.0.md (Japanese)](docs/ja/DESIGN_v1.0.md) | Overall design (Japanese) |
| [INTERNALS_v1.0.md (Japanese)](docs/ja/INTERNALS_v1.0.md) | Internal details (Japanese) |

---

## License

MIT
