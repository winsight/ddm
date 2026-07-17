#!/bin/bash
# Build offline deployment package for DDM
#
# Run this on a networked Linux x86_64 machine:
#
#       ./build_offline.sh
#
# Output:
#   dist/offline_packages/    -- pip wheel files for offline install

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== DDM Offline Deployment Builder ==="
echo "  Platform: $(uname -s) $(uname -m)"
echo "  Python:   $(python3 --version 2>/dev/null || echo 'not found')"
echo ""

# ---- Step 1: Download Python dependencies ----
echo "--- Step 1: Download pip packages for offline install ---"
rm -rf dist/offline_packages
mkdir -p dist/offline_packages

# Install pip download tooling
pip3 install --quiet --upgrade pip 2>/dev/null || true

# Pass 1: Linux x86_64 binary wheels + pure-Python wheels
pip3 download \
  --platform manylinux2014_x86_64 \
  --platform any \
  --python-version 39 \
  --implementation cp \
  --only-binary=:all: \
  -r requirements.txt \
  -d dist/offline_packages/ 2>&1 | grep -v "^Requirement already" || true

# Pass 2: Source dists for anything not yet downloaded
pip3 download \
  -r requirements.txt \
  -d dist/offline_packages/ 2>&1 | grep -v "^Requirement already\|already satisfied" || true

PACKAGE_COUNT=$(ls dist/offline_packages/ | wc -l | tr -d ' ')
echo "  Downloaded $PACKAGE_COUNT packages ($(du -sh dist/offline_packages/ | cut -f1))"

# ---- Step 2: Create deploy archive ----
echo ""
echo "--- Step 2: Creating deploy archive ---"

PACKAGE_DIR="dist/ddm_deploy"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# Source code + config
cp -r ddm/ config/ clean.sh "$PACKAGE_DIR/"
cp requirements.txt "$PACKAGE_DIR/"

# Offline packages
cp -r dist/offline_packages "$PACKAGE_DIR/"

# Docs
cp docs/*.md "$PACKAGE_DIR/" 2>/dev/null || true

# Install script for the offline server
cat > "$PACKAGE_DIR/install.sh" << 'INSTALL_SCRIPT'
#!/bin/bash
# DDM offline install script — run on the target CentOS 7.9 server
# Uses --user to install without root privileges
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== DDM Offline Install (non-root) ==="

# Install Python dependencies from local packages (--user, no root needed)
echo "Installing Python packages to ~/.local/ ..."
pip3 install --user --no-index --find-links offline_packages/ -r requirements.txt 2>&1 | tail -5

# Ensure ~/.local/bin is in PATH for csh users
if ! echo "$PATH" | grep -q ".local/bin"; then
    echo 'set path = ($HOME/.local/bin $path)' >> ~/.cshrc 2>/dev/null || true
    echo "  Added ~/.local/bin to ~/.cshrc"
fi

# Create config if not exists
if [ ! -f config/config.yaml ]; then
    cp config/config.yaml.example config/config.yaml 2>/dev/null || true
    echo "  Created config/config.yaml — please edit it for your environment"
fi

echo ""
echo "=== Install Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config/config.yaml"
echo "  2. source ~/.cshrc    (or re-login)"
echo "  3. python3 -m ddm check"
echo "  4. python3 -m ddm submit -m <MODULE> -t <TAG>"
INSTALL_SCRIPT
chmod +x "$PACKAGE_DIR/install.sh"

# README
cat > "$PACKAGE_DIR/README.txt" << 'README'
DDM — EDA Data Delivery Manager (Offline Deploy Package)
=========================================================

Quick start:
  ./install.sh
  python3 -m ddm check
  python3 -m ddm submit -m CPU -t PV_ITER

Config: edit config/config.yaml before use.
Docs:   USER_GUIDE.md | DEPLOY.md | ARCHITECTURE.md
README

cd dist
tar -czf ddm_deploy.tar.gz ddm_deploy/
rm -rf ddm_deploy/

echo ""
echo "=== Done ==="
echo "  Deploy archive: dist/ddm_deploy.tar.gz ($(du -sh dist/ddm_deploy.tar.gz | cut -f1))"
echo "  Offline wheels: dist/offline_packages/ ($PACKAGE_COUNT packages)"
echo ""
echo "Deploy to target server:"
echo "  scp dist/ddm_deploy.tar.gz user@centos7-server:~/"
echo "  ssh user@centos7-server"
echo "  cd ~ && tar -xzf ddm_deploy.tar.gz"
echo "  cd ddm_deploy && ./install.sh"
