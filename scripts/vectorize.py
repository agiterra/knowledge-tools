#!/usr/bin/env python3
"""Vectorize a knowledge vault using sentence-transformers.

Embeds all markdown files from .knowledge/ and all journal entries from journal.db,
storing vectors in .knowledge/vectors.db for semantic search.

Dependencies: sentence-transformers, numpy (lazy-loaded).
Install:  pip install sentence-transformers

Usage:
  python3 scripts/vectorize.py                     # Full build (incremental by default)
  python3 scripts/vectorize.py --stats             # Show current state
  python3 scripts/vectorize.py --force             # Re-embed everything
  python3 scripts/vectorize.py --incremental       # Scan for changes only
  python3 scripts/vectorize.py update <path>       # Single vault file
  python3 scripts/vectorize.py update --journal <id>  # Single journal entry
  python3 scripts/vectorize.py --check-deps        # Test if deps are available
  python3 scripts/vectorize.py --vault .knowledge   # Override vault directory
"""

import hashlib
import os
import sqlite3
import sys
import time

import config

# All paths relative to CWD (the agent's project root)
MODEL_NAME = 'all-MiniLM-L6-v2'
EMBEDDING_DIM = 384


def _vectors_db():
    return os.path.join(config.vault_dir(), 'vectors.db')


def _journal_db():
    return os.path.join(config.vault_dir(), 'journal.db')


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def check_deps():
    """Test whether heavy dependencies are importable."""
    ok = True
    for mod in ('sentence_transformers', 'numpy'):
        try:
            __import__(mod)
            print(f'  {mod}: ok')
        except ImportError:
            print(f'  {mod}: NOT FOUND')
            ok = False
    if not ok:
        print('\nInstall missing dependencies:')
        print('  pip install sentence-transformers')
    else:
        print('\nAll dependencies available.')
    return ok


# ---------------------------------------------------------------------------
# Blob conversion helpers
# ---------------------------------------------------------------------------

def _np():
    """Lazy numpy import."""
    import numpy as np
    return np


def vector_to_blob(vec):
    """Convert numpy float32 vector to bytes for SQLite BLOB storage."""
    np = _np()
    return np.array(vec, dtype=np.float32).tobytes()


def blob_to_vector(blob):
    """Convert SQLite BLOB back to numpy float32 vector."""
    np = _np()
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# Model loading (lazy, cached)
# ---------------------------------------------------------------------------

_model_cache = None


def get_model():
    """Load sentence-transformers model, caching for reuse."""
    global _model_cache
    if _model_cache is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            sys.stderr.write(
                'Error: sentence-transformers not installed.\n'
                'Run: pip install sentence-transformers\n'
            )
            sys.exit(1)
        _model_cache = SentenceTransformer(MODEL_NAME)
    return _model_cache


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def init_db(conn):
    """Create vector tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_vectors (
            path TEXT PRIMARY KEY,
            embedding BLOB NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal_vectors (
            journal_id INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


def content_hash(text):
    """Short SHA-256 hash for change detection."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Vault file collection
# ---------------------------------------------------------------------------

def collect_vault_files():
    """Collect all markdown files under .knowledge/ and any extra_dirs.

    Returns dict of {relative_path: text}.
    """
    files = {}
    for vault in config.vault_dirs():
        for root, _dirs, filenames in os.walk(vault):
            for fname in filenames:
                if not fname.endswith('.md'):
                    continue
                full = os.path.join(root, fname)
                rel = full  # already relative to CWD
                try:
                    with open(full, 'r', encoding='utf-8', errors='replace') as f:
                        text = f.read()
                    if text.strip():
                        files[rel] = text
                except Exception:
                    pass
    return files


# ---------------------------------------------------------------------------
# Journal entry collection
# ---------------------------------------------------------------------------

def collect_journal_entries():
    """Collect all journal entries from journal.db.

    Returns dict of {journal_id: text}.
    """
    jdb = _journal_db()
    if not os.path.exists(jdb):
        return {}
    entries = {}
    try:
        conn = sqlite3.connect(jdb, timeout=5)
        rows = conn.execute(
            'SELECT id, summary, context FROM journal'
        ).fetchall()
        conn.close()
        for jid, summary, context in rows:
            text = f'{summary or ""}\n{context or ""}'.strip()
            if text:
                entries[jid] = text
    except Exception as e:
        sys.stderr.write(f'[vectorize] journal read error: {e}\n')
    return entries


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_texts(texts, batch_size=64):
    """Embed a list of texts, return list of numpy vectors."""
    model = get_model()
    all_vecs = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        vecs = model.encode(batch, show_progress_bar=False, normalize_embeddings=True)
        all_vecs.extend(vecs)
    return all_vecs


# ---------------------------------------------------------------------------
# Full vectorize (with incremental change detection)
# ---------------------------------------------------------------------------

def vectorize(force=False):
    """Full build: embed all vault files and journal entries.

    Incremental by default — only embeds changed content unless force=True.
    """
    conn = sqlite3.connect(_vectors_db(), timeout=10)
    init_db(conn)
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    # --- Vault files ---
    vault_files = collect_vault_files()
    print(f'Found {len(vault_files)} vault markdown files')

    existing_vault = {}
    for row in conn.execute('SELECT path, content_hash FROM vault_vectors').fetchall():
        existing_vault[row[0]] = row[1]

    vault_to_embed = {}
    for path, text in vault_files.items():
        h = content_hash(text)
        if force or path not in existing_vault or existing_vault[path] != h:
            vault_to_embed[path] = (text, h)

    # Remove entries for deleted files
    deleted_paths = set(existing_vault.keys()) - set(vault_files.keys())
    if deleted_paths:
        conn.executemany('DELETE FROM vault_vectors WHERE path = ?',
                         [(p,) for p in deleted_paths])
        print(f'  Removed {len(deleted_paths)} deleted file vectors')

    if vault_to_embed:
        print(f'  Embedding {len(vault_to_embed)} vault files...')
        paths = list(vault_to_embed.keys())
        texts = [vault_to_embed[p][0] for p in paths]
        hashes = [vault_to_embed[p][1] for p in paths]
        vecs = embed_texts(texts)
        for path, vec, h in zip(paths, vecs, hashes):
            conn.execute(
                'INSERT OR REPLACE INTO vault_vectors '
                '(path, embedding, content_hash, updated_at) VALUES (?, ?, ?, ?)',
                (path, vector_to_blob(vec), h, now)
            )
        conn.commit()
        print(f'  Done: {len(vault_to_embed)} vault vectors updated')
    else:
        print('  All vault vectors up to date')

    # --- Journal entries ---
    journal_entries = collect_journal_entries()
    print(f'Found {len(journal_entries)} journal entries')

    existing_journal = {}
    for row in conn.execute('SELECT journal_id, content_hash FROM journal_vectors').fetchall():
        existing_journal[row[0]] = row[1]

    journal_to_embed = {}
    for jid, text in journal_entries.items():
        h = content_hash(text)
        if force or jid not in existing_journal or existing_journal[jid] != h:
            journal_to_embed[jid] = (text, h)

    deleted_jids = set(existing_journal.keys()) - set(journal_entries.keys())
    if deleted_jids:
        conn.executemany('DELETE FROM journal_vectors WHERE journal_id = ?',
                         [(j,) for j in deleted_jids])
        print(f'  Removed {len(deleted_jids)} deleted journal vectors')

    if journal_to_embed:
        print(f'  Embedding {len(journal_to_embed)} journal entries...')
        jids = list(journal_to_embed.keys())
        texts = [journal_to_embed[j][0] for j in jids]
        hashes = [journal_to_embed[j][1] for j in jids]
        vecs = embed_texts(texts)
        for jid, vec, h in zip(jids, vecs, hashes):
            conn.execute(
                'INSERT OR REPLACE INTO journal_vectors '
                '(journal_id, embedding, content_hash, updated_at) VALUES (?, ?, ?, ?)',
                (jid, vector_to_blob(vec), h, now)
            )
        conn.commit()
        print(f'  Done: {len(journal_to_embed)} journal vectors updated')
    else:
        print('  All journal vectors up to date')

    conn.close()


# ---------------------------------------------------------------------------
# Incremental scan (mtime heuristic + content hash)
# ---------------------------------------------------------------------------

def incremental_scan():
    """Scan for changes using mtime heuristic, then content-hash verify.

    Faster than full build when nothing has changed.
    """
    vdb = _vectors_db()
    if not os.path.exists(vdb):
        print('No vectors.db — running full build')
        vectorize()
        return

    db_mtime = os.path.getmtime(vdb)

    # Quick check: anything changed since last vectorize?
    memory_changed = False
    for vault in config.vault_dirs():
        for root, _dirs, files in os.walk(vault):
            for f in files:
                if f.endswith('.md'):
                    fpath = os.path.join(root, f)
                    if os.path.getmtime(fpath) > db_mtime:
                        memory_changed = True
                        break
            if memory_changed:
                break
        if memory_changed:
            break

    jdb = _journal_db()
    journal_changed = (
        os.path.exists(jdb)
        and os.path.getmtime(jdb) > db_mtime
    )

    if not memory_changed and not journal_changed:
        print('Nothing changed')
        return

    # Something changed — do content-hash based incremental
    vectorize(force=False)


# ---------------------------------------------------------------------------
# Targeted update: single vault file
# ---------------------------------------------------------------------------

def update_vault_file(rel_path):
    """Re-vectorize a single vault file by path."""
    vdb = _vectors_db()
    conn = sqlite3.connect(vdb, timeout=10)
    init_db(conn)
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    # Load existing hash
    row = conn.execute(
        'SELECT content_hash FROM vault_vectors WHERE path = ?', (rel_path,)
    ).fetchone()
    existing_hash = row[0] if row else None

    if not os.path.exists(rel_path):
        # File deleted — remove vector
        if existing_hash:
            conn.execute('DELETE FROM vault_vectors WHERE path = ?', (rel_path,))
            conn.commit()
            print(f'Removed vector for deleted file: {rel_path}')
        else:
            print(f'File not found and no existing vector: {rel_path}')
        conn.close()
        return

    with open(rel_path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()

    if not text.strip():
        print(f'Skipping empty file: {rel_path}')
        conn.close()
        return

    h = content_hash(text)
    if existing_hash == h:
        print(f'No change: {rel_path}')
        conn.close()
        return

    vec = embed_texts([text])[0]
    conn.execute(
        'INSERT OR REPLACE INTO vault_vectors '
        '(path, embedding, content_hash, updated_at) VALUES (?, ?, ?, ?)',
        (rel_path, vector_to_blob(vec), h, now)
    )
    conn.commit()
    conn.close()
    print(f'Updated vector: {rel_path}')


# ---------------------------------------------------------------------------
# Targeted update: single journal entry
# ---------------------------------------------------------------------------

def update_journal_entry(journal_id):
    """Re-vectorize a single journal entry by ID."""
    jdb = _journal_db()
    if not os.path.exists(jdb):
        print(f'No journal.db found at {jdb}')
        return

    # Fetch entry text
    jconn = sqlite3.connect(jdb, timeout=5)
    row = jconn.execute(
        'SELECT id, summary, context FROM journal WHERE id = ?', (journal_id,)
    ).fetchone()
    jconn.close()

    vdb = _vectors_db()
    conn = sqlite3.connect(vdb, timeout=10)
    init_db(conn)
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    if not row:
        # Entry deleted — remove vector
        conn.execute('DELETE FROM journal_vectors WHERE journal_id = ?', (journal_id,))
        conn.commit()
        conn.close()
        print(f'Removed vector for deleted journal entry: j:{journal_id}')
        return

    jid, summary, context = row
    text = f'{summary or ""}\n{context or ""}'.strip()
    if not text:
        print(f'Skipping empty journal entry: j:{journal_id}')
        conn.close()
        return

    # Check existing hash
    existing = conn.execute(
        'SELECT content_hash FROM journal_vectors WHERE journal_id = ?', (journal_id,)
    ).fetchone()
    existing_hash = existing[0] if existing else None

    h = content_hash(text)
    if existing_hash == h:
        print(f'No change: j:{journal_id}')
        conn.close()
        return

    vec = embed_texts([text])[0]
    conn.execute(
        'INSERT OR REPLACE INTO journal_vectors '
        '(journal_id, embedding, content_hash, updated_at) VALUES (?, ?, ?, ?)',
        (journal_id, vector_to_blob(vec), h, now)
    )
    conn.commit()
    conn.close()
    print(f'Updated vector: j:{journal_id}')


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def show_stats():
    """Print vector database statistics."""
    vdb = _vectors_db()
    if not os.path.exists(vdb):
        print('No vectors.db found — run vectorize.py first')
        return

    conn = sqlite3.connect(vdb, timeout=5)
    vault_count = conn.execute('SELECT COUNT(*) FROM vault_vectors').fetchone()[0]
    journal_count = conn.execute('SELECT COUNT(*) FROM journal_vectors').fetchone()[0]

    vault_latest = conn.execute(
        'SELECT updated_at FROM vault_vectors ORDER BY updated_at DESC LIMIT 1'
    ).fetchone()
    journal_latest = conn.execute(
        'SELECT updated_at FROM journal_vectors ORDER BY updated_at DESC LIMIT 1'
    ).fetchone()

    db_size = os.path.getsize(vdb)
    conn.close()

    print(f'vectors.db stats:')
    print(f'  Vault vectors:   {vault_count}')
    print(f'  Journal vectors: {journal_count}')
    print(f'  Total vectors:   {vault_count + journal_count}')
    print(f'  DB size:         {db_size / 1024:.1f} KB')
    print(f'  Embedding dim:   {EMBEDDING_DIM}')
    if vault_latest:
        print(f'  Vault updated:   {vault_latest[0]}')
    if journal_latest:
        print(f'  Journal updated: {journal_latest[0]}')


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

USAGE = """\
Usage: vectorize.py [command] [options]

Commands:
  (default)                  Full build (incremental by default)
  update <path>              Re-vectorize a single vault file
  update --journal <id>      Re-vectorize a single journal entry

Options:
  --stats                    Show vector database statistics
  --force                    Re-embed everything (ignore content hashes)
  --incremental              Scan for changes only (mtime heuristic)
  --check-deps               Test if dependencies are available
  --vault <path>             Override vault directory (default: .knowledge/)
"""

if __name__ == '__main__':
    args = sys.argv[1:]

    # Parse --vault early so config is set before anything else
    if '--vault' in args:
        idx = args.index('--vault')
        if idx + 1 < len(args):
            config.set_vault_dir(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        else:
            print('Error: --vault requires a path')
            sys.exit(1)

    if '--check-deps' in args:
        ok = check_deps()
        sys.exit(0 if ok else 1)

    if '--stats' in args:
        show_stats()
        sys.exit(0)

    # update subcommand
    if args and args[0] == 'update':
        update_args = args[1:]
        if '--journal' in update_args:
            idx = update_args.index('--journal')
            if idx + 1 >= len(update_args):
                print('Usage: vectorize.py update --journal <id>')
                sys.exit(1)
            journal_id = int(update_args[idx + 1])
            update_journal_entry(journal_id)
        elif update_args:
            update_vault_file(update_args[0])
        else:
            print('Usage: vectorize.py update <path>')
            print('       vectorize.py update --journal <id>')
            sys.exit(1)
        sys.exit(0)

    force = '--force' in args
    incremental = '--incremental' in args

    if not args or force:
        t0 = time.time()
        vectorize(force=force)
        elapsed = time.time() - t0
        print(f'\nTotal time: {elapsed:.1f}s')
        show_stats()
    elif incremental:
        t0 = time.time()
        incremental_scan()
        elapsed = time.time() - t0
        print(f'\nTotal time: {elapsed:.1f}s')
    else:
        print(USAGE)
        sys.exit(1)
