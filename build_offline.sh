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

# Also download build deps (setuptools, wheel) needed for pip install -e
pip3 download setuptools wheel -d dist/offline_packages/ 2>&1 | grep -v "^Requirement already" || true

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
# Ship config as .example so deployment never overwrites the user's config.yaml
mv "$PACKAGE_DIR/config/config.yaml" "$PACKAGE_DIR/config/config.yaml.example" 2>/dev/null || true
cp clean.sh ddm.complete.csh ddm_update.csh ddm_web.py setup.py sync_owners.py "$PACKAGE_DIR/"
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

# When called by ddm_update.csh, DDM_LINK is the stable symlink path (e.g. ~/ddm).
# setup_user.sh will embed this stable path so users don't need to re-run it
# after every update — only the symlink target changes.
STABLE_ROOT="${DDM_LINK:-$DDM_ROOT}"
# Ensure it is always absolute — "source <path>" in .cshrc won't work
# from arbitrary directories otherwise.
case "$STABLE_ROOT" in
    /*) ;;                              # already absolute
    *) STABLE_ROOT="$PWD/$STABLE_ROOT" ;; # make absolute
esac

echo "============================================"
echo "  DDM 部署脚本 (共享 venv)"
echo "  DDM_ROOT    = $DDM_ROOT"
if [ -n "$DDM_LINK" ]; then
    echo "  stable link = $DDM_LINK"
fi
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
echo ""

# ---- Step 3: ensure build tools (setuptools) are available ----
echo "--- Step 3: 构建工具 ---"
if [ -d offline_packages ] && [ "$(ls -A offline_packages 2>/dev/null)" ]; then
    pip install --no-index --find-links offline_packages/ setuptools wheel 2>&1 | tail -1
else
    pip install setuptools wheel 2>&1 | tail -1
fi

# ---- Step 4: install DDM ----
echo "--- Step 4: 安装 DDM ---"
pip install -e "$DDM_ROOT" 2>&1 | tail -3
echo "  ddm 已安装到 venv"

# ---- Step 5: fix permissions (directories only, not files) ----
echo ""
echo "--- Step 4: 权限 ---"
SHARED_GRP=$(python3 -c "import yaml; c=yaml.safe_load(open('config/config.yaml')); print(c.get('shared_group','wheel'))" 2>/dev/null || echo "wheel")
echo "  共享组: $SHARED_GRP"

# Only set SGID on directories — file permissions are handled by DDM code.
# Don't chmod -R existing files (they may belong to other users).
for d in repository repository/raw repository/ready repository/release logs; do
    mkdir -p "$DDM_ROOT/$d"
    chgrp "$SHARED_GRP" "$DDM_ROOT/$d" 2>/dev/null || true
    chmod 2775 "$DDM_ROOT/$d" 2>/dev/null || true
done
echo "  目录权限已设置 (2775 SGID, group=$SHARED_GRP)"

# ---- Step 5: config.yaml ----
echo ""
echo "--- Step 5: 配置 ---"
if [ ! -f config/config.yaml ]; then
    if [ -f config/config.yaml.example ]; then
        cp config/config.yaml.example config/config.yaml
        echo "  config/config.yaml 已从 .example 创建，请根据环境修改路径"
        echo "  关键: outgoing_root + repository_root + shared_group"
    else
        echo "  请创建 config/config.yaml (参考 docs/SETUP_TCSH.md)"
        echo "  关键: outgoing_root + repository_root 必须用绝对路径"
    fi
else
    echo "  config/config.yaml 已存在，保留当前配置"
fi

# ---- Step 6: user setup script ----
cat > "$DDM_ROOT/setup_user.sh" << 'USER_SETUP'
#!/bin/bash
# Run ONCE per user to configure their tcsh for DDM
# DDM_ROOT is embedded at install time — uses the stable symlink path
# when deployed via ddm_update.csh, so users don't need to re-run setup
# after every update.
DDM_ROOT="__STABLE_ROOT__"

echo "=== DDM User Setup (venv) ==="
echo "  DDM_ROOT = $DDM_ROOT"
echo ""

CURRENT_DDM=$(grep "DDM" ~/.cshrc 2>/dev/null | grep "$DDM_ROOT" || true)
if [ -z "$CURRENT_DDM" ] && grep -q "DDM" ~/.cshrc 2>/dev/null; then
    echo "  ~/.cshrc has an OLD DDM path (different from current)."
    echo "  Old entries:"
    grep "DDM" ~/.cshrc | sed 's/^/    /'
    echo ""
    echo "  Remove old block and re-add? [y/N] "
    read answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
        # Remove old DDM block (lines between "# ===== DDM" and next blank line)
        perl -i -ne 'if (/^# ===== DDM/){$skip=1; next} if($skip && /^$/){$skip=0; next} print unless $skip' ~/.cshrc
        grep -q "DDM" ~/.cshrc 2>/dev/null || true  # refresh
    fi
fi

if ! grep -q "DDM" ~/.cshrc 2>/dev/null; then
    cat >> ~/.cshrc << 'CSHRC'

# ===== DDM (shared venv) =====
# Uses the venv Python directly — no "source activate" needed.
# This keeps your shell environment (PATH, prompt) untouched.
alias ddm 'CSHRC_DDM_ROOT/venv/bin/python3 -m ddm'
#if (-f CSHRC_DDM_ROOT/ddm.complete.csh) source CSHRC_DDM_ROOT/ddm.complete.csh
CSHRC
    sed -i "s|CSHRC_DDM_ROOT|$DDM_ROOT|g" ~/.cshrc
    echo "  ~/.cshrc 已配置 (stable path: $DDM_ROOT)"
elif grep -q "$DDM_ROOT" ~/.cshrc 2>/dev/null; then
    echo "  ~/.cshrc already points to current DDM — nothing to do."
else
    echo "  ~/.cshrc has DDM config but it was not updated (manual check needed)."
fi

echo ""
echo "=== Done ==="
echo "  source ~/.cshrc  (或重新登录)"
echo "  ddm check"
USER_SETUP
sed -i "s|__STABLE_ROOT__|$STABLE_ROOT|g" "$DDM_ROOT/setup_user.sh"
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
