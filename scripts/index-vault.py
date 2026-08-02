#!/usr/bin/env python3
"""Semantic index for an agent's memory vault.

Builds a keyword-based semantic index of all markdown files in .knowledge/.
Designed to be driven by a model subagent that generates summaries and
keywords for each file, then updates the index via the `update` command.

The index lives at .knowledge/meta/semantic-index.json and supports search
by keyword overlap, structured JSON output for reranking, and miss
logging for evaluation.

Usage:
  # Scan vault — print files that need indexing
  python3 scripts/index-vault.py scan

  # Print a single file's content + hash (for the subagent to summarize)
  python3 scripts/index-vault.py file .knowledge/some/note.md

  # Update index entry after subagent generates summary + keywords
  python3 scripts/index-vault.py update <path> <summary> <keywords-csv> [related-csv]

  # Search by keyword overlap
  python3 scripts/index-vault.py search <query>

  # Search with structured JSON output (for subagent reranking)
  python3 scripts/index-vault.py search-json <query>

  # Log a missed association (query didn't surface expected file)
  python3 scripts/index-vault.py miss <query> <expected-path> [reason]

  # Show miss log
  python3 scripts/index-vault.py misses

  # Show index stats
  python3 scripts/index-vault.py stats
"""

import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
import config


def _index_file():
    """Return path to the semantic index JSON file."""
    return os.path.join(config.vault_dir(), 'meta', 'semantic-index.json')


def _miss_log_file():
    """Return path to the miss log JSON file."""
    return os.path.join(config.vault_dir(), 'meta', 'miss-log.json')


def content_hash(text):
    """Short SHA-256 hash for change detection."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_index():
    """Load index from disk, or return empty structure."""
    try:
        with open(_index_file(), 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'version': 1, 'entries': {}}


def save_index(index):
    """Write index to disk, creating directories as needed."""
    idx = _index_file()
    os.makedirs(os.path.dirname(idx), exist_ok=True)
    with open(idx, 'w') as f:
        json.dump(index, f, indent=2)


def vault_files():
    """Find all markdown files in the vault and any extra_dirs."""
    files = []
    for d in config.vault_dirs():
        files.extend(glob.glob(os.path.join(d, '**', '*.md'), recursive=True))
    return sorted(files)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_scan():
    """Find files needing indexing. Prints info for a subagent to process."""
    index = load_index()
    files = vault_files()
    needs_indexing = []

    unreadable = []
    for fpath in files:
        # A SCAN MUST NOT DIE ON ONE UNREADABLE FILE. One 0600 file in a vault
        # (a deliberately-restricted security report) crashed the whole scan for
        # any other uid -- and the caller's `grep -c NEEDS_INDEX` on a crashed
        # run returns 0, which is INDISTINGUISHABLE FROM "nothing outstanding".
        # A partial answer that names what it could not read beats an exception
        # that a consumer silently rounds to zero.
        try:
            with open(fpath, 'r') as f:
                text = f.read()
        except OSError as exc:
            unreadable.append(fpath)
            print('UNREADABLE: %s (%s)' % (fpath, exc.__class__.__name__))
            continue
        h = content_hash(text)
        entry = index['entries'].get(fpath)
        if entry and entry.get('content_hash') == h:
            continue
        needs_indexing.append(fpath)
        print(f'NEEDS_INDEX: {fpath}')
        print(f'  hash: {h}')
        # Preview first 5 lines for context
        for line in text.split('\n')[:5]:
            print(f'  | {line}')
        print()

    # ★ ALWAYS STATE HOW MANY FILES WERE EXAMINED. "All files indexed and up to
    # date" after scanning ZERO files is indistinguishable from a genuinely clean
    # vault -- and that is exactly what a caller running from the wrong cwd got:
    # an empty scan, exit 0, and a consumer that counted 0 NEEDS_INDEX and called
    # it healthy. A count of what was looked at is the only thing that separates
    # "nothing to report" from "I looked at nothing".
    print('SCANNED: %d file(s)' % len(files))
    if not files:
        print('SCANNED-ZERO: no vault files found from %s — this is NOT a clean '
              'vault, it is an empty or wrong working directory.' % os.getcwd())
    if unreadable:
        print('\n%d file(s) UNREADABLE by %s — not examined, NOT counted as indexed.'
              % (len(unreadable), __import__('getpass').getuser()))
    if not needs_indexing:
        print('All files indexed and up to date.')
    else:
        print(f'\n{len(needs_indexing)} file(s) need indexing.')

    return needs_indexing


def cmd_file(fpath):
    """Print file contents and hash for a subagent to summarize."""
    if not os.path.isfile(fpath):
        print(f'Error: file not found: {fpath}')
        sys.exit(1)
    with open(fpath, 'r') as f:
        text = f.read()
    print(text)
    print(f'\nContent hash: {content_hash(text)}')


def cmd_update(fpath, summary, keywords, related=None):
    """Update a single entry in the index."""
    if not os.path.isfile(fpath):
        print(f'Error: file not found: {fpath}')
        sys.exit(1)
    index = load_index()
    with open(fpath, 'r') as f:
        text = f.read()
    index['entries'][fpath] = {
        'source_path': fpath,
        'content_hash': content_hash(text),
        'summary': summary,
        'keywords': keywords,
        'related': related or [],
    }
    save_index(index)
    print(f'Indexed: {fpath} ({len(keywords)} keywords)')


def _expand_keywords(keywords):
    """Expand compound keywords into individual terms for partial matching.

    "Borden twins" -> {"borden twins", "borden", "twins"}
    """
    terms = set()
    for k in keywords:
        kl = k.lower()
        terms.add(kl)
        parts = kl.split()
        if len(parts) > 1:
            terms.update(parts)
    return terms


def cmd_search(query, expanded_terms=None):
    """Search by keyword overlap with query terms. Prints ranked results.

    If expanded_terms is provided (comma-separated string or list), those
    terms are included as additional query terms at reduced weight (0.6x).
    This enables Haiku-powered query expansion without changing the core
    scoring logic.
    """
    index = load_index()
    query_terms = set(query.lower().split())

    # Parse expanded terms if provided
    expansion = set()
    if expanded_terms:
        if isinstance(expanded_terms, str):
            expansion = {t.strip().lower() for t in expanded_terms.split(',') if t.strip()}
        else:
            expansion = {t.lower() for t in expanded_terms}
        expansion -= query_terms  # Don't double-count original terms

    results = []

    for fpath, entry in index['entries'].items():
        entry_terms = _expand_keywords(entry.get('keywords', []))
        summary_terms = set(entry.get('summary', '').lower().split())
        # Original query terms: full weight for keywords, half for summary
        kw_overlap = len(query_terms & entry_terms)
        summary_overlap = len(query_terms & summary_terms) * 0.5
        # Expanded terms: reduced weight (0.6 for keywords, 0.3 for summary)
        exp_kw_overlap = len(expansion & entry_terms) * 0.6 if expansion else 0
        exp_summary_overlap = len(expansion & summary_terms) * 0.3 if expansion else 0
        score = kw_overlap + summary_overlap + exp_kw_overlap + exp_summary_overlap
        if score > 0:
            results.append((score, fpath, entry))

    results.sort(key=lambda x: -x[0])

    if not results:
        print(f'No matches for: {query}')
        return []

    print(f'Results for: {query}')
    if expansion:
        print(f'  (expanded: {", ".join(sorted(expansion))})')
    print()
    for score, fpath, entry in results[:10]:
        print(f'  [{score:.1f}] {fpath}')
        print(f'        {entry.get("summary", "")[:100]}')
        matched = query_terms & _expand_keywords(entry.get('keywords', []))
        exp_matched = expansion & _expand_keywords(entry.get('keywords', [])) if expansion else set()
        if matched or exp_matched:
            parts = []
            if matched:
                parts.append(', '.join(sorted(matched)))
            if exp_matched:
                parts.append(f'expanded: {", ".join(sorted(exp_matched))}')
            print(f'        matched: {" | ".join(parts)}')
        print()

    return results


def cmd_search_json(query, top_n=10, expanded_terms=None):
    """Search and return structured JSON for subagent reranking."""
    index = load_index()
    query_terms = set(query.lower().split())

    expansion = set()
    if expanded_terms:
        if isinstance(expanded_terms, str):
            expansion = {t.strip().lower() for t in expanded_terms.split(',') if t.strip()}
        else:
            expansion = {t.lower() for t in expanded_terms}
        expansion -= query_terms

    results = []

    for fpath, entry in index['entries'].items():
        entry_terms = _expand_keywords(entry.get('keywords', []))
        summary_terms = set(entry.get('summary', '').lower().split())
        kw_overlap = len(query_terms & entry_terms)
        summary_overlap = len(query_terms & summary_terms) * 0.5
        exp_kw_overlap = len(expansion & entry_terms) * 0.6 if expansion else 0
        exp_summary_overlap = len(expansion & summary_terms) * 0.3 if expansion else 0
        score = kw_overlap + summary_overlap + exp_kw_overlap + exp_summary_overlap
        if score > 0:
            results.append((score, fpath, entry))

    results.sort(key=lambda x: -x[0])

    candidates = []
    for score, fpath, entry in results[:top_n]:
        matched_kw = sorted(query_terms & _expand_keywords(entry.get('keywords', [])))
        exp_matched = sorted(expansion & _expand_keywords(entry.get('keywords', []))) if expansion else []
        candidates.append({
            'path': fpath,
            'keyword_score': score,
            'summary': entry.get('summary', ''),
            'keywords': entry.get('keywords', []),
            'matched_keywords': matched_kw,
            'expanded_matched': exp_matched,
            'related': entry.get('related', []),
        })

    output = {
        'query': query,
        'query_terms': sorted(query_terms),
        'expanded_terms': sorted(expansion) if expansion else [],
        'candidate_count': len(candidates),
        'candidates': candidates,
    }
    print(json.dumps(output, indent=2))
    return output


def cmd_miss(query, expected_path, reason=''):
    """Log a search miss for evaluation."""
    miss_log = _miss_log_file()
    try:
        with open(miss_log, 'r') as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []

    log.append({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'query': query,
        'expected_path': expected_path,
        'reason': reason,
    })
    os.makedirs(os.path.dirname(miss_log), exist_ok=True)
    with open(miss_log, 'w') as f:
        json.dump(log, f, indent=2)
    print(f'Logged miss: query=\'{query}\' expected=\'{expected_path}\'')


def cmd_misses():
    """Print the miss log for review."""
    try:
        with open(_miss_log_file(), 'r') as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print('No misses logged yet.')
        return

    print(f'Miss log: {len(log)} entries\n')
    for entry in log:
        print(f'  [{entry.get("timestamp", "?")[:10]}] query: {entry["query"]}')
        print(f'    expected: {entry["expected_path"]}')
        if entry.get('reason'):
            print(f'    reason: {entry["reason"]}')
        print()


PLACEHOLDER_PREFIXES = (
    'Personal operational guidance:',
    'Knowledge entry:',
    'Incident observation log:',
    'Runtime state snapshot',
)


def _is_thin(candidate, reference):
    """True if candidate looks auto-generated or materially poorer than reference."""
    if not candidate:
        return True
    if candidate.startswith(PLACEHOLDER_PREFIXES):
        return True
    return len(candidate) < len(reference) * 0.6


def cmd_prune(apply=False, delete_orphans=False):
    """Remove index entries whose file is gone -- RESCUING CONTENT FIRST.

    THE NAIVE VERSION OF THIS WAS DANGEROUS AND A DRY RUN CAUGHT IT (Brioche,
    2026-07-31). Deleting every entry whose path is missing is correct about
    PATHS and catastrophic about CONTENT. On his vault 214 of 983 entries were
    dangling -- but 78 were files MOVED by a restructure, whose new paths had
    been re-indexed with AUTO-GENERATED PLACEHOLDER summaries while the old
    entries still held the good hand-written ones:

        stale .knowledge/feedback/ci-not-triggering-checklist.md
              "When CI jobs show SKIPPED, check draft status, diff preconditions"
        live  .knowledge/ci-not-triggering-checklist.md
              "Knowledge entry: ci not triggering checklist"       <- placeholder

    Prune-by-file-existence deletes the RICH entry and keeps the USELESS one, and
    nothing would ever say so -- the file exists, so the live entry looks healthy.
    It would have destroyed 63 hand-written summaries.

    THE QUESTION IS NOT "does this path exist" BUT "is this KNOWLEDGE still
    represented". So dangling entries are classified:

      DUPLICATE -- a live entry shares the basename. Rescue the summary onto it
                   when the live one is a placeholder or materially thinner,
                   union the keyword sets either way, then drop the stale entry.
      ORPHAN    -- nothing live carries that basename. NOT deleted by default:
                   the entry may be the last trace of a note consolidated
                   elsewhere. Preserve-then-delete.

    Dry-run by default. --apply rescues and removes duplicates; --delete-orphans
    is a separate explicit opt-in.
    """
    idx = _index_file()
    try:
        with open(idx, 'r') as f:
            data = json.load(f)
    except Exception as exc:
        print('index-vault prune: cannot read %s: %s' % (idx, exc))
        return 2

    entries = data.get('entries', {})
    dangling = [k for k in entries if not os.path.exists(k)]
    if not dangling:
        print('index-vault prune: 0 dangling of %d entries -- nothing to do.' % len(entries))
        return 0

    live_by_base = {}
    for k in entries:
        if os.path.exists(k):
            live_by_base.setdefault(os.path.basename(k), []).append(k)

    duplicates, orphans = [], []
    for k in dangling:
        (duplicates if live_by_base.get(os.path.basename(k)) else orphans).append(k)

    rescued = merged = 0
    for k in duplicates:
        stale = entries[k]
        for live in live_by_base[os.path.basename(k)]:
            cur = entries[live]
            s_sum = str(stale.get('summary', ''))
            l_sum = str(cur.get('summary', ''))
            print('  DUPLICATE: %s' % k)
            print('      -> live: %s' % live)
            if _is_thin(l_sum, s_sum):
                print('      RESCUE summary (live is a placeholder or materially thinner)')
                print('         stale: %s' % s_sum[:90])
                print('         live : %s' % l_sum[:90])
                if apply:
                    cur['summary'] = s_sum
                rescued += 1
            s_kw = set(stale.get('keywords') or [])
            l_kw = set(cur.get('keywords') or [])
            if s_kw - l_kw:
                print('      MERGE %d keyword(s) missing from the live entry' % len(s_kw - l_kw))
                if apply:
                    cur['keywords'] = sorted(l_kw | s_kw)
                merged += 1

    for k in orphans:
        print('  ORPHAN (no live counterpart): %s' % k)
        print('      %s' % str(entries[k].get('summary', ''))[:90])

    if not apply:
        print('')
        print('index-vault prune: %d dangling of %d -- %d DUPLICATE, %d ORPHAN.'
              % (len(dangling), len(entries), len(duplicates), len(orphans)))
        print('DRY RUN. --apply rescues content and removes duplicates; orphans')
        print('additionally need --delete-orphans (they may be the last trace of')
        print('a note that was consolidated elsewhere).')
        return 1

    for k in duplicates:
        del entries[k]
    removed_orphans = 0
    if delete_orphans:
        for k in orphans:
            del entries[k]
        removed_orphans = len(orphans)

    data['entries'] = entries
    with open(idx, 'w') as f:
        json.dump(data, f, indent=2)
    tail = ((', removed %d orphans' % removed_orphans) if delete_orphans
            else ', LEFT %d orphans in place' % len(orphans))
    print('')
    print('index-vault prune: rescued %d summaries, merged %d keyword sets, '
          'removed %d duplicates%s. %d entries remain.'
          % (rescued, merged, len(duplicates), tail, len(entries)))
    return 0


def cmd_stats():
    """Print index statistics."""
    index = load_index()
    entries = index.get('entries', {})
    total_files = len(vault_files())
    indexed = len(entries)

    all_keywords = set()
    for entry in entries.values():
        all_keywords.update(k.lower() for k in entry.get('keywords', []))

    # Check for stale entries (files that were deleted)
    stale = [p for p in entries if not os.path.isfile(p)]

    print(f'Vault files:      {total_files}')
    print(f'Indexed:          {indexed}')
    print(f'Unique keywords:  {len(all_keywords)}')
    if stale:
        print(f'Stale entries:    {len(stale)} (indexed file no longer exists)')
    if all_keywords:
        sample = sorted(all_keywords)[:20]
        print(f'Sample keywords:  {", ".join(sample)}')

    # Miss log stats
    try:
        with open(_miss_log_file(), 'r') as f:
            misses = json.load(f)
        print(f'Logged misses:    {len(misses)}')
    except (FileNotFoundError, json.JSONDecodeError):
        pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

USAGE = """\
Usage: index-vault.py [--vault <path>] <command> [args]

Commands:
  scan                              Find files needing indexing
  file <path>                       Print file content + hash
  search <query>                    Search by keyword overlap
  search <query> --expand <terms>   Search with expanded terms (reduced weight)
  search-json <query>               Search with JSON output for reranking
  search-json <query> --expand <t>  JSON search with expanded terms
  update <path> <summary> <kw-csv>  Update index entry
  miss <query> <expected> [reason]  Log a search miss
  misses                            Show miss log
  stats                             Show index statistics
"""

if __name__ == '__main__':
    # Parse --vault before command dispatch
    args = sys.argv[1:]
    if '--vault' in args:
        idx = args.index('--vault')
        if idx + 1 < len(args):
            config.set_vault_dir(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        else:
            print('Error: --vault requires a path argument')
            sys.exit(1)

    if len(args) < 1:
        print(USAGE)
        sys.exit(1)

    cmd = args[0]

    if cmd == 'scan':
        cmd_scan()
    elif cmd == 'prune':
        sys.exit(cmd_prune(apply='--apply' in args,
                           delete_orphans='--delete-orphans' in args))
    elif cmd == 'file':
        if len(args) < 2:
            print('Usage: index-vault.py file <path>')
            sys.exit(1)
        cmd_file(args[1])
    elif cmd in ('search', 'search-json'):
        if len(args) < 2:
            print(f'Usage: index-vault.py {cmd} <query> [--expand <terms-csv>]')
            sys.exit(1)
        # Parse --expand flag from args
        cmd_args = args[1:]
        expanded = None
        if '--expand' in cmd_args:
            eidx = cmd_args.index('--expand')
            if eidx + 1 < len(cmd_args):
                expanded = cmd_args[eidx + 1]
                cmd_args = cmd_args[:eidx]  # query is everything before --expand
            else:
                print('Error: --expand requires a comma-separated terms argument')
                sys.exit(1)
        query = ' '.join(cmd_args)
        if cmd == 'search':
            cmd_search(query, expanded_terms=expanded)
        else:
            cmd_search_json(query, expanded_terms=expanded)
    elif cmd == 'update':
        if len(args) < 4:
            print('Usage: index-vault.py update <path> <summary> <keywords-csv> [related-csv]')
            sys.exit(1)
        fpath = args[1]
        summary = args[2]
        keywords = [k.strip() for k in args[3].split(',')]
        related = [r.strip() for r in args[4].split(',')] if len(args) > 4 else []
        cmd_update(fpath, summary, keywords, related)
    elif cmd == 'miss':
        if len(args) < 3:
            print('Usage: index-vault.py miss <query> <expected-path> [reason]')
            sys.exit(1)
        cmd_miss(args[1], args[2], args[3] if len(args) > 3 else '')
    elif cmd == 'misses':
        cmd_misses()
    elif cmd == 'stats':
        cmd_stats()
    else:
        print(f'Unknown command: {cmd}')
        print(USAGE)
        sys.exit(1)
