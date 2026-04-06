#!/bin/bash
# Git pre-push hook: dump journal.db to journal.sql if changed.
# Install: cp this file to .git/hooks/pre-push && chmod +x .git/hooks/pre-push
# Or: ln -sf ../../path/to/journal-dump.sh .git/hooks/pre-push

DB="${KNOWLEDGE_VAULT:-.knowledge}/journal.db"
DUMP="${KNOWLEDGE_VAULT:-.knowledge}/journal.sql"
MARKER="${KNOWLEDGE_VAULT:-.knowledge}/.journal-last-dump"

# Skip if no journal database exists
[ -f "$DB" ] || exit 0

# Check if db has changed since last dump (compare mtime)
if [ -f "$MARKER" ]; then
    db_mtime=$(stat -f %m "$DB" 2>/dev/null || stat -c %Y "$DB" 2>/dev/null)
    last_dump=$(cat "$MARKER" 2>/dev/null)
    if [ "$db_mtime" = "$last_dump" ]; then
        exit 0  # No changes since last dump
    fi
fi

# Dump and stage
sqlite3 "$DB" .dump > "$DUMP"
git add "$DUMP"

# Record current mtime
db_mtime=$(stat -f %m "$DB" 2>/dev/null || stat -c %Y "$DB" 2>/dev/null)
echo "$db_mtime" > "$MARKER"

echo "journal: dumped $DB → $DUMP"
