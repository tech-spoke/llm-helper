#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Code Intel MCP Server v3.6 Setup ==="

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install Python dependencies
echo "Installing Python dependencies..."
./venv/bin/pip install -q -r requirements.txt

# Check external tools
echo ""
echo "Checking required tools..."

check_tool() {
    if command -v "$1" &> /dev/null; then
        echo "  ✓ $1 found"
        return 0
    else
        echo "  ✗ $1 not found - $2"
        return 1
    fi
}

MISSING=0
check_tool "rg" "Install with: apt install ripgrep" || MISSING=1
check_tool "ctags" "Install with: apt install universal-ctags" || MISSING=1

echo ""
echo "Checking optional tools..."
check_tool "repomix" "Install with: npm install -g repomix (optional)" || true
check_tool "devrag" "See: https://github.com/tomohiro-owada/devrag (optional)" || true

if [ $MISSING -eq 1 ]; then
    echo ""
    echo "⚠ Required tools missing. Install them before using the server."
fi

# Generate MCP config snippet
PYTHON_PATH="$SCRIPT_DIR/venv/bin/python"
SERVER_PATH="$SCRIPT_DIR/code_intel_server.py"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "1. Add this to your project's .mcp.json:"
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
echo "2. (Optional) Copy skills to your project:"
echo ""
echo "   mkdir -p .claude/commands"
echo "   cp $SCRIPT_DIR/.claude/commands/*.md .claude/commands/"
echo ""
echo "3. Restart Claude Code to load the MCP server."
