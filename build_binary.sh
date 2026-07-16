#!/bin/bash
# Build DDM standalone binary with PyInstaller
#
# USAGE:
#   Method A (Docker, recommended for macOS → CentOS):
#       docker build -t ddm-builder -f Dockerfile.build .
#       docker run --rm -v $(pwd):/build ddm-builder
#
#   Method B (directly on CentOS 7.9 server):
#       pip3 install pyinstaller
#       pip3 install -r requirements.txt
#       ./build_binary.sh
#
# OUTPUT:
#   dist/ddm           — standalone Linux ELF binary
#   dist/ddm.tar.gz    — deployable archive

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== DDM Binary Build ==="
echo "  Platform: $(uname -s) $(uname -m)"
echo "  Python:   $(python3 --version 2>/dev/null || python --version 2>/dev/null || echo 'not found')"

# Verify PyInstaller is available
python3 -c "import PyInstaller" 2>/dev/null || python -c "import PyInstaller" 2>/dev/null || {
    echo ""
    echo "[ERROR] PyInstaller not found. Install it first:"
    echo "  pip3 install pyinstaller"
    exit 1
}

# Find the right python to use for PyInstaller
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
echo "  Using:    $PYTHON"
echo ""

# Clean
rm -rf build/ dist/ __pycache__/
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Build
echo "--- Running PyInstaller ---"
pyinstaller --clean --noconfirm ddm.spec

echo ""
echo "=== Build Complete ==="
echo "  Binary:   dist/ddm ($(du -h dist/ddm | cut -f1))"
file dist/ddm 2>/dev/null || true

# Quick sanity check (skip on cross-build — binary won't run on non-Linux)
if [ "$(uname -s)" = "Linux" ]; then
    echo ""
    echo "--- Sanity check ---"
    dist/ddm --help > /dev/null 2>&1 && echo "  ✓ binary starts correctly" || echo "  ✗ binary failed (may need glibc check)"
fi

# --- Create deployable archive ---
echo ""
echo "--- Creating deploy archive ---"

PACKAGE_DIR="dist/ddm_package"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

cp dist/ddm "$PACKAGE_DIR/"
cp config/config.yaml "$PACKAGE_DIR/config.yaml.example"
cp USER_GUIDE.md DEPLOY.md ARCHITECTURE.md "$PACKAGE_DIR/" 2>/dev/null || true
cp clean.sh "$PACKAGE_DIR/"

cat > "$PACKAGE_DIR/README.txt" << 'README'
DDM — EDA Data Delivery Manager (Standalone Binary)
====================================================

Quick start:
  1. cp config.yaml.example config.yaml
  2. Edit config.yaml → set outgoing_root, repository_root, admins
  3. ./ddm check
  4. ./ddm submit -m CPU -t PV_ITER

No Python installation required. The binary bundles everything.

Config priority:
  -c <path>            explicit path (highest)
  ./config/config.yaml relative to working directory
  Built-in default     (read-only, bundled at build time)

Docs: USER_GUIDE.md | DEPLOY.md | ARCHITECTURE.md
README

cd dist
tar -czf ddm.tar.gz ddm_package/
rm -rf ddm_package/

echo ""
echo "=== Done ==="
echo "  Binary:  dist/ddm"
echo "  Archive: dist/ddm.tar.gz ($(du -h dist/ddm.tar.gz | cut -f1))"
echo ""
echo "Deploy to target server:"
echo "  scp dist/ddm.tar.gz user@centos7-server:/opt/"
echo "  ssh user@centos7-server"
echo "  cd /opt && tar -xzf ddm.tar.gz"
echo "  cd ddm_package && cp config.yaml.example config.yaml"
echo "  vi config.yaml   # edit paths for your environment"
echo "  ./ddm check"
