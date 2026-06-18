#!/bin/bash
# precompact-backup.sh — Knowledge vault PreCompact hook
#
# Runs before context compaction. Reads hook input from stdin (JSON),
# backs up the transcript, and extracts session state markers.
#
# Input (stdin JSON):
#   session_id, transcript_path, cwd, trigger (manual|auto)
#
# Actions:
#   1. Back up the raw transcript JSONL
#   2. Extract the last assistant turn's text as a recovery hint
#   3. Write a timestamped recovery file for the boot sequence

set -euo pipefail

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
TRIGGER=$(echo "$INPUT" | jq -r '.trigger // "unknown"')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

# Validate
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    echo "precompact-backup: no transcript at '$TRANSCRIPT'" >&2
    exit 0  # Don't fail — compaction should proceed
fi

# Determine knowledge vault directory (shared primitive — absolute KNOWLEDGE_VAULT wins)
. "$(cd "$(dirname "$0")" && pwd)/resolve-vault.sh"
MEMORY_DIR="$VAULT_DIR"
if [ ! -d "$MEMORY_DIR" ]; then
    # Not a knowledge-vault-managed project — skip silently
    exit 0
fi

BACKUP_DIR="$MEMORY_DIR/meta/precompact"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# 1. Back up transcript (keep last 5)
cp "$TRANSCRIPT" "$BACKUP_DIR/transcript-${TIMESTAMP}.jsonl"
ls -t "$BACKUP_DIR"/transcript-*.jsonl 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true

# 2. Extract recovery hints from the transcript
# Pull the last few assistant messages as context for what was happening
RECOVERY_FILE="$BACKUP_DIR/recovery-${TIMESTAMP}.md"
cat > "$RECOVERY_FILE" << HEADER
# Pre-Compaction Recovery — $TIMESTAMP
Trigger: $TRIGGER | Session: $SESSION_ID

## Last Activity
HEADER

# Extract last 3 assistant message texts from the JSONL
# Each line is a JSON object; filter for assistant role, grab content text
if command -v python3 >/dev/null 2>&1; then
    python3 -c "
import json, sys
msgs = []
for line in open('$TRANSCRIPT'):
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        msg = obj.get('message', obj)
        if msg.get('role') == 'assistant':
            content = msg.get('content', [])
            if isinstance(content, list):
                texts = [b.get('text','') for b in content if b.get('type')=='text']
                if texts:
                    msgs.append(' '.join(texts)[:500])
            elif isinstance(content, str):
                msgs.append(content[:500])
    except (json.JSONDecodeError, AttributeError):
        pass
# Last 3 assistant messages
for i, m in enumerate(msgs[-3:], 1):
    print(f'### Message {i}')
    print(m)
    print()
" >> "$RECOVERY_FILE" 2>/dev/null || echo "(transcript parsing failed)" >> "$RECOVERY_FILE"
else
    echo "(python3 not available for transcript parsing)" >> "$RECOVERY_FILE"
fi

# 3. Write a pointer for the boot sequence
cat > "$BACKUP_DIR/latest-recovery.md" << EOF
# Latest Pre-Compaction Recovery
File: recovery-${TIMESTAMP}.md
Transcript: transcript-${TIMESTAMP}.jsonl
Trigger: $TRIGGER
Timestamp: $TIMESTAMP
EOF

echo "precompact-backup: saved recovery data to $BACKUP_DIR"
