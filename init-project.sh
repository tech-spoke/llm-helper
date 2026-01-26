#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Usage
usage() {
    echo "Usage: $0 <project-path> [options]"
    echo ""
    echo "Initialize a project for Code Intel MCP Server v1.11"
    echo ""
    echo "Arguments:"
    echo "  project-path    Path to the target project (required)"
    echo ""
    echo "Options:"
    echo "  --include       Directories/files to index (default: entire project)"
    echo "                  Comma-separated, relative to project root"
    echo "                  Supports: directories, files, glob patterns"
    echo "  --exclude       Additional directories/files to exclude"
    echo "                  Comma-separated, added to default exclusions"
    echo "  --sync-ttl      Hours between auto-sync (default: 1)"
    echo "  --help          Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 /path/to/my-project"
    echo "  $0 /path/to/my-project --include=src,lib"
    echo "  $0 /path/to/my-project --exclude=tests,docs,*.log"
    echo "  $0 /path/to/my-project --include=app/src --exclude=app/src/vendor"
    exit 1
}

# Parse arguments
PROJECT_PATH=""
INCLUDE_DIRS=""
EXCLUDE_DIRS=""
SYNC_TTL_HOURS="1"

while [[ $# -gt 0 ]]; do
    case $1 in
        --include=*)
            INCLUDE_DIRS="${1#*=}"
            shift
            ;;
        --exclude=*)
            EXCLUDE_DIRS="${1#*=}"
            shift
            ;;
        --sync-ttl=*)
            SYNC_TTL_HOURS="${1#*=}"
            shift
            ;;
        --help|-h)
            usage
            ;;
        -*)
            echo "Unknown option: $1"
            usage
            ;;
        *)
            if [ -z "$PROJECT_PATH" ]; then
                PROJECT_PATH="$1"
            else
                echo "Too many arguments"
                usage
            fi
            shift
            ;;
    esac
done

if [ -z "$PROJECT_PATH" ]; then
    echo "Error: project-path is required"
    echo ""
    usage
fi

# Resolve absolute path
PROJECT_PATH="$(cd "$PROJECT_PATH" 2>/dev/null && pwd)" || {
    echo "Error: Directory does not exist: $PROJECT_PATH"
    exit 1
}

echo "=== Code Intel Project Initialization v1.11 ==="
echo ""
echo "Project: $PROJECT_PATH"
echo "MCP Server: $SCRIPT_DIR"
echo ""

# Create .code-intel directory structure
echo "Creating .code-intel/ directory..."
mkdir -p "$PROJECT_PATH/.code-intel/agreements"
mkdir -p "$PROJECT_PATH/.code-intel/chroma"
mkdir -p "$PROJECT_PATH/.code-intel/logs"
mkdir -p "$PROJECT_PATH/.code-intel/verifiers"
mkdir -p "$PROJECT_PATH/.code-intel/doc_research"
mkdir -p "$PROJECT_PATH/.code-intel/review_prompts"
mkdir -p "$PROJECT_PATH/.code-intel/interventions"
echo "  ✓ .code-intel/"
echo "  ✓ .code-intel/agreements/"
echo "  ✓ .code-intel/chroma/"
echo "  ✓ .code-intel/logs/"
echo "  ✓ .code-intel/verifiers/"
echo "  ✓ .code-intel/doc_research/"
echo "  ✓ .code-intel/review_prompts/"
echo "  ✓ .code-intel/interventions/"

# Process --include (default to "." for entire project)
if [ -z "$INCLUDE_DIRS" ]; then
    INCLUDE_DIRS="."
fi

# Convert comma-separated include dirs to JSON array
IFS=',' read -ra INCL_ARRAY <<< "$INCLUDE_DIRS"
INCL_JSON=""
for item in "${INCL_ARRAY[@]}"; do
    item=$(echo "$item" | xargs)  # trim whitespace
    if [ -n "$INCL_JSON" ]; then
        INCL_JSON="$INCL_JSON, "
    fi
    INCL_JSON="$INCL_JSON\"$item\""
done

# Default exclude patterns
DEFAULT_EXCLUDES=(
    "**/node_modules/**"
    "**/__pycache__/**"
    "**/venv/**"
    "**/vendor/**"
    "**/.git/**"
    "**/.code-intel/**"
)

# Process --exclude and merge with defaults
declare -a FINAL_EXCLUDES=("${DEFAULT_EXCLUDES[@]}")

if [ -n "$EXCLUDE_DIRS" ]; then
    IFS=',' read -ra EXCL_ARRAY <<< "$EXCLUDE_DIRS"
    for item in "${EXCL_ARRAY[@]}"; do
        item=$(echo "$item" | xargs)  # trim whitespace
        # Convert to glob pattern if needed
        if [[ "$item" == *"*"* ]]; then
            # Already a glob pattern, use as-is but ensure ** prefix
            if [[ "$item" != "**/"* ]]; then
                item="**/$item"
            fi
        elif [ -d "$PROJECT_PATH/$item" ]; then
            # Directory: add /** suffix
            item="**/$item/**"
        elif [ -f "$PROJECT_PATH/$item" ]; then
            # File: add **/ prefix
            item="**/$item"
        else
            # Assume it's a pattern/path, add ** prefix and suffix
            item="**/$item/**"
        fi
        FINAL_EXCLUDES+=("$item")
    done
fi

# Convert exclude array to JSON
EXCL_JSON=""
for pattern in "${FINAL_EXCLUDES[@]}"; do
    if [ -n "$EXCL_JSON" ]; then
        EXCL_JSON="$EXCL_JSON,"
    fi
    EXCL_JSON="$EXCL_JSON
    \"$pattern\""
done

# Generate config.json
cat > "$PROJECT_PATH/.code-intel/config.json" << EOF
{
  "version": "1.0",
  "embedding_model": "multilingual-e5-small",
  "source_dirs": [$INCL_JSON],
  "exclude_patterns": [$EXCL_JSON
  ],
  "chunk_strategy": "ast",
  "chunk_max_tokens": 512,
  "sync_ttl_hours": $SYNC_TTL_HOURS,
  "sync_on_start": true,
  "max_chunks": 10000,
  "search_weights": {
    "vector": 0.4,
    "keyword": 0.2,
    "definition": 0.3,
    "reference": 0.1
  }
}
EOF
echo "  ✓ .code-intel/config.json"

# Generate context.yml (only if not exists - preserve user settings)
if [ ! -f "$PROJECT_PATH/.code-intel/context.yml" ]; then
    cat > "$PROJECT_PATH/.code-intel/context.yml" << 'EOF'
# Code Intel Context Configuration v1.3
# See: https://github.com/tech-spoke/llm-helper

# Project rules (optional, auto-detected from CLAUDE.md)
# project_rules:
#   source: "CLAUDE.md"
#   summary: ""  # Auto-generated by LLM

# Document research settings (v1.3)
# Sub-agent researches design docs for task-specific rules
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

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
    - ".venv/**"
    - "__pycache__/**"
EOF
    echo "  ✓ .code-intel/context.yml"
else
    echo "  - .code-intel/context.yml already exists (skipped)"
fi

# Copy default verifier templates (if not exists)
if [ -d "$SCRIPT_DIR/.code-intel/verifiers" ]; then
    for file in "$SCRIPT_DIR/.code-intel/verifiers"/*.md; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            if [ ! -f "$PROJECT_PATH/.code-intel/verifiers/$filename" ]; then
                cp "$file" "$PROJECT_PATH/.code-intel/verifiers/"
                echo "  ✓ .code-intel/verifiers/$filename"
            fi
        fi
    done
fi

# Copy default doc_research prompts (if not exists)
if [ -d "$SCRIPT_DIR/.code-intel/doc_research" ]; then
    for file in "$SCRIPT_DIR/.code-intel/doc_research"/*.md; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            if [ ! -f "$PROJECT_PATH/.code-intel/doc_research/$filename" ]; then
                cp "$file" "$PROJECT_PATH/.code-intel/doc_research/"
                echo "  ✓ .code-intel/doc_research/$filename"
            fi
        fi
    done
fi

# Copy review_prompts (garbage_detection, quality_review)
if [ -d "$SCRIPT_DIR/.code-intel/review_prompts" ]; then
    for file in "$SCRIPT_DIR/.code-intel/review_prompts"/*.md; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            if [ ! -f "$PROJECT_PATH/.code-intel/review_prompts/$filename" ]; then
                cp "$file" "$PROJECT_PATH/.code-intel/review_prompts/"
                echo "  ✓ .code-intel/review_prompts/$filename"
            fi
        fi
    done
fi

# Copy interventions prompts (v1.4)
if [ -d "$SCRIPT_DIR/.code-intel/interventions" ]; then
    for file in "$SCRIPT_DIR/.code-intel/interventions"/*.md; do
        if [ -f "$file" ]; then
            filename=$(basename "$file")
            if [ ! -f "$PROJECT_PATH/.code-intel/interventions/$filename" ]; then
                cp "$file" "$PROJECT_PATH/.code-intel/interventions/"
                echo "  ✓ .code-intel/interventions/$filename"
            fi
        fi
    done
fi

# Copy .claude directory (project rules, guides, and skills)
if [ -d "$SCRIPT_DIR/.claude" ]; then
    mkdir -p "$PROJECT_PATH/.claude/commands"

    # Copy CLAUDE.md
    if [ -f "$SCRIPT_DIR/.claude/CLAUDE.md" ] && [ ! -f "$PROJECT_PATH/.claude/CLAUDE.md" ]; then
        cp "$SCRIPT_DIR/.claude/CLAUDE.md" "$PROJECT_PATH/.claude/"
        echo "  ✓ .claude/CLAUDE.md"
    fi

    # Copy PARALLEL_GUIDE.md
    if [ -f "$SCRIPT_DIR/.claude/PARALLEL_GUIDE.md" ] && [ ! -f "$PROJECT_PATH/.claude/PARALLEL_GUIDE.md" ]; then
        cp "$SCRIPT_DIR/.claude/PARALLEL_GUIDE.md" "$PROJECT_PATH/.claude/"
        echo "  ✓ .claude/PARALLEL_GUIDE.md"
    fi

    # Copy skill files (commands/*.md)
    if [ -d "$SCRIPT_DIR/.claude/commands" ]; then
        for file in "$SCRIPT_DIR/.claude/commands"/*.md; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                if [ ! -f "$PROJECT_PATH/.claude/commands/$filename" ]; then
                    cp "$file" "$PROJECT_PATH/.claude/commands/"
                    echo "  ✓ .claude/commands/$filename"
                fi
            fi
        done
    fi
fi

# Show configured paths
echo ""
echo "Index configuration:"
echo "  Include: $INCLUDE_DIRS"
if [ -n "$EXCLUDE_DIRS" ]; then
    echo "  Exclude: $EXCLUDE_DIRS (+ defaults)"
else
    echo "  Exclude: (defaults only)"
fi

# Update .gitignore
GITIGNORE_ENTRIES="
# Code Intel MCP Server
.code-intel/vectors-*.db
.code-intel/chroma/
.code-intel/sync_state.json
.code-intel/.last_sync
.code-intel/learned_pairs.json
"

if [ -f "$PROJECT_PATH/.gitignore" ]; then
    if ! grep -q ".code-intel/chroma" "$PROJECT_PATH/.gitignore" 2>/dev/null; then
        echo "$GITIGNORE_ENTRIES" >> "$PROJECT_PATH/.gitignore"
        echo "  ✓ Updated .gitignore"
    else
        echo "  - .gitignore already configured"
    fi
else
    echo "$GITIGNORE_ENTRIES" > "$PROJECT_PATH/.gitignore"
    echo "  ✓ Created .gitignore"
fi

# Get paths for MCP config
PYTHON_PATH="$SCRIPT_DIR/venv/bin/python"
SERVER_PATH="$SCRIPT_DIR/code_intel_server.py"

echo ""
echo "=== Initialization Complete ==="
echo ""
echo "Project structure:"
echo "  $PROJECT_PATH/"
echo "  ├── .claude/"
echo "  │   ├── CLAUDE.md            (project rules for LLM)"
echo "  │   ├── PARALLEL_GUIDE.md    (efficiency guide for LLM)"
echo "  │   └── commands/            (skill files: /code, /exp, etc.)"
echo "  └── .code-intel/"
echo "      ├── config.json          (indexing configuration)"
echo "      ├── context.yml          (context & doc research settings)"
echo "      ├── agreements/          (learned NL->Symbol pairs)"
echo "      ├── chroma/              (ChromaDB vector database)"
echo "      ├── logs/                (DecisionLog, OutcomeLog)"
echo "      ├── verifiers/           (verification prompts)"
echo "      ├── doc_research/        (document research prompts)"
echo "      ├── review_prompts/      (garbage detection, quality review)"
echo "      ├── interventions/       (intervention prompts)"
echo "      └── sync_state.json      (incremental sync state)"
echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Install chromadb (required):"
echo ""
echo "   pip install chromadb"
echo ""
echo "2. Add to your .mcp.json (create if not exists):"
echo ""
cat << EOF
{
  "mcpServers": {
    "code-intel": {
      "type": "stdio",
      "command": "$PYTHON_PATH",
      "args": ["$SERVER_PATH"],
      "env": {
        "PYTHONPATH": "$SCRIPT_DIR"
      }
    }
  }
}
EOF
echo ""
echo "3. Restart Claude Code to load the MCP server."
echo ""
echo "=== Features ==="
echo ""
echo "• ChromaDB-based semantic search (Forest/Map architecture)"
echo "• AST-based chunking for PHP, Python, JS, Blade, etc."
echo "• Fingerprint-based incremental sync (SHA256)"
echo "• Phase-gated implementation workflow"
echo "• Document research with sub-agent (v1.3)"
echo "• Intervention system for verification failures (v1.4)"
echo "• Quality review phase (v1.5)"
echo "• Git branch isolation for garbage detection (v1.2/v1.6)"
echo "• Parallel execution for search_text, Read, Grep (v1.7 - saves 27-35s)"
echo "• Optimized phase transitions with skip_implementation (v1.8)"
echo "• Phase necessity checks Q1/Q2/Q3 (v1.10)"
echo "• Deferred branch creation to READY phase (v1.11)"
echo ""
echo "Use '/code' skill for guided implementation workflow."
echo "Use 'sync_index' tool to manually trigger re-indexing."
echo ""
