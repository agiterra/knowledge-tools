/**
 * Associative retrieval search — the mechanical search layer.
 *
 * Takes event text as input, returns scored associations from:
 * 1. Semantic index keyword expansion (spreading activation with IDF weighting)
 * 2. Journal full-text search (journal.db)
 * 3. Vault file matching (semantic-index.json)
 * 4. Vector similarity search (vector service over HTTP) — optional, graceful degradation
 *
 * Pure TypeScript, no LLM, target <50ms per query (vector search may add ~100ms).
 */

import { Database } from "bun:sqlite";
import { existsSync, readFileSync, statSync } from "fs";
import { dirname, join, resolve } from "path";
import { vaultDir } from "./config";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AssociationResult {
  source: string;
  type: string;
  score: number;
  summary: string;
  matched_keywords: string[];
  search_method?: string;
  normalized_score?: number;
}

export interface SearchResult {
  results: AssociationResult[];
  timing_ms: number;
  sources_used: string[];
  keywords: string[];
  expanded_keywords: string[];
  metrics: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

function journalDb(): string {
  return join(vaultDir(), "journal.db");
}

function semanticIndexPath(): string {
  return join(vaultDir(), "meta", "semantic-index.json");
}

function vectorsDb(): string {
  return join(vaultDir(), "vectors.db");
}

// ---------------------------------------------------------------------------
// Stopwords
// ---------------------------------------------------------------------------

const STOPWORDS = new Set([
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
]);

// ---------------------------------------------------------------------------
// Semantic index cache
// ---------------------------------------------------------------------------

let semanticIndexCache: Record<string, { keywords?: string[]; summary?: string; related?: string[] }> | null = null;
let semanticIndexMtime = 0;

function loadSemanticIndex(): Record<string, { keywords?: string[]; summary?: string; related?: string[] }> {
  const idxPath = semanticIndexPath();
  if (!existsSync(idxPath)) return {};

  const mtime = statSync(idxPath).mtimeMs;
  if (semanticIndexCache !== null && mtime === semanticIndexMtime) {
    return semanticIndexCache;
  }

  try {
    const data = JSON.parse(readFileSync(idxPath, "utf-8"));
    semanticIndexCache = data.entries ?? {};
    semanticIndexMtime = mtime;
  } catch (e) {
    console.error(`[assoc] semantic index load error: ${e}`);
    semanticIndexCache = {};
  }
  return semanticIndexCache!;
}

// ---------------------------------------------------------------------------
// Keyword extraction
// ---------------------------------------------------------------------------

export function extractKeywords(text: string, maxKeywords = 15): string[] {
  const tokens = text.toLowerCase().match(/[a-zA-Z_][a-zA-Z0-9_-]*/g) ?? [];
  const filtered = tokens.filter((t) => !STOPWORDS.has(t) && t.length > 2);

  const freq = new Map<string, number>();
  for (const t of filtered) {
    freq.set(t, (freq.get(t) ?? 0) + 1);
  }

  return [...freq.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, maxKeywords)
    .map(([word]) => word);
}

// ---------------------------------------------------------------------------
// Keyword expansion via semantic index (spreading activation)
// ---------------------------------------------------------------------------

export function expandKeywords(keywords: string[], maxExpansion = 10): string[] {
  const entries = loadSemanticIndex();
  const keywordSet = new Set(keywords);
  const expansion = new Map<string, number>();
  const docFreq = new Map<string, number>();

  for (const entry of Object.values(entries)) {
    const entryKeywords = new Set((entry.keywords ?? []).map((k) => k.toLowerCase()));

    for (const ek of entryKeywords) {
      docFreq.set(ek, (docFreq.get(ek) ?? 0) + 1);
    }

    const overlap = [...keywordSet].filter((k) => entryKeywords.has(k));
    if (overlap.length > 0) {
      for (const ek of entryKeywords) {
        if (!keywordSet.has(ek)) {
          expansion.set(ek, (expansion.get(ek) ?? 0) + overlap.length);
        }
      }
    }
  }

  const idfScored: [string, number][] = [];
  for (const [ek, rawScore] of expansion) {
    if (STOPWORDS.has(ek) || ek.length <= 2) continue;
    const df = docFreq.get(ek) ?? 1;
    const idfScore = rawScore / Math.log(1 + df);
    idfScored.push([ek, idfScore]);
  }

  idfScored.sort((a, b) => b[1] - a[1]);
  return idfScored.slice(0, maxExpansion).map(([k]) => k);
}

// ---------------------------------------------------------------------------
// Journal search
// ---------------------------------------------------------------------------

export function searchJournal(keywords: string[], limit = 10): AssociationResult[] {
  const dbPath = journalDb();
  if (!existsSync(dbPath)) return [];

  const results: AssociationResult[] = [];
  try {
    const db = new Database(dbPath, { readonly: true });
    const rows = db.query(
      "SELECT id, category, summary, context, tags, timestamp FROM journal"
    ).all() as { id: number; category: string; summary: string; context: string; tags: string; timestamp: string }[];
    db.close();

    for (const row of rows) {
      const searchable = `${row.summary} ${row.context} ${row.tags}`.toLowerCase();
      const matches = keywords.filter((k) => searchable.includes(k));
      if (matches.length === 0) continue;

      let score = matches.length;
      const summaryLower = (row.summary ?? "").toLowerCase();
      score += keywords.filter((k) => summaryLower.includes(k)).length * 0.5;

      results.push({
        source: `journal:${row.id}`,
        type: "journal",
        score,
        summary: row.summary,
        matched_keywords: matches,
      });
    }

    results.sort((a, b) => b.score - a.score);
  } catch (e) {
    console.error(`[assoc] journal search error: ${e}`);
  }
  return results.slice(0, limit);
}

// ---------------------------------------------------------------------------
// Semantic index search
// ---------------------------------------------------------------------------

export function searchSemanticIndex(keywords: string[], limit = 10): AssociationResult[] {
  const entries = loadSemanticIndex();
  const keywordSet = new Set(keywords);
  const results: AssociationResult[] = [];

  for (const [path, entry] of Object.entries(entries)) {
    const entryKeywords = new Set((entry.keywords ?? []).map((k) => k.toLowerCase()));
    const summary = (entry.summary ?? "").toLowerCase();

    const overlap = [...keywordSet].filter((k) => entryKeywords.has(k));
    let score: number;
    let matched: string[];

    if (overlap.length === 0) {
      const summaryMatches = keywords.filter((k) => summary.includes(k));
      if (summaryMatches.length === 0) continue;
      score = summaryMatches.length * 0.5;
      matched = summaryMatches;
    } else {
      score = overlap.length;
      matched = overlap;
    }

    const relatedCount = (entry.related ?? []).filter(Boolean).length;
    score += Math.min(relatedCount * 0.1, 0.5);

    results.push({
      source: path,
      type: "vault",
      summary: entry.summary ?? "",
      score,
      matched_keywords: matched,
    });
  }

  results.sort((a, b) => b.score - a.score);
  return results.slice(0, limit);
}

// ---------------------------------------------------------------------------
// Vector similarity search (via HTTP vector service)
// ---------------------------------------------------------------------------

const VECTOR_SERVICE_URL = process.env.VECTOR_SERVICE_URL ?? "http://127.0.0.1:9801";

export async function searchVectors(text: string, limit = 10): Promise<AssociationResult[]> {
  if (limit <= 0) return [];
  const vdb = vectorsDb();
  if (!existsSync(vdb)) return [];

  const vaultPath = resolve(dirname(vdb));

  try {
    const resp = await fetch(`${VECTOR_SERVICE_URL}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text, vault_path: vaultPath, top_k: limit }),
      signal: AbortSignal.timeout(5000),
    });
    const data = await resp.json() as { results?: { source: string; type: string; summary?: string; score: number }[] };

    return (data.results ?? []).map((r) => ({
      source: r.source,
      type: r.type,
      summary: r.summary ?? "",
      score: r.score,
      matched_keywords: [],
      search_method: "vector",
    }));
  } catch (e) {
    console.error(`[assoc] vector service unavailable (${e})`);
    return [];
  }
}

// ---------------------------------------------------------------------------
// Combined search with keyword expansion
// ---------------------------------------------------------------------------

export interface SearchOptions {
  topK?: number;
  journalLimit?: number;
  vaultLimit?: number;
  vectorLimit?: number;
  sources?: ("journal" | "vault" | "vector")[];
}

export async function searchAssociations(text: string, opts: SearchOptions = {}): Promise<SearchResult> {
  const {
    topK = 5,
    journalLimit = 10,
    vaultLimit = 10,
    vectorLimit = 5,
    sources,
  } = opts;

  const metrics: Record<string, unknown> = {};
  const sourcesUsed: string[] = [];
  const t0 = performance.now();

  const searchJournalFlag = sources == null || sources.includes("journal");
  const searchVaultFlag = sources == null || sources.includes("vault");
  const searchVectorFlag = (sources == null || sources.includes("vector")) && vectorLimit > 0;

  // Phase 1: Keyword extraction
  const tKw = performance.now();
  const rawKeywords = extractKeywords(text);
  metrics.keyword_extraction_ms = +(performance.now() - tKw).toFixed(2);
  metrics.raw_keywords_count = rawKeywords.length;
  metrics.input_token_count = text.split(/\s+/).length;

  if (rawKeywords.length === 0) {
    return {
      results: [],
      timing_ms: +(performance.now() - t0).toFixed(2),
      sources_used: [],
      keywords: [],
      expanded_keywords: [],
      metrics,
    };
  }

  // Phase 2: Keyword expansion
  const tExpand = performance.now();
  const expanded = expandKeywords(rawKeywords);
  const allKeywords = [...rawKeywords, ...expanded];
  metrics.expansion_ms = +(performance.now() - tExpand).toFixed(2);
  metrics.expanded_keywords_count = expanded.length;
  metrics.total_keywords = allKeywords.length;

  // Phase 3: Search — keyword and vector in parallel
  let journalResults: AssociationResult[] = [];
  let vaultResults: AssociationResult[] = [];
  let vectorResults: AssociationResult[] = [];

  const keywordSearch = () => {
    if (searchJournalFlag) {
      const tJ = performance.now();
      journalResults = searchJournal(allKeywords, journalLimit);
      metrics.journal_search_ms = +(performance.now() - tJ).toFixed(2);
      metrics.journal_hits = journalResults.length;
    }
    if (searchVaultFlag) {
      const tV = performance.now();
      vaultResults = searchSemanticIndex(allKeywords, vaultLimit);
      metrics.vault_search_ms = +(performance.now() - tV).toFixed(2);
      metrics.vault_hits = vaultResults.length;
    }
  };

  if (searchVectorFlag) {
    const vectorPromise = (async () => {
      const tVec = performance.now();
      vectorResults = await searchVectors(text, vectorLimit);
      metrics.vector_search_ms = +(performance.now() - tVec).toFixed(2);
      metrics.vector_hits = vectorResults.length;
    })();

    keywordSearch();
    await vectorPromise;
  } else {
    keywordSearch();
  }

  if (journalResults.length > 0) sourcesUsed.push("journal");
  if (vaultResults.length > 0) sourcesUsed.push("vault");
  if (vectorResults.length > 0) sourcesUsed.push("vector");

  // Phase 4: Normalize and merge
  const tMerge = performance.now();

  function normalize(results: AssociationResult[]): AssociationResult[] {
    if (results.length === 0) return results;
    const maxScore = Math.max(...results.map((r) => r.score));
    if (maxScore <= 0) return results;
    for (const r of results) {
      r.normalized_score = r.score / maxScore;
    }
    return results;
  }

  normalize(journalResults);
  normalize(vaultResults);
  normalize(vectorResults);

  const seen = new Map<string, AssociationResult>();
  for (const r of [...journalResults, ...vaultResults, ...vectorResults]) {
    const existing = seen.get(r.source);
    if (!existing || (r.normalized_score ?? 0) > (existing.normalized_score ?? 0)) {
      seen.set(r.source, r);
    }
  }

  const allResults = [...seen.values()].sort(
    (a, b) => (b.normalized_score ?? 0) - (a.normalized_score ?? 0)
  );
  metrics.merge_ms = +(performance.now() - tMerge).toFixed(2);

  // Coverage
  const allMatched = new Set<string>();
  for (const r of allResults) {
    for (const k of r.matched_keywords) allMatched.add(k);
  }
  const rawMatched = rawKeywords.filter((k) => allMatched.has(k));
  metrics.keyword_coverage = rawKeywords.length > 0
    ? +(rawMatched.length / rawKeywords.length).toFixed(2)
    : 0;

  const totalMs = +(performance.now() - t0).toFixed(2);
  metrics.total_ms = totalMs;

  const results = allResults.slice(0, topK).map((r) => ({
    source: r.source,
    type: r.type,
    score: r.normalized_score ?? 0,
    summary: r.summary,
    matched_keywords: r.matched_keywords,
    search_method: r.search_method,
  }));

  return {
    results,
    timing_ms: totalMs,
    sources_used: sourcesUsed,
    keywords: rawKeywords,
    expanded_keywords: expanded,
    metrics,
  };
}
