#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Code Intel MCP Server Setup ==="

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install Python dependencies
echo "Installing Python dependencies..."
./venv/bin/pip install -q mcp tree-sitter tree-sitter-languages

# Check external tools
echo ""
echo "Checking external tools..."

check_tool() {
    if command -v "$1" &> /dev/null; then
        echo "  ✓ $1 found"
        return 0
    else
        echo "  ✗ $1 not found - $2"
        return 1
    fi
}

check_tool "rg" "Install with: apt install ripgrep"
check_tool "ctags" "Install with: apt install universal-ctags"
check_tool "repomix" "Install with: npm install -g repomix"

# Generate MCP config snippet
PYTHON_PATH="$SCRIPT_DIR/venv/bin/python"
SERVER_PATH="$SCRIPT_DIR/code_intel_server.py"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Add this to ~/.claude/mcp.json (or project .mcp.json):"
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
