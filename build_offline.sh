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

# Source code + config + scripts
cp -r ddm/ config/ "$PACKAGE_DIR/"
cp clean.sh ddm.complete.csh ddm_update.csh ddm_web.py "$PACKAGE_DIR/"
cp requirements.txt "$PACKAGE_DIR/"

# Offline packages
cp -r dist/offline_packages "$PACKAGE_DIR/"

# Docs
cp docs/*.md "$PACKAGE_DIR/" 2>/dev/null || true

# Install script for the offline server
cat > "$PACKAGE_DIR/install.sh" << 'INSTALL_SCRIPT'
#!/bin/bash
# DDM deployment script — shared NFS + tcsh environment
# Run once per server/node by admin, then each user runs setup_user.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
DDM_ROOT="$SCRIPT_DIR"

echo "============================================"
echo "  DDM 部署脚本"
echo "  DDM_ROOT = $DDM_ROOT"
echo "============================================"
echo ""

# ---- Step 1: install Python deps if needed ----
echo "--- Step 1: Python 依赖 ---"
if [ -d offline_packages ] && [ "$(ls -A offline_packages 2>/dev/null)" ]; then
    echo "  安装离线 pip 包到 ~/.local/ ..."
    pip3 install --user --no-index --find-links offline_packages/ -r requirements.txt 2>&1 | tail -3
else
    echo "  跳过 (无离线包)，请确认系统已安装: click rich pydantic pyyaml loguru psutil"
fi
echo ""

# ---- Step 2: fix permissions ----
echo "--- Step 2: 权限 ---"
chmod -R g+rX,o+rX "$DDM_ROOT"
chmod -R g+w "$DDM_ROOT/repository" "$DDM_ROOT/logs" "$DDM_ROOT/a0.outgoing" 2>/dev/null || true
# SGID on repo dirs so new files inherit group
for d in repository repository/raw repository/ready repository/release; do
    mkdir -p "$DDM_ROOT/$d"
    chmod 2775 "$DDM_ROOT/$d" 2>/dev/null || true
done
echo "  目录权限已设置 (2775 SGID)"
echo ""

# ---- Step 3: config.yaml ----
echo "--- Step 3: 配置 ---"
if [ ! -f config/config.yaml ]; then
    echo "  请创建 config/config.yaml (参考 docs/SETUP_TCSH.md)"
    echo "  关键配置: outgoing_root + repository_root 用绝对路径"
else
    echo "  config/config.yaml 已存在"
fi
echo ""

# ---- Step 4: user setup script ----
cat > "$DDM_ROOT/setup_user.sh" << 'USER_SETUP'
#!/bin/bash
# Run this ONCE per user to configure their tcsh environment
DDM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== DDM User Setup ==="
echo "  DDM_ROOT = $DDM_ROOT"
echo ""

# Add to ~/.cshrc if not already there
if ! grep -q "DDM" ~/.cshrc 2>/dev/null; then
    cat >> ~/.cshrc << 'CSHRC'

# ===== DDM =====
setenv PYTHONPATH CSHRC_DDM_ROOT
alias ddm 'python3 -m ddm'
if (-f CSHRC_DDM_ROOT/ddm.complete.csh) source CSHRC_DDM_ROOT/ddm.complete.csh
CSHRC
    # Replace placeholder with actual path
    sed -i "s|CSHRC_DDM_ROOT|$DDM_ROOT|g" ~/.cshrc
    echo "  ~/.cshrc 已配置"
else
    echo "  ~/.cshrc 已有 DDM 配置，跳过"
fi

echo ""
echo "=== Done ==="
echo "  请执行: source ~/.cshrc"
echo "  然后:   ddm check"
USER_SETUP
chmod +x "$DDM_ROOT/setup_user.sh"

# ---- Done ----
echo "============================================"
echo "  部署完成"
echo ""
echo "  下一步 — 管理员:"
echo "    1. 编辑 $DDM_ROOT/config/config.yaml"
echo "    2. 路径用绝对路径 (如 /nfs/eda/shared/ddm/...)"
echo "    3. sh $DDM_ROOT/setup_user.sh"
echo ""
echo "  下一步 — 普通用户:"
echo "    sh $DDM_ROOT/setup_user.sh"
echo "    source ~/.cshrc"
echo "    ddm check"
echo "============================================"
INSTALL_SCRIPT
chmod +x "$PACKAGE_DIR/install.sh"

# README
cat > "$PACKAGE_DIR/README.txt" << 'README'
DDM — EDA Data Delivery Manager (Offline Deploy Package)
=========================================================

For admin (once per deploy):
  ./install.sh              # install deps, fix perms, generate setup_user.sh
  vi config/config.yaml     # set absolute paths (outgoing_root, repository_root)

For each user:
  sh ./setup_user.sh        # adds DDM to ~/.cshrc
  source ~/.cshrc
  ddm check
  ddm submit -m CPU -t PV_ITER

Docs: SETUP_TCSH.md | USER_GUIDE.md | DEPLOY.md | WORKFLOWS.md
README

cd dist
if [ -d ddm_deploy ] && [ "$(ls -A ddm_deploy 2>/dev/null)" ]; then
    tar -czf ddm_deploy.tar.gz ddm_deploy/
    ARCHIVE_SIZE=$(du -sh ddm_deploy.tar.gz 2>/dev/null | cut -f1)
else
    ARCHIVE_SIZE="(empty)"
fi
rm -rf ddm_deploy/
cd ..

echo ""
echo "=== Done ==="
if [ -f dist/ddm_deploy.tar.gz ]; then
    echo "  Deploy archive: dist/ddm_deploy.tar.gz ($ARCHIVE_SIZE)"
fi
echo "  Offline wheels: dist/offline_packages/ ($PACKAGE_COUNT packages)"
echo ""
echo "Deploy to target server:"
echo "  scp dist/ddm_deploy.tar.gz user@centos7-server:~/"
echo "  ssh user@centos7-server"
echo "  cd ~ && tar -xzf ddm_deploy.tar.gz"
echo "  cd ddm_deploy && ./install.sh"
