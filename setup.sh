#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Code Intel MCP Server v1.0 Setup ==="
echo ""
echo "This script sets up the MCP server itself."
echo "For project initialization, use: ./init-project.sh <project-path>"
echo ""

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install Python dependencies
echo "Upgrading pip..."
./venv/bin/pip install -q --upgrade pip

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
        echo "  ✗ $1 not found"
        return 1
    fi
}

install_required_tools() {
    echo ""
    echo "Installing required tools..."

    # Detect package manager
    if command -v apt &> /dev/null; then
        echo "  Using apt..."
        sudo apt update -qq
        sudo apt install -y ripgrep universal-ctags
    elif command -v brew &> /dev/null; then
        echo "  Using brew..."
        brew install ripgrep universal-ctags
    elif command -v dnf &> /dev/null; then
        echo "  Using dnf..."
        sudo dnf install -y ripgrep ctags
    elif command -v pacman &> /dev/null; then
        echo "  Using pacman..."
        sudo pacman -S --noconfirm ripgrep ctags
    else
        echo "  ✗ No supported package manager found (apt/brew/dnf/pacman)"
        echo "  Please install ripgrep and universal-ctags manually."
        return 1
    fi
}

MISSING=0
check_tool "rg" || MISSING=1
check_tool "ctags" || MISSING=1

if [ $MISSING -eq 1 ]; then
    echo ""
    if [ -t 0 ]; then
        # Interactive mode: ask user
        read -p "Required tools missing. Install automatically? [Y/n] " -n 1 -r
        echo ""
    else
        # Non-interactive mode: auto-install
        echo "Non-interactive mode detected. Installing required tools automatically..."
        REPLY="y"
    fi
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        install_required_tools
        # Re-check
        MISSING=0
        check_tool "rg" || MISSING=1
        check_tool "ctags" || MISSING=1
    fi
fi

# Verify chromadb installation
echo ""
echo "Verifying ChromaDB installation..."
if ./venv/bin/python -c "import chromadb; print(f'  ✓ chromadb {chromadb.__version__}')" 2>/dev/null; then
    :
else
    echo "  ✗ chromadb installation failed"
    echo "  Trying to install again..."
    ./venv/bin/pip install chromadb
fi

if [ $MISSING -eq 1 ]; then
    echo ""
    echo "⚠ Required tools still missing. Install them manually before using the server."
    echo "  Ubuntu/Debian: sudo apt install ripgrep universal-ctags"
    echo "  macOS: brew install ripgrep universal-ctags"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "MCP server is ready at: $SCRIPT_DIR"
echo ""
echo "Features:"
echo "  • ChromaDB-based semantic search"
echo "  • AST-based chunking for PHP, Python, JS, Blade, etc."
echo "  • Fingerprint-based incremental sync"
echo "  • Auto-sync on session start"
echo "  • Git branch isolation for garbage detection"
echo ""
echo "Next steps:"
echo "  1. Initialize your target project:"
echo "     ./init-project.sh /path/to/your/project"
echo ""
echo "  2. Follow the instructions to configure .mcp.json"
echo ""
