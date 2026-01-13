#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Usage
usage() {
    echo "Usage: $0 <project-path> [options]"
    echo ""
    echo "Initialize a project for Code Intel MCP Server v3.9"
    echo ""
    echo "Arguments:"
    echo "  project-path    Path to the target project (required)"
    echo ""
    echo "Options:"
    echo "  --src-dirs      Source directories to index (default: src,lib,app)"
    echo "                  Comma-separated, relative to project root"
    echo "  --sync-ttl      Hours between auto-sync (default: 1)"
    echo "  --help          Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 /path/to/my-project"
    echo "  $0 /path/to/my-project --src-dirs=src,packages,modules"
    echo "  $0 .   # Initialize current directory"
    exit 1
}

# Parse arguments
PROJECT_PATH=""
SRC_DIRS="src,lib,app"
SYNC_TTL_HOURS="1"

while [[ $# -gt 0 ]]; do
    case $1 in
        --src-dirs=*)
            SRC_DIRS="${1#*=}"
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

echo "=== Code Intel Project Initialization v3.9 ==="
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

# Convert comma-separated dirs to JSON array
IFS=',' read -ra DIRS <<< "$SRC_DIRS"
DIRS_JSON=""
for dir in "${DIRS[@]}"; do
    dir=$(echo "$dir" | xargs)  # trim whitespace
    if [ -n "$DIRS_JSON" ]; then
        DIRS_JSON="$DIRS_JSON, "
    fi
    DIRS_JSON="$DIRS_JSON\"$dir\""
done

# Generate v3.9 config.json
cat > "$PROJECT_PATH/.code-intel/config.json" << EOF
{
  "version": "3.9",
  "embedding_model": "multilingual-e5-small",
  "source_dirs": [$DIRS_JSON],
  "exclude_patterns": [
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/venv/**",
    "**/vendor/**",
    "**/.git/**",
    "**/.code-intel/**"
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
echo "  ✓ .code-intel/config.json (v3.9)"

# Backward compatibility: keep devrag configs if they exist
if [ ! -f "$PROJECT_PATH/.code-intel/devrag-forest.json" ]; then
    # Generate devrag-forest.json for legacy fallback
    PATTERNS=""
    for dir in "${DIRS[@]}"; do
        dir=$(echo "$dir" | xargs)
        if [ -n "$PATTERNS" ]; then
            PATTERNS="$PATTERNS, "
        fi
        PATTERNS="$PATTERNS\"../$dir\""
    done

    cat > "$PROJECT_PATH/.code-intel/devrag-forest.json" << EOF
{
  "document_patterns": [$PATTERNS],
  "db_path": "./vectors-forest.db",
  "chunk_size": 500,
  "search_top_k": 5,
  "compute": {
    "device": "auto",
    "fallback_to_cpu": true
  },
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
EOF
    echo "  ✓ .code-intel/devrag-forest.json (legacy fallback)"
fi

if [ ! -f "$PROJECT_PATH/.code-intel/devrag-map.json" ]; then
    cat > "$PROJECT_PATH/.code-intel/devrag-map.json" << 'EOF'
{
  "document_patterns": ["./agreements"],
  "db_path": "./vectors-map.db",
  "chunk_size": 300,
  "search_top_k": 10,
  "compute": {
    "device": "auto",
    "fallback_to_cpu": true
  },
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
EOF
    echo "  ✓ .code-intel/devrag-map.json (legacy fallback)"
fi

# Update .gitignore
GITIGNORE_ENTRIES="
# Code Intel MCP Server v3.9
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
echo "      ├── config.json          (v3.9 configuration)"
echo "      ├── agreements/          (learned NL→Symbol pairs)"
echo "      ├── chroma/              (ChromaDB vector database)"
echo "      ├── sync_state.json      (incremental sync state)"
echo "      └── devrag-*.json        (legacy fallback configs)"
echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Install chromadb (required for v3.9):"
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
echo "=== v3.9 New Features ==="
echo ""
echo "• ChromaDB-based semantic search (replaces devrag)"
echo "• AST-based chunking for PHP, Python, JS, Blade, etc."
echo "• Fingerprint-based incremental sync (SHA256)"
echo "• Auto-sync on session start (configurable)"
echo "• Short-circuit: map hits ≥0.7 skip forest search"
echo ""
echo "Use 'sync_index' tool to manually trigger re-indexing."
echo "Use 'semantic_search' tool for map/forest vector search."
echo ""
