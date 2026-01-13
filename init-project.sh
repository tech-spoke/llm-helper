#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Usage
usage() {
    echo "Usage: $0 <project-path> [options]"
    echo ""
    echo "Initialize a project for Code Intel MCP Server v1.0"
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

echo "=== Code Intel Project Initialization v1.0 ==="
echo ""
echo "Project: $PROJECT_PATH"
echo "MCP Server: $SCRIPT_DIR"
echo ""

# Create .code-intel directory structure
echo "Creating .code-intel/ directory..."
mkdir -p "$PROJECT_PATH/.code-intel/agreements"
mkdir -p "$PROJECT_PATH/.code-intel/chroma"
echo "  ✓ .code-intel/"
echo "  ✓ .code-intel/agreements/"
echo "  ✓ .code-intel/chroma/"

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
echo "  └── .code-intel/"
echo "      ├── config.json          (configuration)"
echo "      ├── agreements/          (learned NL->Symbol pairs)"
echo "      ├── chroma/              (ChromaDB vector database)"
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
echo "3. (Optional) Copy skills to your project:"
echo ""
echo "   mkdir -p $PROJECT_PATH/.claude/commands"
echo "   cp $SCRIPT_DIR/.claude/commands/*.md $PROJECT_PATH/.claude/commands/"
echo ""
echo "4. Restart Claude Code to load the MCP server."
echo ""
echo "=== Features ==="
echo ""
echo "• ChromaDB-based semantic search"
echo "• AST-based chunking for PHP, Python, JS, Blade, etc."
echo "• Fingerprint-based incremental sync (SHA256)"
echo "• Auto-sync on session start (configurable)"
echo "• Short-circuit: map hits >=0.7 skip forest search"
echo ""
echo "Use 'sync_index' tool to manually trigger re-indexing."
echo "Use 'semantic_search' tool for map/forest vector search."
echo ""
