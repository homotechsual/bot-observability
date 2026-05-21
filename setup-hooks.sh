#!/bin/bash
# Setup pre-commit hooks for bot-observability
# Run this once after cloning: ./setup-hooks.sh

set -e

HOOKS_DIR=".git/hooks"
HOOK_FILE="$HOOKS_DIR/pre-commit"

# Make sure hooks directory exists
mkdir -p "$HOOKS_DIR"

# Copy the pre-commit hook
echo "Installing pre-commit hook..."
chmod +x "$HOOK_FILE" 2>/dev/null || true

echo "✓ Pre-commit hook installed."
echo ""
echo "Next time you commit, dashboard JSON files will be automatically validated."
echo "To skip validation (not recommended): git commit --no-verify"
