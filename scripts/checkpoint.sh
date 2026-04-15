#!/bin/bash
# checkpoint.sh — Canonical vault persistence pipeline.
#
# One script, one job: make the vault durable.
#   1. Back up journal.db to journal.sql (via journal.py backup).
#   2. git add the vault directory.
#   3. git commit if there are changes.
#   4. git push if origin is set and --no-push wasn't passed.
#
# Idempotent and safe to run from anywhere:
#   - noop if the vault directory doesn't exist
#   - noop if the cwd isn't inside a git repo
#   - noop if there are no changes to commit
#   - tolerates missing remote (push failure is non-fatal unless --strict)
#
# Usage:
#   checkpoint.sh [--cwd DIR] [--message MSG] [--no-push] [--strict]
#
# The cwd is where the vault lives (PWD by default). The vault directory is
# ${KNOWLEDGE_VAULT:-.knowledge} relative to cwd. The git repo is auto-detected
# from cwd — it might be the vault itself (vault-as-repo) or a parent project
# repo that contains the vault as a subdirectory.

set -eu

CWD="$PWD"
MSG=""
PUSH=1
STRICT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --cwd) CWD="$2"; shift 2 ;;
        --message|-m) MSG="$2"; shift 2 ;;
        --no-push) PUSH=0; shift ;;
        --strict) STRICT=1; shift ;;
        *) echo "checkpoint: unknown arg: $1" >&2; exit 2 ;;
    esac
done

VAULT_NAME="${KNOWLEDGE_VAULT:-.knowledge}"
VAULT_DIR="$CWD/$VAULT_NAME"

if [ ! -d "$VAULT_DIR" ]; then
    exit 0
fi

cd "$CWD"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    exit 0
fi

# --- Step 1: journal backup ---
# Find journal.py and run the safe backup (atomic replace + verification).
# The script lives in the same directory as this one.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JOURNAL_PY="$SCRIPT_DIR/journal.py"

if [ -f "$VAULT_DIR/journal.db" ] && [ -f "$JOURNAL_PY" ]; then
    # journal.py backup operates from the vault's parent dir (it reads
    # ${KNOWLEDGE_VAULT:-.knowledge}/journal.db relative to cwd).
    (cd "$CWD" && KNOWLEDGE_VAULT="$VAULT_NAME" python3 "$JOURNAL_PY" backup >/dev/null 2>&1 || true)
fi

# --- Step 2 & 3: add + commit ---
# Stage the entire vault subdir. Then commit only if there are staged changes.
git add -- "$VAULT_DIR"

if git diff --cached --quiet -- "$VAULT_DIR"; then
    # Nothing changed under the vault — bail out cleanly.
    exit 0
fi

if [ -z "$MSG" ]; then
    TS=$(date +%Y-%m-%d\ %H:%M)
    MSG="Checkpoint vault ($TS)"
fi

git commit -m "$MSG" --no-gpg-sign -- "$VAULT_DIR" >/dev/null

# --- Step 4: push ---
if [ "$PUSH" = "1" ]; then
    if git remote get-url origin >/dev/null 2>&1; then
        if ! git push -q 2>/dev/null; then
            if [ "$STRICT" = "1" ]; then
                echo "checkpoint: git push failed" >&2
                exit 1
            fi
        fi
    fi
fi

echo "checkpoint: vault committed at $(git rev-parse --short HEAD)"
