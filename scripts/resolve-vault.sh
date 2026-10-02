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

# vault_is_implicit_shared_root — was $VAULT_DIR resolved FROM THE CWD (KNOWLEDGE_VAULT not absolute) into a
# project listed as a SHARED checkout? (2026-10-02, Brioche 649049): a session that forgot KNOWLEDGE_VAULT and ran
# in a box-wide shared tree exported journal.sql into it (0600, another uid) and tried to commit there; the tree is
# pull-only and its sync job discards the write. The list is HOST data, not code: one absolute project dir per line
# in ${KNOWLEDGE_SHARED_ROOTS_FILE:-/opt/agiterra/etc/knowledge-shared-roots} ('#' comments). No file → never true.
# An absolute KNOWLEDGE_VAULT is an explicit choice and is never refused, wherever it points.
vault_is_implicit_shared_root() {
  case "${KNOWLEDGE_VAULT:-}" in /*) return 1 ;; esac
  local f="${KNOWLEDGE_SHARED_ROOTS_FILE:-/opt/agiterra/etc/knowledge-shared-roots}" proj line
  [ -r "$f" ] || return 1
  proj=$(cd "$(dirname "$VAULT_DIR")" 2>/dev/null && pwd -P) || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"; line="${line%"${line##*[![:space:]]}"}"; line="${line#"${line%%[![:space:]]*}"}"
    [ -n "$line" ] || continue
    line=$(cd "$line" 2>/dev/null && pwd -P) || continue
    [ "$proj" = "$line" ] && return 0
  done < "$f"
  return 1
}
