#!/bin/bash
# Canonical knowledge-vault resolver — the ONE place shell consumers resolve the vault.
# Source this (it sets VAULT_DIR); do NOT re-derive `$CWD/$KNOWLEDGE_VAULT` inline.
#
# Inputs:  $CWD (falls back to $PWD), $KNOWLEDGE_VAULT
# Output:  VAULT_DIR (always absolute when $CWD is absolute)
#   - KNOWLEDGE_VAULT absolute  → used directly (CWD-INDEPENDENT — the whole point:
#                                 a spawned-anywhere agent can point at a fixed vault)
#   - KNOWLEDGE_VAULT relative  → $CWD/$KNOWLEDGE_VAULT   (backward-compatible)
#   - unset                     → $CWD/.knowledge          (default)
#
# Mirrors the Python (config.py) / TS (config.ts) resolvers, which already handle an
# absolute KNOWLEDGE_VAULT correctly via os.path.join / path.resolve. This brings shell
# to parity. The thin CC/Codex wrappers source THIS rather than duplicating the logic.
__kv_cwd="${CWD:-$PWD}"
VAULT_DIR="${KNOWLEDGE_VAULT:-.knowledge}"
case "$VAULT_DIR" in
  /*) : ;;                              # already absolute — use as-is
  *)  VAULT_DIR="$__kv_cwd/$VAULT_DIR" ;;
esac
unset __kv_cwd

# vault_is_real — is $VAULT_DIR a REAL vault (initialised by /knowledge:init), not merely a directory named
# .knowledge? (2026-09-25, j:1845): the pre-compact hook wrote and COMMITTED transcripts into any repo that
# happened to contain a .knowledge/ dir — one reached a PUBLIC repo. Writers and committers must require this.
vault_is_real() { [ -f "$VAULT_DIR/meta/session-state.md" ]; }
