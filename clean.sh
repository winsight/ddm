#!/bin/bash
# Clean runtime data while preserving directory structure
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR/repository"

echo "Cleaning runtime data..."

# Remove raw data (including hidden lock files)
if [ -d "$REPO_DIR/raw" ]; then
    rm -rf "$REPO_DIR/raw"/* "$REPO_DIR/raw"/.lock_* 2>/dev/null || true
    echo "  cleaned raw/"
fi

# Remove ready data (including hidden lock files)
if [ -d "$REPO_DIR/ready" ]; then
    rm -rf "$REPO_DIR/ready"/* "$REPO_DIR/ready"/.lock_* 2>/dev/null || true
    echo "  cleaned ready/"
fi

# Remove release data
if [ -d "$REPO_DIR/release" ]; then
    rm -rf "$REPO_DIR/release"/*
    echo "  cleaned release/"
fi

# Remove SQLite database
if [ -f "$REPO_DIR/ddm.db" ]; then
    rm -f "$REPO_DIR/ddm.db"
    echo "  removed ddm.db"
fi

# Remove logs
if [ -d "$SCRIPT_DIR/logs" ]; then
    rm -rf "$SCRIPT_DIR/logs"/*
    echo "  cleaned logs/"
fi

echo "Done. Runtime data cleared."
