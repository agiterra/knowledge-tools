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

# Resolve the vault dir via the shared primitive (absolute KNOWLEDGE_VAULT wins —
# cwd-independent; else $CWD/.knowledge). Single source of truth for all consumers.
. "$(cd "$(dirname "$0")" && pwd)/resolve-vault.sh"

if [ ! -d "$VAULT_DIR" ]; then
    exit 0
fi

# Operate in the vault's OWN dir/repo (differs from CWD when KNOWLEDGE_VAULT is absolute).
cd "$(dirname "$VAULT_DIR")"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    exit 0
fi

# --- Step 1: journal backup ---
# Find journal.py and run the safe backup (atomic replace + verification).
# The script lives in the same directory as this one.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JOURNAL_PY="$SCRIPT_DIR/journal.py"

if [ -f "$VAULT_DIR/journal.db" ] && [ -f "$JOURNAL_PY" ]; then
    # journal.py reads $KNOWLEDGE_VAULT/journal.db; VAULT_DIR is absolute, so no cd needed.
    (KNOWLEDGE_VAULT="$VAULT_DIR" python3 "$JOURNAL_PY" backup >/dev/null 2>&1 || true)
fi

# --- Step 2 & 3: add + commit ---
# Stage the entire vault subdir. Then commit only if there are staged changes.
git add -- "$VAULT_DIR"

# ★★★ DO NOT `exit 0` HERE (2026-08-01). This used to bail out when the vault
# was unchanged — which skipped the push step below entirely. "Nothing new to
# COMMIT" is not "nothing to PUSH": any commit made outside the vault (a tool
# fix, a patch, a script) then sat unpushed indefinitely, and checkpoint exited
# 0 and printed nothing, so it read as a successful checkpoint.
#
# Observed: three code commits stayed local and only reached origin because a
# vault change happened to accompany a later run. The durability guarantee this
# script exists to provide was silently conditional on unrelated vault activity.
#
# ⇒ Fall through to the push. The commit is what is conditional, not the push.
if git diff --cached --quiet -- "$VAULT_DIR"; then
    VAULT_UNCHANGED=1
else
    VAULT_UNCHANGED=0
    if [ -z "$MSG" ]; then
        TS=$(date +%Y-%m-%d\ %H:%M)
        MSG="Checkpoint vault ($TS)"
    fi
    git commit -m "$MSG" --no-gpg-sign -- "$VAULT_DIR" >/dev/null
fi

# --- Step 4: push ---
# ⚠️ Reached even when the vault was unchanged — see the note above.
if [ "$PUSH" = "1" ]; then
    if git remote get-url origin >/dev/null 2>&1; then
        # ★★ NEVER SWALLOW THE FAILURE. This used to run `git push -q 2>/dev/null`
        # and, in the default non-strict mode, print NOTHING when the push failed:
        # stderr was discarded and the only `echo` lived behind `STRICT=1`. A push
        # that failed and a push that succeeded were byte-identical to the caller.
        # ⇒ Capture stderr and always SAY something; --strict still controls
        # whether a failure is fatal, but never whether it is REPORTED.
        if ! push_err=$(git push 2>&1); then
            echo "checkpoint: git push FAILED — the vault is committed locally but NOT on origin." >&2
            printf '%s\n' "$push_err" | sed 's/^/  /' >&2
            [ "$STRICT" = "1" ] && exit 1
        fi
    else
        # ★ Say it. An absent remote is a legitimate configuration, but silence
        # here is indistinguishable from a successful push.
        echo "checkpoint: no 'origin' remote — committed locally only, nothing pushed." >&2
    fi
fi

# ★ Report what actually happened, and ONLY what actually happened.
#
# ⚠️ The "vault committed at <HEAD>" line below used to be UNCONDITIONAL. That
# was harmless only because the old `exit 0` above made it unreachable whenever
# nothing was committed. Removing that early exit made it reachable — so it
# began announcing a commit that had not occurred, printing whatever HEAD
# happened to be. A silent-failure fix that introduces a FALSE REPORT is a worse
# trade: silence is ambiguous, a confident wrong statement is not.
# ⇒ Each branch now states only its own case, and the sha is printed only when
# this run actually created it.
unpushed=$(git rev-list --count @{u}..HEAD 2>/dev/null || echo "?")
if [ "$VAULT_UNCHANGED" = "1" ]; then
    if [ "$PUSH" = "1" ]; then
        echo "checkpoint: vault unchanged, nothing to commit (${unpushed} unpushed commit(s) remaining)"
    else
        echo "checkpoint: vault unchanged, nothing to commit (--no-push; ${unpushed} unpushed commit(s))"
    fi
else
    echo "checkpoint: vault committed at $(git rev-parse --short HEAD) (${unpushed} unpushed commit(s) remaining)"
fi
