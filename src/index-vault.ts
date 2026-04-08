/**
 * Semantic index for a knowledge vault.
 *
 * Builds a keyword-based semantic index of all markdown files in .knowledge/.
 * Designed to be driven by a model subagent that generates summaries and
 * keywords for each file, then updates the index via the `update` function.
 */

import { Database } from "bun:sqlite";
import { createHash } from "crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { join } from "path";
import { Glob } from "bun";
import { vaultDir, vaultDirs } from "./config";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface IndexEntry {
  source_path: string;
  content_hash: string;
  summary: string;
  keywords: string[];
  related: string[];
}

export interface VaultIndex {
  version: number;
  entries: Record<string, IndexEntry>;
}

interface MissEntry {
  timestamp: string;
  query: string;
  expected_path: string;
  reason: string;
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

function indexFile(): string {
  return join(vaultDir(), "meta", "semantic-index.json");
}

function missLogFile(): string {
  return join(vaultDir(), "meta", "miss-log.json");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function contentHash(text: string): string {
  return createHash("sha256").update(text).digest("hex").slice(0, 16);
}

export function loadIndex(): VaultIndex {
  try {
    return JSON.parse(readFileSync(indexFile(), "utf-8"));
  } catch {
    return { version: 1, entries: {} };
  }
}

export function saveIndex(index: VaultIndex): void {
  const idx = indexFile();
  mkdirSync(join(idx, ".."), { recursive: true });
  writeFileSync(idx, JSON.stringify(index, null, 2));
}

export function listVaultFiles(): string[] {
  const files: string[] = [];
  for (const d of vaultDirs()) {
    const glob = new Glob("**/*.md");
    for (const path of glob.scanSync({ cwd: d })) {
      files.push(join(d, path));
    }
  }
  return files.sort();
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/** Find files needing indexing. Returns paths that need processing. */
export function scan(): string[] {
  const index = loadIndex();
  const files = listVaultFiles();
  const needsIndexing: string[] = [];

  for (const fpath of files) {
    const text = readFileSync(fpath, "utf-8");
    const h = contentHash(text);
    const entry = index.entries[fpath];
    if (entry && entry.content_hash === h) continue;
    needsIndexing.push(fpath);
  }

  return needsIndexing;
}

/** Update a single entry in the index. */
export function update(
  fpath: string,
  summary: string,
  keywords: string[],
  related: string[] = [],
): void {
  if (!existsSync(fpath)) {
    throw new Error(`File not found: ${fpath}`);
  }
  const index = loadIndex();
  const text = readFileSync(fpath, "utf-8");
  index.entries[fpath] = {
    source_path: fpath,
    content_hash: contentHash(text),
    summary,
    keywords,
    related,
  };
  saveIndex(index);
}

/** Expand compound keywords into individual terms for partial matching. */
function expandKeywords(keywords: string[]): Set<string> {
  const terms = new Set<string>();
  for (const k of keywords) {
    const kl = k.toLowerCase();
    terms.add(kl);
    const parts = kl.split(/\s+/);
    if (parts.length > 1) {
      for (const p of parts) terms.add(p);
    }
  }
  return terms;
}

/** Search by keyword overlap. Returns scored results. */
export function search(
  query: string,
  expandedTerms?: string[],
  topN = 10,
): { score: number; path: string; entry: IndexEntry }[] {
  const index = loadIndex();
  const queryTerms = new Set(query.toLowerCase().split(/\s+/));

  const expansion = new Set<string>();
  if (expandedTerms) {
    for (const t of expandedTerms) {
      const tl = t.toLowerCase().trim();
      if (tl && !queryTerms.has(tl)) expansion.add(tl);
    }
  }

  const results: { score: number; path: string; entry: IndexEntry }[] = [];

  for (const [fpath, entry] of Object.entries(index.entries)) {
    const entryTerms = expandKeywords(entry.keywords ?? []);
    const summaryTerms = new Set((entry.summary ?? "").toLowerCase().split(/\s+/));

    const kwOverlap = [...queryTerms].filter((t) => entryTerms.has(t)).length;
    const summaryOverlap = [...queryTerms].filter((t) => summaryTerms.has(t)).length * 0.5;
    const expKwOverlap = expansion.size > 0
      ? [...expansion].filter((t) => entryTerms.has(t)).length * 0.6
      : 0;
    const expSummaryOverlap = expansion.size > 0
      ? [...expansion].filter((t) => summaryTerms.has(t)).length * 0.3
      : 0;

    const score = kwOverlap + summaryOverlap + expKwOverlap + expSummaryOverlap;
    if (score > 0) {
      results.push({ score, path: fpath, entry });
    }
  }

  results.sort((a, b) => b.score - a.score);
  return results.slice(0, topN);
}

/** Log a search miss for evaluation. */
export function logMiss(query: string, expectedPath: string, reason = ""): void {
  const logPath = missLogFile();
  let log: MissEntry[] = [];
  try {
    log = JSON.parse(readFileSync(logPath, "utf-8"));
  } catch {
    // empty or missing
  }

  log.push({
    timestamp: new Date().toISOString(),
    query,
    expected_path: expectedPath,
    reason,
  });

  mkdirSync(join(logPath, ".."), { recursive: true });
  writeFileSync(logPath, JSON.stringify(log, null, 2));
}

/** Print index statistics. */
export function stats(): {
  totalFiles: number;
  indexed: number;
  uniqueKeywords: number;
  stale: string[];
} {
  const index = loadIndex();
  const entries = index.entries;
  const totalFiles = listVaultFiles().length;
  const indexed = Object.keys(entries).length;

  const allKeywords = new Set<string>();
  for (const entry of Object.values(entries)) {
    for (const k of entry.keywords ?? []) {
      allKeywords.add(k.toLowerCase());
    }
  }

  const stale = Object.keys(entries).filter((p) => !existsSync(p));

  return { totalFiles, indexed, uniqueKeywords: allKeywords.size, stale };
}
