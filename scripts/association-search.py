#!/usr/bin/env python3
"""Associative retrieval search — the mechanical search layer.

Takes event text as input, returns scored associations from:
1. Semantic index keyword expansion (spreading activation with IDF weighting)
2. Journal full-text search (journal.db)
3. Vault file matching (semantic-index.json)
4. Vector similarity search (vectors.db) — optional, graceful degradation

Pure Python, no LLM, target <50ms per query (vector search may add ~100ms).

All paths are resolved via config.vault_dir(), defaulting to .knowledge/
in CWD. Override with --vault <path> or KNOWLEDGE_VAULT env var.

Usage:
    python3 scripts/association-search.py "some event text"
    python3 scripts/association-search.py --json "some event text"
    python3 scripts/association-search.py --no-vector "fast keyword-only search"
    python3 scripts/association-search.py --top 10 "more results"
    python3 scripts/association-search.py --vault /path/to/.knowledge "query"

As a library:
    from association_search import search_associations
    results = search_associations("some event text", top_k=5)
    results = search_associations("fast mode", vector_limit=0)
"""

import json
import math
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import config

# ---------------------------------------------------------------------------
# Config — all paths resolved dynamically via config.vault_dir()
# ---------------------------------------------------------------------------


def _journal_db():
    return os.path.join(config.vault_dir(), 'journal.db')


def _semantic_index():
    return os.path.join(config.vault_dir(), 'meta', 'semantic-index.json')


def _vault_label():
    """Derive a short label from the vault path for multi-vault disambiguation.

    For relative paths like '.knowledge', uses the project directory name.
    For absolute paths, uses the parent directory name.
    Returns empty string if vault is in CWD (single-vault, no prefix needed).
    """
    vd = config.vault_dir()
    resolved = os.path.realpath(vd)
    cwd_resolved = os.path.realpath('.')
    parent = os.path.dirname(resolved)
    # If vault is in CWD, no prefix needed
    if parent == cwd_resolved:
        return ""
    # Use the parent directory name as label
    return os.path.basename(parent)


def _vectors_db():
    return os.path.join(config.vault_dir(), 'vectors.db')


# Stopwords for keyword extraction
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about", "up",
    "it", "its", "this", "that", "these", "those", "i", "me", "my",
    "we", "our", "you", "your", "he", "him", "his", "she", "her",
    "they", "them", "their", "what", "which", "who", "whom",
    "think", "also", "like", "get", "got", "make", "much", "even",
    "thing", "things", "something", "anything", "nothing", "really",
}

# Cache for semantic index (loaded once per process)
_semantic_index_cache = None
_semantic_index_mtime = 0


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def extract_keywords(text, max_keywords=15):
    """Extract meaningful keywords from text using simple tokenization."""
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_-]*', text.lower())
    tokens = [t for t in tokens if t not in STOPWORDS and len(t) > 2]
    freq = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [word for word, _ in ranked[:max_keywords]]


# ---------------------------------------------------------------------------
# Semantic index loading (cached)
# ---------------------------------------------------------------------------

def _load_semantic_index():
    """Load semantic index, caching by file mtime."""
    global _semantic_index_cache, _semantic_index_mtime
    idx_path = _semantic_index()
    if not os.path.exists(idx_path):
        return {}
    mtime = os.path.getmtime(idx_path)
    if _semantic_index_cache is not None and mtime == _semantic_index_mtime:
        return _semantic_index_cache
    try:
        with open(idx_path) as f:
            _semantic_index_cache = json.load(f).get("entries", {})
        _semantic_index_mtime = mtime
    except Exception as e:
        sys.stderr.write(f"[assoc] semantic index load error: {e}\n")
        _semantic_index_cache = {}
    return _semantic_index_cache


# ---------------------------------------------------------------------------
# Keyword expansion via semantic index (spreading activation)
# ---------------------------------------------------------------------------

def expand_keywords(keywords, max_expansion=10):
    """Expand keywords by finding related terms through the semantic index.

    For each keyword that matches a vault file's keywords, pull that file's
    OTHER keywords as expansion candidates. This is spreading activation:
    the concept graph propagates relevance beyond the original query terms.

    Uses IDF weighting to penalize ubiquitous terms and boost rare, specific
    terms that actually discriminate.
    """
    entries = _load_semantic_index()
    keyword_set = set(keywords)
    expansion = {}  # candidate -> raw activation count
    doc_freq = {}   # candidate -> number of files containing it

    # First pass: count activations and document frequency
    for entry in entries.values():
        entry_keywords = set(k.lower() for k in entry.get("keywords", []))
        # Count document frequency for all keywords
        for ek in entry_keywords:
            doc_freq[ek] = doc_freq.get(ek, 0) + 1
        # Spreading activation from matching files
        overlap = keyword_set & entry_keywords
        if overlap:
            for ek in entry_keywords - keyword_set:
                expansion[ek] = expansion.get(ek, 0) + len(overlap)

    # Second pass: IDF-weight the expansion scores
    # score = raw_activation / log(1 + doc_freq) — penalizes common terms
    idf_scored = []
    for ek, raw_score in expansion.items():
        if ek in STOPWORDS or len(ek) <= 2:
            continue
        df = doc_freq.get(ek, 1)
        idf_score = raw_score / math.log(1 + df)
        idf_scored.append((ek, idf_score))

    idf_scored.sort(key=lambda x: -x[1])
    return [k for k, _ in idf_scored[:max_expansion]]


# ---------------------------------------------------------------------------
# Journal search
# ---------------------------------------------------------------------------

def search_journal(keywords, limit=10):
    """Search journal.db for entries matching keywords."""
    db_path = _journal_db()
    if not os.path.exists(db_path):
        return []

    results = []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        rows = conn.execute(
            "SELECT id, category, summary, context, tags, timestamp FROM journal"
        ).fetchall()
        conn.close()

        for jid, category, summary, context, tags, created_at in rows:
            searchable = f"{summary} {context} {tags}".lower()
            matches = [k for k in keywords if k in searchable]
            if not matches:
                continue

            score = len(matches)
            # Bonus for summary matches (concentrated signal)
            summary_lower = (summary or "").lower()
            score += sum(0.5 for k in keywords if k in summary_lower)

            context_snippet = (context[:200] + "...") if context and len(context) > 200 else context
            label = _vault_label()
            source_id = f"{label}/journal:{jid}" if label else f"journal:{jid}"
            results.append({
                "source": source_id,
                "type": "journal",
                "category": category,
                "summary": summary,
                "context_snippet": context_snippet,
                "score": score,
                "matched_keywords": matches,
                "created_at": created_at,
            })

        results.sort(key=lambda x: -x["score"])
    except Exception as e:
        sys.stderr.write(f"[assoc] journal search error: {e}\n")
    return results[:limit]


# ---------------------------------------------------------------------------
# Semantic index search
# ---------------------------------------------------------------------------

def search_semantic_index(keywords, limit=10):
    """Search semantic-index.json for vault files matching keywords."""
    entries = _load_semantic_index()
    keyword_set = set(keywords)
    results = []

    for path, entry in entries.items():
        entry_keywords = set(k.lower() for k in entry.get("keywords", []))
        summary = entry.get("summary", "").lower()

        overlap = keyword_set & entry_keywords
        if not overlap:
            summary_matches = [k for k in keywords if k in summary]
            if not summary_matches:
                continue
            score = len(summary_matches) * 0.5
            matched = summary_matches
        else:
            score = len(overlap)
            matched = list(overlap)

        # Connection density bonus
        related_count = len([r for r in entry.get("related", []) if r])
        score += min(related_count * 0.1, 0.5)

        label = _vault_label()
        source_path = f"{label}/{path}" if label else path
        results.append({
            "source": source_path,
            "type": "vault",
            "summary": entry.get("summary", ""),
            "score": score,
            "matched_keywords": matched,
        })

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ---------------------------------------------------------------------------
# Vector similarity search
# ---------------------------------------------------------------------------

VECTOR_SERVICE_URL = os.environ.get("VECTOR_SERVICE_URL", "http://127.0.0.1:9801")


def search_vectors(text, limit=10):
    """Search for semantically similar entries via the Wire vector service.

    Calls the persistent vector service over HTTP. Falls back to in-process
    search if the service is unavailable.

    Gracefully returns [] if vectors.db doesn't exist or service is down.
    """
    if limit <= 0:
        return []
    vectors_db = _vectors_db()
    if not os.path.exists(vectors_db):
        return []

    vault_path = os.path.abspath(os.path.dirname(vectors_db))

    # Try the persistent vector service first (fast — model already loaded)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{VECTOR_SERVICE_URL}/search",
            data=json.dumps({"query": text, "vault_path": vault_path, "top_k": limit}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        label = _vault_label()
        results = []
        for r in data.get("results", []):
            src = f"{label}/{r['source']}" if label else r["source"]
            results.append({
                "source": src,
                "type": r["type"],
                "summary": r.get("summary", ""),
                "score": r["score"],
                "matched_keywords": [],
                "search_method": "vector",
            })
        return results
    except Exception as e:
        sys.stderr.write(f"[assoc] vector service unavailable ({e}), falling back to in-process\n")

    # Fallback: in-process vector search (slow — loads model each time)
    try:
        import importlib.util
        script_dir = os.path.dirname(os.path.abspath(__file__))
        vs_path = os.path.join(script_dir, "vector-search.py")
        spec = importlib.util.spec_from_file_location("vector_search", vs_path)
        if spec is None or spec.loader is None:
            return []
        vector_search_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vector_search_mod)
        vector_search = vector_search_mod.vector_search
        raw = vector_search(text, top_k=limit)
        label = _vault_label()
        results = []
        for r in raw:
            src = f"{label}/{r['source']}" if label else r["source"]
            results.append({
                "source": src,
                "type": r["type"],
                "summary": r.get("summary", ""),
                "score": r["score"],
                "matched_keywords": [],
                "search_method": "vector",
            })
        return results
    except Exception as e:
        sys.stderr.write(f"[assoc] vector search error: {e}\n")
        return []


# ---------------------------------------------------------------------------
# Combined search with keyword expansion
# ---------------------------------------------------------------------------

def search_associations(text, top_k=5, journal_limit=10, vault_limit=10, vector_limit=5, sources=None):
    """Run associative search across all sources with keyword expansion.

    Args:
        text: Query text to search for
        top_k: Maximum results to return
        journal_limit: Max journal hits to consider
        vault_limit: Max vault hits to consider
        vector_limit: Max vector hits (0 = skip vector search entirely)
        sources: Optional list of sources to search ("journal", "vault", "vector")
                 None means search all available sources.

    Returns:
        dict with keys: results, timing_ms, sources_used, keywords,
        expanded_keywords, metrics

    Flow:
    1. Extract keywords from event text
    2. Expand via semantic index (spreading activation)
    3. Search journal, vault, and vector store
    4. Merge, deduplicate, normalize, rank, return top-K with metrics
    """
    metrics = {}
    sources_used = []
    t0 = time.time()

    # Determine which sources to search
    search_journal_flag = sources is None or "journal" in sources
    search_vault_flag = sources is None or "vault" in sources
    search_vector_flag = (sources is None or "vector" in sources) and vector_limit > 0

    # Phase 1: Keyword extraction
    t_kw = time.time()
    raw_keywords = extract_keywords(text)
    metrics["keyword_extraction_ms"] = round((time.time() - t_kw) * 1000, 2)
    metrics["raw_keywords_count"] = len(raw_keywords)
    metrics["input_token_count"] = len(text.split())

    if not raw_keywords:
        return {
            "results": [],
            "timing_ms": round((time.time() - t0) * 1000, 2),
            "sources_used": [],
            "keywords": [],
            "expanded_keywords": [],
            "metrics": metrics,
        }

    # Phase 2: Keyword expansion
    t_expand = time.time()
    expanded = expand_keywords(raw_keywords)
    all_keywords = raw_keywords + expanded
    metrics["expansion_ms"] = round((time.time() - t_expand) * 1000, 2)
    metrics["expanded_keywords_count"] = len(expanded)
    metrics["total_keywords"] = len(all_keywords)

    # Phase 3: Search each source — keyword-based and vector run in parallel
    journal_results = []
    vault_results = []
    vector_results = []

    def _search_keywords():
        """Run keyword-based searches (journal + vault) sequentially."""
        jr, vr = [], []
        if search_journal_flag:
            t_journal = time.time()
            jr = search_journal(all_keywords, limit=journal_limit)
            metrics["journal_search_ms"] = round((time.time() - t_journal) * 1000, 2)
            metrics["journal_hits"] = len(jr)
        if search_vault_flag:
            t_vault = time.time()
            vr = search_semantic_index(all_keywords, limit=vault_limit)
            metrics["vault_search_ms"] = round((time.time() - t_vault) * 1000, 2)
            metrics["vault_hits"] = len(vr)
        return jr, vr

    def _search_vector():
        """Run vector similarity search."""
        t_vector = time.time()
        vr = search_vectors(text, limit=vector_limit)
        metrics["vector_search_ms"] = round((time.time() - t_vector) * 1000, 2)
        metrics["vector_hits"] = len(vr)
        return vr

    if search_vector_flag:
        # Run keyword and vector branches in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            kw_future = pool.submit(_search_keywords)
            vec_future = pool.submit(_search_vector)
            journal_results, vault_results = kw_future.result()
            vector_results = vec_future.result()
    else:
        # No vector search — run keyword branch only (no thread overhead)
        journal_results, vault_results = _search_keywords()

    if journal_results:
        sources_used.append("journal")
    if vault_results:
        sources_used.append("vault")
    if vector_results:
        sources_used.append("vector")

    # Phase 4: Normalize and merge
    t_merge = time.time()

    def normalize(results):
        if not results:
            return results
        max_score = max(r["score"] for r in results)
        if max_score <= 0:
            return results
        for r in results:
            r["normalized_score"] = r["score"] / max_score
        return results

    journal_results = normalize(journal_results)
    vault_results = normalize(vault_results)
    vector_results = normalize(vector_results)

    # Merge all results, deduplicating by source (keep highest score)
    seen = {}
    for r in journal_results + vault_results + vector_results:
        src = r["source"]
        if src not in seen or r.get("normalized_score", 0) > seen[src].get("normalized_score", 0):
            seen[src] = r

    all_results = list(seen.values())
    all_results.sort(key=lambda x: -x.get("normalized_score", 0))
    metrics["merge_ms"] = round((time.time() - t_merge) * 1000, 2)

    # Coverage: what fraction of raw keywords matched something?
    all_matched = set()
    for r in all_results:
        all_matched.update(r.get("matched_keywords", []))
    raw_matched = all_matched & set(raw_keywords)
    metrics["keyword_coverage"] = round(len(raw_matched) / len(raw_keywords), 2) if raw_keywords else 0

    total_ms = round((time.time() - t0) * 1000, 2)
    metrics["total_ms"] = total_ms

    # Build result list in standard format
    results = []
    for r in all_results[:top_k]:
        results.append({
            "source": r["source"],
            "type": r.get("type", "unknown"),
            "score": r.get("normalized_score", 0),
            "summary": r.get("summary", ""),
            "matched_keywords": r.get("matched_keywords", []),
        })

    return {
        "results": results,
        "timing_ms": total_ms,
        "sources_used": sources_used,
        "keywords": raw_keywords,
        "expanded_keywords": expanded,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """\
Usage: association-search.py [options] <query text>

Options:
  --vault PATH  Override vault directory (default: .knowledge/)
  --json        Machine-readable JSON output
  --no-vector   Skip vector search (fast keyword-only mode)
  --top N       Number of results to return (default 8)

As a library:
  from association_search import search_associations
  results = search_associations("query", top_k=5, vector_limit=5)
"""


def main():
    # Parse --vault first so config is set before anything else
    argv = sys.argv[1:]
    filtered_argv = []
    i = 0
    while i < len(argv):
        if argv[i] == "--vault" and i + 1 < len(argv):
            config.set_vault_dir(argv[i + 1])
            i += 2
            continue
        filtered_argv.append(argv[i])
        i += 1

    if not filtered_argv:
        print(USAGE)
        sys.exit(1)

    json_output = "--json" in filtered_argv
    no_vector = "--no-vector" in filtered_argv

    # Parse --top N
    top_k = 8
    args = []
    skip_next = False
    for idx, a in enumerate(filtered_argv):
        if skip_next:
            skip_next = False
            continue
        if a == "--top" and idx + 1 < len(filtered_argv):
            try:
                top_k = int(filtered_argv[idx + 1])
            except ValueError:
                pass
            skip_next = True
            continue
        if a in ("--json", "--no-vector"):
            continue
        args.append(a)

    if not args:
        print(USAGE)
        sys.exit(1)

    text = " ".join(args)
    vector_limit = 0 if no_vector else 5

    result = search_associations(text, top_k=top_k, vector_limit=vector_limit)

    if json_output:
        print(json.dumps(result, indent=2))
    else:
        m = result.get("metrics", {})
        print(f"Keywords: {', '.join(result['keywords'])}")
        if result.get("expanded_keywords"):
            print(f"Expanded: {', '.join(result['expanded_keywords'])}")

        hit_parts = []
        if "journal_hits" in m:
            hit_parts.append(f"{m['journal_hits']} journal")
        if "vault_hits" in m:
            hit_parts.append(f"{m['vault_hits']} vault")
        if "vector_hits" in m:
            hit_parts.append(f"{m['vector_hits']} vector")
        print(f"Found: {' + '.join(hit_parts)} hits")

        timing_parts = [f"{m.get('total_ms', 0)}ms total"]
        timing_parts.append(f"kw:{m.get('keyword_extraction_ms', 0)}ms")
        timing_parts.append(f"expand:{m.get('expansion_ms', 0)}ms")
        if "journal_search_ms" in m:
            timing_parts.append(f"journal:{m['journal_search_ms']}ms")
        if "vault_search_ms" in m:
            timing_parts.append(f"vault:{m['vault_search_ms']}ms")
        if "vector_search_ms" in m:
            timing_parts.append(f"vector:{m['vector_search_ms']}ms")
        print(f"Timing: {' '.join(timing_parts)}")

        print(f"Coverage: {m.get('keyword_coverage', 0):.0%} of raw keywords matched")
        if result.get("sources_used"):
            print(f"Sources: {', '.join(result['sources_used'])}")
        print()

        for i, assoc in enumerate(result["results"], 1):
            source = assoc["source"]
            score = assoc.get("score", 0)
            summary = assoc.get("summary", "")[:120]
            atype = assoc.get("type", "?")
            print(f"  {i}. [{atype}] {source} (score: {score:.2f})")
            print(f"     {summary}")
            if assoc.get("matched_keywords"):
                print(f"     matched: {', '.join(assoc['matched_keywords'][:8])}")
            print()


if __name__ == "__main__":
    main()
