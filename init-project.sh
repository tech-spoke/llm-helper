#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Usage
usage() {
    echo "Usage: $0 <project-path> [options]"
    echo ""
    echo "Initialize a project for Code Intel MCP Server v3.8"
    echo ""
    echo "Arguments:"
    echo "  project-path    Path to the target project (required)"
    echo ""
    echo "Options:"
    echo "  --src-dirs      Source directories to index (default: src,lib,app)"
    echo "                  Comma-separated, relative to project root"
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

while [[ $# -gt 0 ]]; do
    case $1 in
        --src-dirs=*)
            SRC_DIRS="${1#*=}"
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

echo "=== Code Intel Project Initialization ==="
echo ""
echo "Project: $PROJECT_PATH"
echo "MCP Server: $SCRIPT_DIR"
echo ""

# Create .code-intel directory structure
echo "Creating .code-intel/ directory..."
mkdir -p "$PROJECT_PATH/.code-intel/agreements"
echo "  ✓ .code-intel/"
echo "  ✓ .code-intel/agreements/"

# Generate devrag-forest.json (森: source code search)
# Convert comma-separated dirs to JSON array with ../ prefix
IFS=',' read -ra DIRS <<< "$SRC_DIRS"
PATTERNS=""
for dir in "${DIRS[@]}"; do
    dir=$(echo "$dir" | xargs)  # trim whitespace
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
echo "  ✓ .code-intel/devrag-forest.json"

# Generate devrag-map.json (地図: agreements search)
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
echo "  ✓ .code-intel/devrag-map.json"

# Update .gitignore
if [ -f "$PROJECT_PATH/.gitignore" ]; then
    if ! grep -q ".code-intel/vectors" "$PROJECT_PATH/.gitignore" 2>/dev/null; then
        echo "" >> "$PROJECT_PATH/.gitignore"
        echo "# Code Intel MCP Server v3.8" >> "$PROJECT_PATH/.gitignore"
        echo ".code-intel/vectors-*.db" >> "$PROJECT_PATH/.gitignore"
        echo ".code-intel/learned_pairs.json" >> "$PROJECT_PATH/.gitignore"
        echo "  ✓ Updated .gitignore"
    else
        echo "  - .gitignore already configured"
    fi
else
    cat > "$PROJECT_PATH/.gitignore" << 'EOF'
# Code Intel MCP Server v3.8
.code-intel/vectors-*.db
.code-intel/learned_pairs.json
EOF
    echo "  ✓ Created .gitignore"
fi

# Get paths for MCP config
PYTHON_PATH="$SCRIPT_DIR/venv/bin/python"
SERVER_PATH="$SCRIPT_DIR/code_intel_server.py"
DEVRAG_PATH=$(command -v devrag 2>/dev/null || echo "/usr/local/bin/devrag")

echo ""
echo "=== Initialization Complete ==="
echo ""
echo "Project structure:"
echo "  $PROJECT_PATH/"
echo "  └── .code-intel/"
echo "      ├── devrag-forest.json  (source code search config)"
echo "      ├── devrag-map.json     (agreements search config)"
echo "      ├── agreements/         (learned NL→Symbol pairs)"
echo "      ├── vectors-forest.db   (created after sync)"
echo "      └── vectors-map.db      (created after sync)"
echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Add to your .mcp.json (create if not exists):"
echo ""
cat << EOF
{
  "mcpServers": {
    "devrag-map": {
      "type": "stdio",
      "command": "$DEVRAG_PATH",
      "args": ["--config", "$PROJECT_PATH/.code-intel/devrag-map.json"],
      "env": {
        "LD_LIBRARY_PATH": "/usr/local/lib"
      }
    },
    "devrag-forest": {
      "type": "stdio",
      "command": "$DEVRAG_PATH",
      "args": ["--config", "$PROJECT_PATH/.code-intel/devrag-forest.json"],
      "env": {
        "LD_LIBRARY_PATH": "/usr/local/lib"
      }
    },
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
echo "2. Initialize devrag databases:"
echo ""
echo "   cd $PROJECT_PATH/.code-intel"
echo "   devrag --config devrag-forest.json sync"
echo "   devrag --config devrag-map.json sync"
echo ""
echo "3. (Optional) Copy skills to your project:"
echo ""
echo "   mkdir -p $PROJECT_PATH/.claude/commands"
echo "   cp $SCRIPT_DIR/.claude/commands/*.md $PROJECT_PATH/.claude/commands/"
echo ""
echo "4. Restart Claude Code to load the MCP servers."
echo ""
