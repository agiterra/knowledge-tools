#!/usr/bin/env python3
"""Semantic vector search over a knowledge vault.

Loads a query, embeds it with all-MiniLM-L6-v2, and finds the most similar
vault files and journal entries by cosine similarity.

Dependencies: sentence-transformers, numpy (lazy-loaded).
Install:  pip install sentence-transformers

CLI:
  python3 scripts/vector-search.py "what is the bleaching?"
  python3 scripts/vector-search.py --top 10 "lossy compression"
  python3 scripts/vector-search.py --json "identity persistence"
  python3 scripts/vector-search.py --vault-only "vault architecture"
  python3 scripts/vector-search.py --journal-only "decision log"
  python3 scripts/vector-search.py --vault .knowledge "query"

Library:
  from importlib.machinery import SourceFileLoader
  vs = SourceFileLoader('vector_search', 'scripts/vector-search.py').load_module()
  results = vs.vector_search("query", top_k=5)
"""

import json
import os
import sqlite3
import sys

import config

# All paths relative to CWD (the agent's project root)
MODEL_NAME = 'all-MiniLM-L6-v2'


def _vectors_db():
    return os.path.join(config.vault_dir(), 'vectors.db')


def _journal_db():
    return os.path.join(config.vault_dir(), 'journal.db')


# ---------------------------------------------------------------------------
# Blob conversion helpers
# ---------------------------------------------------------------------------

def _np():
    """Lazy numpy import."""
    try:
        import numpy as np
        return np
    except ImportError:
        sys.stderr.write(
            'Error: numpy not installed.\n'
            'Run: pip install sentence-transformers\n'
        )
        sys.exit(1)


def blob_to_vector(blob):
    """Convert SQLite BLOB back to numpy float32 vector."""
    np = _np()
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# Model loading (lazy, cached)
# ---------------------------------------------------------------------------

_model_cache = None


def _get_model():
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
# Journal summary lookup
# ---------------------------------------------------------------------------

def _get_journal_summaries(jids):
    """Look up journal summaries for a list of journal IDs."""
    jdb = _journal_db()
    if not jids or not os.path.exists(jdb):
        return {}
    try:
        conn = sqlite3.connect(jdb, timeout=5)
        placeholders = ','.join('?' * len(jids))
        rows = conn.execute(
            f'SELECT id, summary FROM journal WHERE id IN ({placeholders})', jids
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------------------

def vector_search(query, top_k=5, vault_only=False, journal_only=False):
    """Search the vector store for entries most similar to query.

    Args:
        query: Search text to embed and compare.
        top_k: Number of results to return.
        vault_only: Only search vault file vectors.
        journal_only: Only search journal entry vectors.

    Returns list of dicts:
        [{"source": ".knowledge/...", "type": "vault"|"journal",
          "score": 0.85, "summary": "..."}]
    """
    np = _np()
    vdb = _vectors_db()
    if not os.path.exists(vdb):
        return []

    model = _get_model()
    query_vec = model.encode(query, normalize_embeddings=True)

    conn = sqlite3.connect(vdb, timeout=5)
    results = []

    # Vault vectors
    if not journal_only:
        vault_rows = conn.execute('SELECT path, embedding FROM vault_vectors').fetchall()
        for path, blob in vault_rows:
            vec = blob_to_vector(blob)
            score = float(np.dot(query_vec, vec))
            results.append({
                'source': path,
                'type': 'vault',
                'score': score,
                'summary': '',
            })

    # Journal vectors
    if not vault_only:
        journal_rows = conn.execute(
            'SELECT journal_id, embedding FROM journal_vectors'
        ).fetchall()
        journal_scores = []
        for jid, blob in journal_rows:
            vec = blob_to_vector(blob)
            score = float(np.dot(query_vec, vec))
            journal_scores.append((jid, score))

        # Look up journal summaries
        if journal_scores:
            jids = [j[0] for j in journal_scores]
            summaries = _get_journal_summaries(jids)
            for jid, score in journal_scores:
                results.append({
                    'source': f'j:{jid}',
                    'type': 'journal',
                    'score': score,
                    'summary': summaries.get(jid, ''),
                })

    conn.close()

    # Sort by score descending
    results.sort(key=lambda x: -x['score'])
    return results[:top_k]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

USAGE = """\
Usage: vector-search.py [options] <query>

Options:
  --top N           Number of results (default: 5)
  --json            Machine-readable JSON output
  --vault-only      Only search vault file vectors
  --journal-only    Only search journal entry vectors
  --vault <path>    Override vault directory (default: .knowledge/)
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

    json_output = '--json' in args
    args = [a for a in args if a != '--json']

    vault_only = '--vault-only' in args
    args = [a for a in args if a != '--vault-only']

    journal_only = '--journal-only' in args
    args = [a for a in args if a != '--journal-only']

    top_k = 5
    if '--top' in args:
        idx = args.index('--top')
        if idx + 1 < len(args):
            top_k = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        else:
            print('Error: --top requires a number')
            sys.exit(1)

    if not args:
        print(USAGE)
        sys.exit(1)

    query = ' '.join(args)
    results = vector_search(query, top_k=top_k,
                            vault_only=vault_only, journal_only=journal_only)

    if json_output:
        print(json.dumps(results, indent=2))
    else:
        print(f'Query: "{query}"')
        print(f'Results: {len(results)}')
        print()
        for i, r in enumerate(results, 1):
            print(f'  {i}. [{r["type"]}] {r["source"]} (score: {r["score"]:.4f})')
            if r['summary']:
                print(f'     {r["summary"][:120]}')
            print()
