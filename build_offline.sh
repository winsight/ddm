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
cp clean.sh ddm.complete.csh ddm_update.csh ddm_web.py setup.py "$PACKAGE_DIR/"
cp requirements.txt "$PACKAGE_DIR/"

# Offline packages
cp -r dist/offline_packages "$PACKAGE_DIR/"

# Docs
cp -r docs "$PACKAGE_DIR/"

# Install script for the offline server
cat > "$PACKAGE_DIR/install.sh" << 'INSTALL_SCRIPT'
#!/bin/bash
# DDM deployment script — shared NFS + tcsh + venv
# Admin runs this ONCE, then each user runs setup_user.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
DDM_ROOT="$SCRIPT_DIR"

echo "============================================"
echo "  DDM 部署脚本 (共享 venv)"
echo "  DDM_ROOT = $DDM_ROOT"
echo "============================================"
echo ""

# ---- Step 1: create shared venv ----
echo "--- Step 1: 创建共享虚拟环境 (继承系统包) ---"
if [ ! -d "$DDM_ROOT/venv" ]; then
    python3 -m venv --system-site-packages "$DDM_ROOT/venv"
    echo "  venv 已创建: $DDM_ROOT/venv"
else
    echo "  venv 已存在，跳过"
fi

source "$DDM_ROOT/venv/bin/activate"

# ---- Step 2: install missing Python deps ----
echo ""
echo "--- Step 2: Python 依赖 ---"
# Try importing each required package; only install what's missing
MISSING=""
for pkg in click rich pydantic loguru psutil blake3; do
    /usr/bin/env python3 -c "import $pkg" 2>/dev/null || MISSING="$MISSING $pkg"
done
/usr/bin/env python3 -c "import yaml" 2>/dev/null || MISSING="$MISSING PyYAML"
if [ -n "$MISSING" ]; then
    echo "  缺失: $MISSING"
    if [ -d offline_packages ] && [ "$(ls -A offline_packages 2>/dev/null)" ]; then
        echo "  从离线包安装..."
        pip install --no-index --find-links offline_packages/ -r requirements.txt 2>&1 | tail -3
    else
        echo "  在线安装..."
        pip install -r requirements.txt 2>&1 | tail -3
    fi
else
    echo "  所有依赖已从系统环境继承"
fi
    if ! python3 -c "import $pkg" 2>/dev/null; then
        MISSING="$MISSING $pkg"
    fi
done
if [ -n "$MISSING" ]; then
    echo "  缺失: $MISSING"
    if [ -d offline_packages ] && [ "$(ls -A offline_packages 2>/dev/null)" ]; then
        echo "  从离线包安装..."
        pip install --no-index --find-links offline_packages/ -r requirements.txt 2>&1 | tail -3
    else
        echo "  在线安装..."
        pip install -r requirements.txt 2>&1 | tail -3
    fi
else
    echo "  所有依赖已从系统环境继承"
fi
echo ""

# ---- Step 3: install DDM ----
echo "--- Step 3: 安装 DDM ---"
pip install -e "$DDM_ROOT" 2>&1 | tail -3
echo "  ddm 已安装到 venv"

# ---- Step 4: fix permissions (directories only, not files) ----
echo ""
echo "--- Step 4: 权限 ---"
SHARED_GRP=$(python3 -c "import yaml; c=yaml.safe_load(open('config/config.yaml')); print(c.get('shared_group','wheel'))" 2>/dev/null || echo "wheel")
echo "  共享组: $SHARED_GRP"

# Only set SGID on directories — file permissions are handled by DDM code.
# Don't chmod -R existing files (they may belong to other users).
for d in repository repository/raw repository/ready repository/release logs a0.outgoing; do
    mkdir -p "$DDM_ROOT/$d"
    chgrp "$SHARED_GRP" "$DDM_ROOT/$d" 2>/dev/null || true
    chmod 2775 "$DDM_ROOT/$d" 2>/dev/null || true
done
echo "  目录权限已设置 (2775 SGID, group=$SHARED_GRP)"

# ---- Step 5: config.yaml ----
echo ""
echo "--- Step 5: 配置 ---"
if [ ! -f config/config.yaml ]; then
    echo "  请创建 config/config.yaml (参考 docs/SETUP_TCSH.md)"
    echo "  关键: outgoing_root + repository_root 必须用绝对路径"
else
    echo "  config/config.yaml 已存在"
fi

# ---- Step 6: user setup script ----
cat > "$DDM_ROOT/setup_user.sh" << 'USER_SETUP'
#!/bin/bash
# Run ONCE per user to configure their tcsh for DDM
DDM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== DDM User Setup (venv) ==="
echo "  DDM_ROOT = $DDM_ROOT"
echo ""

if ! grep -q "DDM" ~/.cshrc 2>/dev/null; then
    cat >> ~/.cshrc << 'CSHRC'

# ===== DDM (shared venv) =====
# Clear any old DDM paths to avoid conflicts
unsetenv PYTHONPATH
source CSHRC_DDM_ROOT/venv/bin/activate.csh
alias ddm 'python3 -m ddm'
if (-f CSHRC_DDM_ROOT/ddm.complete.csh) source CSHRC_DDM_ROOT/ddm.complete.csh
CSHRC
    sed -i "s|CSHRC_DDM_ROOT|$DDM_ROOT|g" ~/.cshrc
    echo "  ~/.cshrc 已配置"
else
    echo "  ~/.cshrc 已有 DDM 配置，跳过"
fi

echo ""
echo "=== Done ==="
echo "  source ~/.cshrc  (或重新登录)"
echo "  ddm check"
USER_SETUP
chmod +x "$DDM_ROOT/setup_user.sh"

# ---- Done ----
echo ""
echo "============================================"
echo "  部署完成"
echo ""
echo "  管理员: vi $DDM_ROOT/config/config.yaml"
echo "  用户:   sh $DDM_ROOT/setup_user.sh"
echo "          source ~/.cshrc"
echo "          ddm check"
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

Docs: docs/SETUP_TCSH.md | docs/USER_GUIDE.md | docs/WORKFLOWS.md
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
