#!/usr/bin/env python3
"""Journal system for agent memory — the log of WHY beliefs exist.

An append-only SQLite database that records the context behind changes
to an agent's identity, values, and knowledge. Each entry captures not
just what was learned, but why — the reasoning chain, what was considered,
what was rejected.

Core memory (identity.md) references journal entries via `[j:N]` notation,
creating a provenance chain from belief to experience.

The journal.db binary is .gitignored. A text SQL dump (journal.sql) lives
in git, updated by a pre-push hook. On fresh clone or pull, rebuild the
db from the dump.

The vault directory defaults to .knowledge/ and can be overridden with
--vault <path> or the KNOWLEDGE_VAULT environment variable.

Usage:
  python3 scripts/journal.py init
  python3 scripts/journal.py add <category> <summary> <context> [--source X] [--tags a,b] [--refs 1,2]
  python3 scripts/journal.py get <id>
  python3 scripts/journal.py search <query>
  python3 scripts/journal.py recent [N]
  python3 scripts/journal.py by-category <category>
  python3 scripts/journal.py by-tag <tag>
  python3 scripts/journal.py refs <id>
  python3 scripts/journal.py backup
  python3 scripts/journal.py dump
  python3 scripts/journal.py rebuild [sql-file] [--force]
  python3 scripts/journal.py migrate
  python3 scripts/journal.py stats
"""

import os
import shutil
import re
import sqlite3
import sys
from datetime import datetime, timezone

import config


def _db_path():
    """Return path to journal.db inside the active vault directory."""
    return os.path.join(config.vault_dir(), 'journal.db')


def _dump_path():
    """Return path to journal.sql inside the active vault directory."""
    return os.path.join(config.vault_dir(), 'journal.sql')


SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    category TEXT,
    summary TEXT NOT NULL,
    context TEXT NOT NULL,
    source TEXT,
    tags TEXT,
    refs TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    summary, context, tags,
    content='journal',
    content_rowid='id'
);

-- Triggers to keep FTS in sync with journal table
CREATE TRIGGER IF NOT EXISTS journal_ai AFTER INSERT ON journal BEGIN
    INSERT INTO journal_fts(rowid, summary, context, tags)
    VALUES (new.id, new.summary, new.context, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS journal_ad AFTER DELETE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, summary, context, tags)
    VALUES ('delete', old.id, old.summary, old.context, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS journal_au AFTER UPDATE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, summary, context, tags)
    VALUES ('delete', old.id, old.summary, old.context, old.tags);
    INSERT INTO journal_fts(rowid, summary, context, tags)
    VALUES (new.id, new.summary, new.context, new.tags);
END;
"""


def get_db():
    """Open or create the journal database."""
    db = _db_path()
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def format_entry(row):
    """Format a journal entry for display."""
    lines = [f'j:{row["id"]}  [{row["timestamp"][:10]}]  {row["category"] or "—"}']
    lines.append(f'  {row["summary"]}')
    if row['source']:
        lines.append(f'  source: {row["source"]}')
    if row['tags']:
        lines.append(f'  tags: {row["tags"]}')
    if row['refs']:
        lines.append(f'  refs: {row["refs"]}')
    lines.append(f'  ---')
    # Indent context, truncate for display
    ctx = row['context']
    if len(ctx) > 500:
        ctx = ctx[:497] + '...'
    for line in ctx.split('\n'):
        lines.append(f'  {line}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init():
    """Initialize the journal database with schema."""
    db = _db_path()
    if os.path.exists(db):
        print(f'Journal already exists at {db}')
        conn = get_db()
        # Ensure schema is current
        conn.executescript(SCHEMA)
        conn.close()
        print('Schema verified.')
        return

    conn = get_db()
    conn.executescript(SCHEMA)
    conn.close()
    print(f'Journal initialized at {db}')


def cmd_migrate():
    """Migrate a legacy journal.db (id, created_at, category, body) to the current schema."""
    db = _db_path()
    if not os.path.exists(db):
        print(f'No journal at {db} — nothing to migrate.')
        sys.exit(1)
    conn = get_db()
    cols = {r[1] for r in conn.execute('PRAGMA table_info(journal)').fetchall()}
    if {'timestamp', 'summary', 'context'} <= cols:
        print('Schema is already current — nothing to migrate.')
        conn.close()
        return
    if not {'id', 'created_at', 'category', 'body'} <= cols:
        print(f'Unrecognized journal schema (columns: {sorted(cols)}) — refusing to guess. Migrate manually.')
        conn.close()
        sys.exit(1)
    rows = conn.execute('SELECT id, created_at, category, body FROM journal ORDER BY id').fetchall()
    conn.close()

    backup = db + '.bak-migrate-' + datetime.now().strftime('%Y%m%d-%H%M%S')
    shutil.copy2(db, backup)
    print(f'Backup: {backup} ({len(rows)} legacy entries)')

    conn = get_db()
    conn.executescript(
        'DROP TRIGGER IF EXISTS journal_ai;'
        'DROP TRIGGER IF EXISTS journal_ad;'
        'DROP TRIGGER IF EXISTS journal_au;'
        'DROP TABLE IF EXISTS journal_fts;'
        'ALTER TABLE journal RENAME TO journal_legacy;'
    )
    conn.executescript(SCHEMA)
    for r in rows:
        body = (r['body'] or '').strip()
        first = body.split('\n', 1)[0].strip()
        summary = first if len(first) <= 120 else first[:117] + '...'
        ts = '' if r['created_at'] is None else str(r['created_at'])
        if ts.isdigit():  # epoch seconds or milliseconds
            secs = int(ts) / 1000 if len(ts) >= 13 else int(ts)
            ts = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
        conn.execute(
            'INSERT INTO journal (id, timestamp, category, summary, context) VALUES (?, ?, ?, ?, ?)',
            (r['id'], ts, r['category'], summary or '(no summary)', body or '(empty)'),
        )
    conn.commit()
    n_new = conn.execute('SELECT count(*) FROM journal').fetchone()[0]
    n_fts = conn.execute('SELECT count(*) FROM journal_fts').fetchone()[0]
    if n_new != len(rows):
        conn.close()
        print(f'MIGRATION MISMATCH: {len(rows)} legacy rows but {n_new} migrated — journal_legacy table kept, db NOT finalized. Restore from {backup} or investigate.')
        sys.exit(1)
    conn.execute('DROP TABLE journal_legacy')
    conn.commit()
    conn.close()
    print(f'Migrated {n_new} entries (FTS rows: {n_fts}). body → context, summary = first line. Backup kept at {backup}')


def cmd_add(category, summary, context, source=None, tags=None, refs=None):
    """Add a new journal entry. Returns the new entry ID."""
    conn = get_db()
    ts = datetime.now(timezone.utc).isoformat()

    # Normalize tags and refs
    tags_str = ','.join(t.strip() for t in tags.split(',')) if tags else None
    refs_str = refs if refs else None

    cursor = conn.execute(
        'INSERT INTO journal (timestamp, category, summary, context, source, tags, refs) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (ts, category, summary, context, source, tags_str, refs_str)
    )
    entry_id = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f'j:{entry_id}  [{ts[:10]}]  {category}')
    print(f'  {summary}')
    return entry_id


def cmd_get(entry_id):
    """Retrieve a single journal entry by ID."""
    conn = get_db()
    row = conn.execute('SELECT * FROM journal WHERE id = ?', (entry_id,)).fetchone()
    conn.close()

    if not row:
        print(f'No entry with id {entry_id}')
        return None

    print(format_entry(row))
    return row


def cmd_search(query):
    """Full-text search across summary, context, and tags."""
    conn = get_db()
    # FTS5 query — quote terms for safety
    fts_query = ' OR '.join(f'"{term}"' for term in query.split())
    try:
        rows = conn.execute(
            'SELECT j.*, rank FROM journal_fts fts '
            'JOIN journal j ON j.id = fts.rowid '
            'WHERE journal_fts MATCH ? '
            'ORDER BY rank '
            'LIMIT 20',
            (fts_query,)
        ).fetchall()
    except sqlite3.OperationalError:
        # Fallback to LIKE search if FTS fails
        like_pattern = f'%{query}%'
        rows = conn.execute(
            'SELECT *, 0 as rank FROM journal '
            'WHERE summary LIKE ? OR context LIKE ? OR tags LIKE ? '
            'ORDER BY timestamp DESC LIMIT 20',
            (like_pattern, like_pattern, like_pattern)
        ).fetchall()

    conn.close()

    if not rows:
        print(f'No matches for: {query}')
        return []

    print(f'Journal search: {query}  ({len(rows)} results)\n')
    for row in rows:
        print(format_entry(row))
        print()

    return rows


def cmd_recent(n=10):
    """Show the N most recent journal entries."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM journal ORDER BY id DESC LIMIT ?', (n,)
    ).fetchall()
    conn.close()

    if not rows:
        print('Journal is empty.')
        return []

    print(f'Last {len(rows)} entries:\n')
    for row in rows:
        print(format_entry(row))
        print()

    return rows


def cmd_by_category(category):
    """List entries by category."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM journal WHERE category = ? ORDER BY id DESC',
        (category,)
    ).fetchall()
    conn.close()

    if not rows:
        print(f'No entries with category: {category}')
        return []

    print(f'Category: {category}  ({len(rows)} entries)\n')
    for row in rows:
        print(format_entry(row))
        print()

    return rows


def cmd_by_tag(tag):
    """List entries containing a specific tag."""
    conn = get_db()
    like_pattern = f'%{tag}%'
    rows = conn.execute(
        'SELECT * FROM journal WHERE tags LIKE ? ORDER BY id DESC',
        (like_pattern,)
    ).fetchall()
    conn.close()

    # Filter for exact tag match within comma-separated list
    filtered = []
    for row in rows:
        entry_tags = [t.strip().lower() for t in (row['tags'] or '').split(',')]
        if tag.lower() in entry_tags:
            filtered.append(row)

    if not filtered:
        print(f'No entries with tag: {tag}')
        return []

    print(f'Tag: {tag}  ({len(filtered)} entries)\n')
    for row in filtered:
        print(format_entry(row))
        print()

    return filtered


def cmd_refs(entry_id):
    """Find all entries that reference a given entry ID."""
    conn = get_db()
    # Search for references like "1" or "1," or ",1" in the refs field
    rows = conn.execute(
        'SELECT * FROM journal WHERE refs LIKE ? OR refs LIKE ? OR refs LIKE ? OR refs = ? '
        'ORDER BY id DESC',
        (f'{entry_id},%', f'%,{entry_id},%', f'%,{entry_id}', str(entry_id))
    ).fetchall()
    conn.close()

    if not rows:
        print(f'No entries reference j:{entry_id}')
        return []

    print(f'Entries referencing j:{entry_id}  ({len(rows)} results)\n')
    for row in rows:
        print(format_entry(row))
        print()

    return rows


def cmd_dump():
    """Dump the journal to SQL text format."""
    db = _db_path()
    dump_path = _dump_path()
    if not os.path.exists(db):
        print('No journal database found.')
        return

    conn = sqlite3.connect(db)
    dump = '\n'.join(conn.iterdump())
    conn.close()

    with open(dump_path, 'w') as f:
        f.write(dump)
        f.write('\n')

    print(f'Dumped to {dump_path}')


def cmd_backup():
    """Safe backup: dump to temp file, verify, then atomically replace.

    This is the ONLY safe way to update journal.sql. Never use shell
    redirects with dump — cmd_dump() writes to the file directly AND
    prints to stdout, so '> journal.sql' overwrites the good dump with
    the status line.
    """
    import shutil
    import tempfile

    db = _db_path()
    dump_path = _dump_path()

    if not os.path.exists(db):
        print('No journal database found.')
        return False

    # 1. Count entries in live DB
    conn = get_db()
    db_count = conn.execute('SELECT COUNT(*) FROM journal').fetchone()[0]
    if db_count == 0:
        print('Journal is empty — nothing to back up.')
        conn.close()
        return False

    # 2. Dump SQL to temp file.
    #    iterdump() emits the FTS5 shadow tables (journal_fts_data/idx/content/
    #    docsize/config) as literal CREATE TABLEs. On reload, CREATE VIRTUAL TABLE
    #    creates them itself, so the explicit ones collide with
    #    "object name reserved for internal use" and the whole script aborts.
    #    They are DERIVED state: drop them and rebuild the index on load instead.
    #    Filter whole STATEMENTS (iterdump yields one per item) and anchor on the
    #    statement's TARGET -- a substring test also matches journal rows whose
    #    text merely mentions a shadow table, silently dropping those entries.
    _SHADOW = re.compile(
        r'''^\s*(?:CREATE\s+TABLE|INSERT\s+INTO)\s+["'`\[]?'''
        r'''journal_fts_(?:data|idx|content|docsize|config)\b''',
        re.I,
    )
    raw_conn = sqlite3.connect(db)
    dump = '\n'.join(
        stmt for stmt in raw_conn.iterdump() if not _SHADOW.match(stmt)
    )
    dump += "\nINSERT INTO journal_fts(journal_fts) VALUES('rebuild');"
    raw_conn.close()
    conn.close()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.sql', prefix='journal-backup-')
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            f.write(dump)
            f.write('\n')

        # 3. Verify: count INSERT statements in the dump
        insert_count = dump.count('INSERT INTO "journal"')
        if insert_count < db_count:
            print(f'VERIFICATION FAILED: DB has {db_count} entries but dump has {insert_count} INSERTs.')
            print(f'Temp file preserved at {tmp_path} for inspection.')
            return False

        # 4. Verify by reloading THE ACTUAL DUMP -- unmodified.
        #    The previous implementation filtered the dump line-by-line before
        #    verifying, which (a) tested a different artifact than the one being
        #    written and (b) shredded multi-line CREATE TRIGGER bodies into a
        #    syntax error. It then caught that error and fell back to counting
        #    INSERT statements while still printing "(verified)" -- a check that
        #    could not run, silently downgraded to a weaker one, reporting green.
        #    A backup that has not been restored is not a backup: fail loudly.
        try:
            verify_conn = sqlite3.connect(':memory:')
            verify_conn.executescript(dump)
            verify_count = verify_conn.execute('SELECT COUNT(*) FROM journal').fetchone()[0]
            verify_conn.close()
        except sqlite3.Error as e:
            print(f'VERIFICATION FAILED: dump does not reload ({e}).')
            print(f'Temp file preserved at {tmp_path} for inspection.')
            return False

        if verify_count != db_count:
            print(f'VERIFICATION FAILED: DB has {db_count} entries but reloaded dump has {verify_count}.')
            print(f'Temp file preserved at {tmp_path} for inspection.')
            return False

        # 5. Atomically replace dump_path
        os.makedirs(os.path.dirname(dump_path), exist_ok=True)
        shutil.move(tmp_path, dump_path)
        print(f'Backed up {db_count} entries to {dump_path} (verified)')
        return True

    except Exception as e:
        print(f'Backup failed: {e}')
        if os.path.exists(tmp_path):
            print(f'Temp file preserved at {tmp_path} for inspection.')
        return False


def cmd_rebuild(sql_file=None, force=False):
    """Rebuild journal.db from a SQL dump file.

    Safety: validates the SQL file contains entries before deleting the
    existing DB. Backs up the existing DB before replacement.
    """
    import shutil

    db = _db_path()
    sql_file = sql_file or _dump_path()

    if not os.path.exists(sql_file):
        print(f'SQL dump not found: {sql_file}')
        return False

    # Read and validate SQL before touching the DB
    with open(sql_file, 'r') as f:
        sql = f.read()

    #    Count BOTH quoting forms. This guard is load-bearing -- it is what stops
    #    a 0-entry dump from replacing a populated journal -- and substring-matching
    #    a single dialect means it silently stops applying if anything ever writes
    #    the dump by another route. A guard whose firing depends on a quoting
    #    convention is not a guard.
    insert_count = len(re.findall(
        r'INSERT\s+INTO\s+["\'`\[]?journal["\'`\]]?\s*(?:\(|VALUES)', sql, re.I))
    if insert_count == 0 and os.path.exists(db) and not force:
        db_count = sqlite3.connect(db).execute('SELECT COUNT(*) FROM journal').fetchone()[0]
        if db_count > 0:
            print(f'REFUSED: SQL file has 0 INSERTs but existing DB has {db_count} entries.')
            print(f'This would destroy data. Run "backup" first, or pass --force to override.')
            return False

    # Build into a TEMP path and swap only after the result verifies.
    #
    # The previous implementation deleted the live DB first and called
    # executescript() OUTSIDE any try -- so a malformed dump killed the process
    # with a traceback having already removed the journal, and the restore
    # handler was never reached. Restoring from .bak is RECOVERY; not deleting
    # is PREVENTION. A recovery path that the next retry can clobber is one
    # incident away from being no mechanism at all. (Shape credit: Fondant.)
    #
    # With temp-then-swap, a bad dump leaves the live journal untouched and
    # there is no recovery path that has to be correct.
    tmp_db = db + '.rebuild.tmp'
    if os.path.exists(tmp_db):
        os.remove(tmp_db)

    try:
        conn = sqlite3.connect(tmp_db)
        conn.executescript(sql)
        conn.close()
        count = sqlite3.connect(tmp_db).execute(
            'SELECT COUNT(*) FROM journal').fetchone()[0]
    except sqlite3.Error as e:
        print(f'ERROR: rebuild failed ({e}). SQL file may be invalid.')
        print(f'Live journal at {db} is UNTOUCHED.')
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
        return False

    # Refuse a REGRESSION, not merely an empty result. A dump that is perfectly
    # valid but truncated silently replaces the journal with a fraction of
    # itself -- worse than the crash, because nothing errors. Checking only
    # `count == 0` catches total loss and misses partial loss entirely.
    #    AN ABSENT COUNT IS NOT A ZERO. Coercing an unreadable live DB to 0 makes
    #    `count < live_count` unsatisfiable and silently disables this guard --
    #    in exactly the situation that makes anyone run rebuild at all. Nobody
    #    rebuilds a healthy journal; they rebuild when the live DB is already in
    #    trouble, which is precisely the corrupt/locked/permission-denied case.
    #    A file that is PRESENT AND UNREADABLE is more alarming than one that is
    #    absent, so it must refuse rather than proceed. (Caught by Fondant.)
    if os.path.exists(db) and not force:
        try:
            live_count = sqlite3.connect(db).execute(
                'SELECT COUNT(*) FROM journal').fetchone()[0]
        except sqlite3.Error as e:
            print(f'REFUSED: live journal at {db} exists but is unreadable ({e}), '
                  f'so it cannot be compared against the {count} entries in the dump.')
            print('Live journal is UNTOUCHED. Pass --force to replace it anyway.')
            os.remove(tmp_db)
            return False
        if count < live_count:
            print(f'REFUSED: rebuilt DB has {count} entries but live journal has '
                  f'{live_count}. Live journal at {db} is UNTOUCHED.')
            print('Pass --force if you intend to restore an older snapshot.')
            os.remove(tmp_db)
            return False

    # Keep a .bak as a convenience, but it is no longer the thing standing
    # between a bad dump and data loss.
    if os.path.exists(db):
        shutil.copy2(db, db + '.bak')

    os.replace(tmp_db, db)          # atomic within a filesystem
    print(f'Rebuilt {db} from {sql_file} ({count} entries)')
    return True


def cmd_stats():
    """Print journal statistics."""
    db = _db_path()
    if not os.path.exists(db):
        print('No journal database found. Run: journal.py init')
        return

    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM journal').fetchone()[0]

    if total == 0:
        print('Journal is empty.')
        conn.close()
        return

    categories = conn.execute(
        'SELECT category, COUNT(*) as cnt FROM journal GROUP BY category ORDER BY cnt DESC'
    ).fetchall()

    first = conn.execute('SELECT timestamp FROM journal ORDER BY id ASC LIMIT 1').fetchone()
    last = conn.execute('SELECT timestamp FROM journal ORDER BY id DESC LIMIT 1').fetchone()

    # Tag frequency
    all_tags = {}
    rows = conn.execute('SELECT tags FROM journal WHERE tags IS NOT NULL').fetchall()
    for row in rows:
        for tag in row['tags'].split(','):
            tag = tag.strip().lower()
            if tag:
                all_tags[tag] = all_tags.get(tag, 0) + 1

    conn.close()

    print(f'Journal entries:  {total}')
    print(f'Date range:       {first[0][:10]} to {last[0][:10]}')
    print(f'Categories:')
    for cat in categories:
        print(f'  {cat["category"] or "(none)":20s}  {cat["cnt"]}')
    if all_tags:
        top_tags = sorted(all_tags.items(), key=lambda x: -x[1])[:15]
        print(f'Top tags:')
        for tag, count in top_tags:
            print(f'  {tag:20s}  {count}')


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

USAGE = """\
Usage: journal.py <command> [args] [--vault <path>]

Commands:
  init                                      Create journal database
  add <cat> <summary> <context> [options]   Add entry (--source X --tags a,b --refs 1,2)
  get <id>                                  Show entry by ID
  search <query>                            Full-text search
  recent [N]                                Last N entries (default 10)
  by-category <category>                    Filter by category
  by-tag <tag>                              Filter by tag
  refs <id>                                 Find entries referencing ID
  backup                                    Safe dump: verify then atomically replace journal.sql
  dump                                      Export to journal.sql (prefer 'backup' for safety)
  rebuild [sql-file]                        Rebuild db from SQL dump (validates before deletion)
  migrate                                   Migrate legacy schema (created_at/body) to current; backs up first
  stats                                     Show statistics

Categories: learning, correction, decision, experiment, conversation

Options:
  --vault <path>                            Override vault directory (default: .knowledge/)
"""

if __name__ == '__main__':
    # Parse --vault early, before command dispatch
    argv = list(sys.argv[1:])
    if '--vault' in argv:
        idx = argv.index('--vault')
        if idx + 1 < len(argv):
            config.set_vault_dir(argv[idx + 1])
            del argv[idx:idx + 2]
        else:
            print('--vault requires a path argument')
            sys.exit(1)

    if len(argv) < 1:
        print(USAGE)
        sys.exit(1)

    cmd = argv[0]

    if cmd == 'init':
        cmd_init()

    elif cmd == 'add':
        if len(argv) < 4:
            print('Usage: journal.py add <category> <summary> <context> [--source X] [--tags a,b] [--refs 1,2]')
            sys.exit(1)
        category = argv[1]
        summary = argv[2]
        context = argv[3]
        # <category> <summary> <context> are positional; a flag landing in one
        # of those slots means the caller used flag syntax — the flag strings
        # would be stored literally and the real values dropped (silent data loss).
        if any(v.startswith('--') for v in (category, summary, context)):
            print('journal.py add: <category> <summary> <context> are positional; flags come after them')
            print('Usage: journal.py add <category> <summary> <context> [--source X] [--tags a,b] [--refs 1,2]')
            sys.exit(1)
        # Parse optional flags
        source = tags = refs = None
        i = 4
        while i < len(argv):
            if argv[i] == '--source' and i + 1 < len(argv):
                source = argv[i + 1]
                i += 2
            elif argv[i] == '--tags' and i + 1 < len(argv):
                tags = argv[i + 1]
                i += 2
            elif argv[i] == '--refs' and i + 1 < len(argv):
                refs = argv[i + 1]
                i += 2
            else:
                print(f'journal.py add: unrecognized argument: {argv[i]}')
                print('Usage: journal.py add <category> <summary> <context> [--source X] [--tags a,b] [--refs 1,2]')
                sys.exit(1)
        cmd_add(category, summary, context, source, tags, refs)

    elif cmd == 'get':
        if len(argv) < 2:
            print('Usage: journal.py get <id>')
            sys.exit(1)
        cmd_get(int(argv[1]))

    elif cmd == 'search':
        if len(argv) < 2:
            print('Usage: journal.py search <query>')
            sys.exit(1)
        cmd_search(' '.join(argv[1:]))

    elif cmd == 'recent':
        n = int(argv[1]) if len(argv) > 1 else 10
        cmd_recent(n)

    elif cmd == 'by-category':
        if len(argv) < 2:
            print('Usage: journal.py by-category <category>')
            sys.exit(1)
        cmd_by_category(argv[1])

    elif cmd == 'by-tag':
        if len(argv) < 2:
            print('Usage: journal.py by-tag <tag>')
            sys.exit(1)
        cmd_by_tag(argv[1])

    elif cmd == 'refs':
        if len(argv) < 2:
            print('Usage: journal.py refs <id>')
            sys.exit(1)
        cmd_refs(int(argv[1]))

    elif cmd == 'backup':
        cmd_backup()

    elif cmd == 'dump':
        cmd_dump()

    elif cmd == 'rebuild':
        force = '--force' in argv
        sql_file = None
        for arg in argv[1:]:
            if arg != '--force':
                sql_file = arg
                break
        cmd_rebuild(sql_file, force=force)

    elif cmd == 'stats':
        cmd_stats()

    elif cmd == 'migrate':
        cmd_migrate()

    else:
        print(f'Unknown command: {cmd}')
        print(USAGE)
        sys.exit(1)
