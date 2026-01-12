#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Code Intel MCP Server v3.8 Setup ==="
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
    read -p "Required tools missing. Install automatically? [Y/n] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        install_required_tools
        # Re-check
        MISSING=0
        check_tool "rg" || MISSING=1
        check_tool "ctags" || MISSING=1
    fi
fi

echo ""
echo "Checking optional tools..."
check_tool "repomix" || echo "    Install with: npm install -g repomix (optional)"
check_tool "devrag" || echo "    See: https://github.com/tomohiro-owada/devrag (required for v3.8)"

# Check and install ONNX Runtime for devrag
install_onnxruntime() {
    local ORT_VERSION="1.22.0"
    local ORT_URL="https://github.com/microsoft/onnxruntime/releases/download/v${ORT_VERSION}/onnxruntime-linux-x64-${ORT_VERSION}.tgz"

    echo ""
    echo "Installing ONNX Runtime ${ORT_VERSION} for devrag..."

    cd /tmp
    wget -q "${ORT_URL}" -O onnxruntime.tgz
    tar xzf onnxruntime.tgz
    sudo cp onnxruntime-linux-x64-${ORT_VERSION}/lib/*.so* /usr/local/lib/
    sudo ln -sf /usr/local/lib/libonnxruntime.so /usr/local/lib/onnxruntime.so
    sudo ldconfig 2>/dev/null || true
    rm -rf onnxruntime.tgz onnxruntime-linux-x64-${ORT_VERSION}
    cd - > /dev/null

    echo "  ✓ ONNX Runtime ${ORT_VERSION} installed"
}

# Check if ONNX Runtime is available
if ! ldconfig -p 2>/dev/null | grep -q libonnxruntime; then
    echo ""
    echo "ONNX Runtime not found (required for devrag embeddings)"
    read -p "Install ONNX Runtime automatically? [Y/n] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        install_onnxruntime
    else
        echo "  ⚠ Skip ONNX Runtime installation. devrag may not work."
    fi
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
echo "Next steps:"
echo "  1. Initialize your target project:"
echo "     ./init-project.sh /path/to/your/project"
echo ""
echo "  2. Follow the instructions to configure .mcp.json"
echo ""
