// @bun
// src/association-search.ts
import { Database } from "bun:sqlite";
import { existsSync as existsSync2, readFileSync as readFileSync2, statSync } from "fs";
import { dirname, join as join2, resolve as resolve2 } from "path";

// src/config.ts
import { existsSync, readFileSync } from "fs";
import { join, resolve } from "path";
function setVaultDir(path) {
  vaultDirOverride = resolve(path);
  configCache = null;
}
function vaultDir() {
  if (vaultDirOverride)
    return vaultDirOverride;
  return process.env.KNOWLEDGE_VAULT ?? ".knowledge";
}
function loadConfig() {
  if (configCache !== null)
    return configCache;
  const configFile = join(vaultDir(), "config.json");
  configCache = {};
  if (existsSync(configFile)) {
    try {
      configCache = JSON.parse(readFileSync(configFile, "utf-8"));
    } catch {
    }
  }
  return configCache;
}
function vaultDirs() {
  const cfg = loadConfig();
  const vd = vaultDir();
  const dirs = [vd];
  const extraDirs = cfg.extra_dirs ?? [];
  for (const d of extraDirs) {
    if (existsSync(d) && !dirs.includes(d)) {
      dirs.push(d);
    }
  }
  return dirs;
}
var vaultDirOverride = null;
var configCache = null;

// src/association-search.ts
function journalDb() {
  return join2(vaultDir(), "journal.db");
}
function semanticIndexPath() {
  return join2(vaultDir(), "meta", "semantic-index.json");
}
function vectorsDb() {
  return join2(vaultDir(), "vectors.db");
}
function loadSemanticIndex() {
  const idxPath = semanticIndexPath();
  if (!existsSync2(idxPath))
    return {};
  const mtime = statSync(idxPath).mtimeMs;
  if (semanticIndexCache !== null && mtime === semanticIndexMtime) {
    return semanticIndexCache;
  }
  try {
    const data = JSON.parse(readFileSync2(idxPath, "utf-8"));
    semanticIndexCache = data.entries ?? {};
    semanticIndexMtime = mtime;
  } catch (e) {
    console.error(`[assoc] semantic index load error: ${e}`);
    semanticIndexCache = {};
  }
  return semanticIndexCache;
}
function extractKeywords(text, maxKeywords = 15) {
  const tokens = text.toLowerCase().match(/[a-zA-Z_][a-zA-Z0-9_-]*/g) ?? [];
  const filtered = tokens.filter((t) => !STOPWORDS.has(t) && t.length > 2);
  const freq = new Map;
  for (const t of filtered) {
    freq.set(t, (freq.get(t) ?? 0) + 1);
  }
  return [...freq.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, maxKeywords).map(([word]) => word);
}
function expandKeywords(keywords, maxExpansion = 10) {
  const entries = loadSemanticIndex();
  const keywordSet = new Set(keywords);
  const expansion = new Map;
  const docFreq = new Map;
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
  const idfScored = [];
  for (const [ek, rawScore] of expansion) {
    if (STOPWORDS.has(ek) || ek.length <= 2)
      continue;
    const df = docFreq.get(ek) ?? 1;
    const idfScore = rawScore / Math.log(1 + df);
    idfScored.push([ek, idfScore]);
  }
  idfScored.sort((a, b) => b[1] - a[1]);
  return idfScored.slice(0, maxExpansion).map(([k]) => k);
}
function searchJournal(keywords, limit = 10) {
  const dbPath = journalDb();
  if (!existsSync2(dbPath))
    return [];
  const results = [];
  try {
    const db = new Database(dbPath, { readonly: true });
    const rows = db.query("SELECT id, category, summary, context, tags, timestamp FROM journal").all();
    db.close();
    for (const row of rows) {
      const searchable = `${row.summary} ${row.context} ${row.tags}`.toLowerCase();
      const matches = keywords.filter((k) => searchable.includes(k));
      if (matches.length === 0)
        continue;
      let score = matches.length;
      const summaryLower = (row.summary ?? "").toLowerCase();
      score += keywords.filter((k) => summaryLower.includes(k)).length * 0.5;
      results.push({
        source: `journal:${row.id}`,
        type: "journal",
        score,
        summary: row.summary,
        matched_keywords: matches
      });
    }
    results.sort((a, b) => b.score - a.score);
  } catch (e) {
    console.error(`[assoc] journal search error: ${e}`);
  }
  return results.slice(0, limit);
}
function searchSemanticIndex(keywords, limit = 10) {
  const entries = loadSemanticIndex();
  const keywordSet = new Set(keywords);
  const results = [];
  for (const [path, entry] of Object.entries(entries)) {
    const entryKeywords = new Set((entry.keywords ?? []).map((k) => k.toLowerCase()));
    const summary = (entry.summary ?? "").toLowerCase();
    const overlap = [...keywordSet].filter((k) => entryKeywords.has(k));
    let score;
    let matched;
    if (overlap.length === 0) {
      const summaryMatches = keywords.filter((k) => summary.includes(k));
      if (summaryMatches.length === 0)
        continue;
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
      matched_keywords: matched
    });
  }
  results.sort((a, b) => b.score - a.score);
  return results.slice(0, limit);
}
async function searchVectors(text, limit = 10) {
  if (limit <= 0)
    return [];
  const vdb = vectorsDb();
  if (!existsSync2(vdb))
    return [];
  const vaultPath = resolve2(dirname(vdb));
  try {
    const resp = await fetch(`${VECTOR_SERVICE_URL}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text, vault_path: vaultPath, top_k: limit }),
      signal: AbortSignal.timeout(5000)
    });
    const data = await resp.json();
    return (data.results ?? []).map((r) => ({
      source: r.source,
      type: r.type,
      summary: r.summary ?? "",
      score: r.score,
      matched_keywords: [],
      search_method: "vector"
    }));
  } catch (e) {
    console.error(`[assoc] vector service unavailable (${e})`);
    return [];
  }
}
async function searchAssociations(text, opts = {}) {
  const {
    topK = 5,
    journalLimit = 10,
    vaultLimit = 10,
    vectorLimit = 5,
    sources
  } = opts;
  const metrics = {};
  const sourcesUsed = [];
  const t0 = performance.now();
  const searchJournalFlag = sources == null || sources.includes("journal");
  const searchVaultFlag = sources == null || sources.includes("vault");
  const searchVectorFlag = (sources == null || sources.includes("vector")) && vectorLimit > 0;
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
      metrics
    };
  }
  const tExpand = performance.now();
  const expanded = expandKeywords(rawKeywords);
  const allKeywords = [...rawKeywords, ...expanded];
  metrics.expansion_ms = +(performance.now() - tExpand).toFixed(2);
  metrics.expanded_keywords_count = expanded.length;
  metrics.total_keywords = allKeywords.length;
  let journalResults = [];
  let vaultResults = [];
  let vectorResults = [];
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
  if (journalResults.length > 0)
    sourcesUsed.push("journal");
  if (vaultResults.length > 0)
    sourcesUsed.push("vault");
  if (vectorResults.length > 0)
    sourcesUsed.push("vector");
  const tMerge = performance.now();
  function normalize(results2) {
    if (results2.length === 0)
      return results2;
    const maxScore = Math.max(...results2.map((r) => r.score));
    if (maxScore <= 0)
      return results2;
    for (const r of results2) {
      r.normalized_score = r.score / maxScore;
    }
    return results2;
  }
  normalize(journalResults);
  normalize(vaultResults);
  normalize(vectorResults);
  const seen = new Map;
  for (const r of [...journalResults, ...vaultResults, ...vectorResults]) {
    const existing = seen.get(r.source);
    if (!existing || (r.normalized_score ?? 0) > (existing.normalized_score ?? 0)) {
      seen.set(r.source, r);
    }
  }
  const allResults = [...seen.values()].sort((a, b) => (b.normalized_score ?? 0) - (a.normalized_score ?? 0));
  metrics.merge_ms = +(performance.now() - tMerge).toFixed(2);
  const allMatched = new Set;
  for (const r of allResults) {
    for (const k of r.matched_keywords)
      allMatched.add(k);
  }
  const rawMatched = rawKeywords.filter((k) => allMatched.has(k));
  metrics.keyword_coverage = rawKeywords.length > 0 ? +(rawMatched.length / rawKeywords.length).toFixed(2) : 0;
  const totalMs = +(performance.now() - t0).toFixed(2);
  metrics.total_ms = totalMs;
  const results = allResults.slice(0, topK).map((r) => ({
    source: r.source,
    type: r.type,
    score: r.normalized_score ?? 0,
    summary: r.summary,
    matched_keywords: r.matched_keywords,
    search_method: r.search_method
  }));
  return {
    results,
    timing_ms: totalMs,
    sources_used: sourcesUsed,
    keywords: rawKeywords,
    expanded_keywords: expanded,
    metrics
  };
}
var STOPWORDS = new Set([
  "the",
  "a",
  "an",
  "is",
  "are",
  "was",
  "were",
  "be",
  "been",
  "being",
  "have",
  "has",
  "had",
  "do",
  "does",
  "did",
  "will",
  "would",
  "could",
  "should",
  "may",
  "might",
  "shall",
  "can",
  "need",
  "dare",
  "ought",
  "used",
  "to",
  "of",
  "in",
  "for",
  "on",
  "with",
  "at",
  "by",
  "from",
  "as",
  "into",
  "through",
  "during",
  "before",
  "after",
  "above",
  "below",
  "between",
  "out",
  "off",
  "over",
  "under",
  "again",
  "further",
  "then",
  "once",
  "here",
  "there",
  "when",
  "where",
  "why",
  "how",
  "all",
  "each",
  "every",
  "both",
  "few",
  "more",
  "most",
  "other",
  "some",
  "such",
  "no",
  "nor",
  "not",
  "only",
  "own",
  "same",
  "so",
  "than",
  "too",
  "very",
  "just",
  "because",
  "but",
  "and",
  "or",
  "if",
  "while",
  "about",
  "up",
  "it",
  "its",
  "this",
  "that",
  "these",
  "those",
  "i",
  "me",
  "my",
  "we",
  "our",
  "you",
  "your",
  "he",
  "him",
  "his",
  "she",
  "her",
  "they",
  "them",
  "their",
  "what",
  "which",
  "who",
  "whom",
  "think",
  "also",
  "like",
  "get",
  "got",
  "make",
  "much",
  "even",
  "thing",
  "things",
  "something",
  "anything",
  "nothing",
  "really"
]);
var semanticIndexCache = null;
var semanticIndexMtime = 0;
var VECTOR_SERVICE_URL = process.env.VECTOR_SERVICE_URL ?? "http://127.0.0.1:9801";
// src/index-vault.ts
import { createHash } from "crypto";
import { existsSync as existsSync3, mkdirSync, readFileSync as readFileSync3, writeFileSync } from "fs";
import { join as join3 } from "path";
var {Glob } = globalThis.Bun;
function indexFile() {
  return join3(vaultDir(), "meta", "semantic-index.json");
}
function missLogFile() {
  return join3(vaultDir(), "meta", "miss-log.json");
}
function contentHash(text) {
  return createHash("sha256").update(text).digest("hex").slice(0, 16);
}
function loadIndex() {
  try {
    return JSON.parse(readFileSync3(indexFile(), "utf-8"));
  } catch {
    return { version: 1, entries: {} };
  }
}
function saveIndex(index) {
  const idx = indexFile();
  mkdirSync(join3(idx, ".."), { recursive: true });
  writeFileSync(idx, JSON.stringify(index, null, 2));
}
function listVaultFiles() {
  const files = [];
  for (const d of vaultDirs()) {
    const glob = new Glob("**/*.md");
    for (const path of glob.scanSync({ cwd: d })) {
      files.push(join3(d, path));
    }
  }
  return files.sort();
}
function scan() {
  const index = loadIndex();
  const files = listVaultFiles();
  const needsIndexing = [];
  for (const fpath of files) {
    const text = readFileSync3(fpath, "utf-8");
    const h = contentHash(text);
    const entry = index.entries[fpath];
    if (entry && entry.content_hash === h)
      continue;
    needsIndexing.push(fpath);
  }
  return needsIndexing;
}
function update(fpath, summary, keywords, related = []) {
  if (!existsSync3(fpath)) {
    throw new Error(`File not found: ${fpath}`);
  }
  const index = loadIndex();
  const text = readFileSync3(fpath, "utf-8");
  index.entries[fpath] = {
    source_path: fpath,
    content_hash: contentHash(text),
    summary,
    keywords,
    related
  };
  saveIndex(index);
}
function expandKeywords2(keywords) {
  const terms = new Set;
  for (const k of keywords) {
    const kl = k.toLowerCase();
    terms.add(kl);
    const parts = kl.split(/\s+/);
    if (parts.length > 1) {
      for (const p of parts)
        terms.add(p);
    }
  }
  return terms;
}
function search(query, expandedTerms, topN = 10) {
  const index = loadIndex();
  const queryTerms = new Set(query.toLowerCase().split(/\s+/));
  const expansion = new Set;
  if (expandedTerms) {
    for (const t of expandedTerms) {
      const tl = t.toLowerCase().trim();
      if (tl && !queryTerms.has(tl))
        expansion.add(tl);
    }
  }
  const results = [];
  for (const [fpath, entry] of Object.entries(index.entries)) {
    const entryTerms = expandKeywords2(entry.keywords ?? []);
    const summaryTerms = new Set((entry.summary ?? "").toLowerCase().split(/\s+/));
    const kwOverlap = [...queryTerms].filter((t) => entryTerms.has(t)).length;
    const summaryOverlap = [...queryTerms].filter((t) => summaryTerms.has(t)).length * 0.5;
    const expKwOverlap = expansion.size > 0 ? [...expansion].filter((t) => entryTerms.has(t)).length * 0.6 : 0;
    const expSummaryOverlap = expansion.size > 0 ? [...expansion].filter((t) => summaryTerms.has(t)).length * 0.3 : 0;
    const score = kwOverlap + summaryOverlap + expKwOverlap + expSummaryOverlap;
    if (score > 0) {
      results.push({ score, path: fpath, entry });
    }
  }
  results.sort((a, b) => b.score - a.score);
  return results.slice(0, topN);
}
function logMiss(query, expectedPath, reason = "") {
  const logPath = missLogFile();
  let log = [];
  try {
    log = JSON.parse(readFileSync3(logPath, "utf-8"));
  } catch {
  }
  log.push({
    timestamp: new Date().toISOString(),
    query,
    expected_path: expectedPath,
    reason
  });
  mkdirSync(join3(logPath, ".."), { recursive: true });
  writeFileSync(logPath, JSON.stringify(log, null, 2));
}
function stats() {
  const index = loadIndex();
  const entries = index.entries;
  const totalFiles = listVaultFiles().length;
  const indexed = Object.keys(entries).length;
  const allKeywords = new Set;
  for (const entry of Object.values(entries)) {
    for (const k of entry.keywords ?? []) {
      allKeywords.add(k.toLowerCase());
    }
  }
  const stale = Object.keys(entries).filter((p) => !existsSync3(p));
  return { totalFiles, indexed, uniqueKeywords: allKeywords.size, stale };
}
// src/journal.ts
import { Database as Database2 } from "bun:sqlite";
import { copyFileSync, existsSync as existsSync4, mkdirSync as mkdirSync2, readFileSync as readFileSync4, writeFileSync as writeFileSync2 } from "fs";
import { dirname as dirname2, join as join4 } from "path";
function dbPath() {
  return join4(vaultDir(), "journal.db");
}
function dumpPath() {
  return join4(vaultDir(), "journal.sql");
}
function getDb() {
  const path = dbPath();
  mkdirSync2(dirname2(path), { recursive: true });
  return new Database2(path);
}
function formatEntry(row) {
  const lines = [`j:${row.id}  [${row.timestamp.slice(0, 10)}]  ${row.category ?? "\u2014"}`];
  lines.push(`  ${row.summary}`);
  if (row.source)
    lines.push(`  source: ${row.source}`);
  if (row.tags)
    lines.push(`  tags: ${row.tags}`);
  if (row.refs)
    lines.push(`  refs: ${row.refs}`);
  lines.push("  ---");
  let ctx = row.context;
  if (ctx.length > 500)
    ctx = ctx.slice(0, 497) + "...";
  for (const line of ctx.split("\n")) {
    lines.push(`  ${line}`);
  }
  return lines.join("\n");
}
function init() {
  const db = getDb();
  db.exec(SCHEMA);
  db.close();
}
function add(category, summary, context, opts = {}) {
  const db = getDb();
  db.exec(SCHEMA);
  const ts = new Date().toISOString();
  const tagsStr = opts.tags ? opts.tags.split(",").map((t) => t.trim()).join(",") : null;
  const result = db.run("INSERT INTO journal (timestamp, category, summary, context, source, tags, refs) VALUES (?, ?, ?, ?, ?, ?, ?)", [ts, category, summary, context, opts.source ?? null, tagsStr, opts.refs ?? null]);
  const entryId = Number(result.lastInsertRowid);
  db.close();
  return entryId;
}
function get(entryId) {
  const db = getDb();
  const row = db.query("SELECT * FROM journal WHERE id = ?").get(entryId);
  db.close();
  return row;
}
function searchEntries(query, limit = 20) {
  const db = getDb();
  const ftsQuery = query.split(/\s+/).map((t) => `"${t}"`).join(" OR ");
  let rows;
  try {
    rows = db.query(`SELECT j.* FROM journal_fts fts
       JOIN journal j ON j.id = fts.rowid
       WHERE journal_fts MATCH ?
       ORDER BY rank
       LIMIT ?`).all(ftsQuery, limit);
  } catch {
    const pattern = `%${query}%`;
    rows = db.query(`SELECT * FROM journal
       WHERE summary LIKE ? OR context LIKE ? OR tags LIKE ?
       ORDER BY timestamp DESC LIMIT ?`).all(pattern, pattern, pattern, limit);
  }
  db.close();
  return rows;
}
function recent(n = 10) {
  const db = getDb();
  const rows = db.query("SELECT * FROM journal ORDER BY id DESC LIMIT ?").all(n);
  db.close();
  return rows;
}
function byCategory(category) {
  const db = getDb();
  const rows = db.query("SELECT * FROM journal WHERE category = ? ORDER BY id DESC").all(category);
  db.close();
  return rows;
}
function byTag(tag) {
  const db = getDb();
  const pattern = `%${tag}%`;
  const rows = db.query("SELECT * FROM journal WHERE tags LIKE ? ORDER BY id DESC").all(pattern);
  db.close();
  return rows.filter((row) => {
    const entryTags = (row.tags ?? "").split(",").map((t) => t.trim().toLowerCase());
    return entryTags.includes(tag.toLowerCase());
  });
}
function refs(entryId) {
  const db = getDb();
  const id = String(entryId);
  const rows = db.query(`SELECT * FROM journal
     WHERE refs LIKE ? OR refs LIKE ? OR refs LIKE ? OR refs = ?
     ORDER BY id DESC`).all(`${id},%`, `%,${id},%`, `%,${id}`, id);
  db.close();
  return rows;
}
function dump() {
  const path = dbPath();
  if (!existsSync4(path))
    throw new Error("No journal database found.");
  const result = Bun.spawnSync(["sqlite3", path, ".dump"]);
  const sql = result.stdout.toString();
  writeFileSync2(dumpPath(), sql + "\n");
  return sql;
}
function backup() {
  const path = dbPath();
  if (!existsSync4(path))
    return false;
  const db = getDb();
  const dbCount = db.query("SELECT COUNT(*) as cnt FROM journal").get().cnt;
  db.close();
  if (dbCount === 0)
    return false;
  const sql = (() => {
    const result = Bun.spawnSync(["sqlite3", path, ".dump"]);
    return result.stdout.toString();
  })();
  const insertCount = (sql.match(/INSERT INTO "journal"/g) ?? []).length;
  if (insertCount < dbCount) {
    console.error(`VERIFICATION FAILED: DB has ${dbCount} entries but dump has ${insertCount} INSERTs.`);
    return false;
  }
  writeFileSync2(dumpPath(), sql + "\n");
  return true;
}
function rebuild(sqlFile, force = false) {
  const path = dbPath();
  const file = sqlFile ?? dumpPath();
  if (!existsSync4(file)) {
    console.error(`SQL dump not found: ${file}`);
    return false;
  }
  const sql = readFileSync4(file, "utf-8");
  const insertCount = (sql.match(/INSERT INTO "journal"/g) ?? []).length;
  if (insertCount === 0 && existsSync4(path) && !force) {
    const db2 = new Database2(path, { readonly: true });
    const dbCount = db2.query("SELECT COUNT(*) as cnt FROM journal").get().cnt;
    db2.close();
    if (dbCount > 0) {
      console.error(`REFUSED: SQL file has 0 INSERTs but existing DB has ${dbCount} entries.`);
      return false;
    }
  }
  if (existsSync4(path)) {
    copyFileSync(path, path + ".bak");
    Bun.spawnSync(["rm", path]);
  }
  const db = new Database2(path);
  db.exec(sql);
  db.close();
  return true;
}
function journalStats() {
  const path = dbPath();
  if (!existsSync4(path))
    return { total: 0, categories: [], dateRange: null, topTags: [] };
  const db = getDb();
  const total = db.query("SELECT COUNT(*) as cnt FROM journal").get().cnt;
  if (total === 0) {
    db.close();
    return { total: 0, categories: [], dateRange: null, topTags: [] };
  }
  const categories = db.query("SELECT category, COUNT(*) as cnt FROM journal GROUP BY category ORDER BY cnt DESC").all();
  const first = db.query("SELECT timestamp FROM journal ORDER BY id ASC LIMIT 1").get();
  const last = db.query("SELECT timestamp FROM journal ORDER BY id DESC LIMIT 1").get();
  const tagRows = db.query("SELECT tags FROM journal WHERE tags IS NOT NULL").all();
  const tagFreq = new Map;
  for (const row of tagRows) {
    for (const tag of row.tags.split(",")) {
      const t = tag.trim().toLowerCase();
      if (t)
        tagFreq.set(t, (tagFreq.get(t) ?? 0) + 1);
    }
  }
  db.close();
  const topTags = [...tagFreq.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15).map(([tag, count]) => ({ tag, count }));
  return {
    total,
    categories: categories.map((c) => ({ category: c.category, count: c.cnt })),
    dateRange: { first: first.timestamp, last: last.timestamp },
    topTags
  };
}
var SCHEMA = `
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
`;
// src/sonnet-filter.ts
import { existsSync as existsSync5, readFileSync as readFileSync5 } from "fs";
import { join as join5 } from "path";
function getApiKey() {
  if (apiKeyCache)
    return apiKeyCache;
  const envKey = process.env.ANTHROPIC_API_KEY;
  if (envKey) {
    apiKeyCache = envKey;
    return envKey;
  }
  const envFile = join5(process.cwd(), ".env");
  if (existsSync5(envFile)) {
    try {
      for (const line of readFileSync5(envFile, "utf-8").split("\n")) {
        const trimmed = line.trim();
        if (trimmed.startsWith("ANTHROPIC_API_KEY=")) {
          const key = trimmed.split("=", 2)[1].trim().replace(/^['"]|['"]$/g, "");
          if (key) {
            apiKeyCache = key;
            return key;
          }
        }
      }
    } catch {
    }
  }
  return null;
}
async function callSonnet(prompt, systemPrompt) {
  const apiKey = getApiKey();
  if (!apiKey)
    return { text: null, metrics: { error: "no_api_key" } };
  const payload = {
    model: MODEL,
    max_tokens: MAX_TOKENS,
    messages: [{ role: "user", content: prompt }]
  };
  if (systemPrompt)
    payload.system = systemPrompt;
  const t0 = performance.now();
  try {
    const resp = await fetch(API_URL, {
      method: "POST",
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(TIMEOUT)
    });
    const result = await resp.json();
    const text = (result.content ?? []).filter((b) => b.type === "text").map((b) => b.text).join("");
    return {
      text,
      metrics: {
        latency_ms: +(performance.now() - t0).toFixed(2),
        input_tokens: result.usage?.input_tokens ?? 0,
        output_tokens: result.usage?.output_tokens ?? 0,
        model: result.model ?? MODEL
      }
    };
  } catch (e) {
    return {
      text: null,
      metrics: {
        error: String(e),
        latency_ms: +(performance.now() - t0).toFixed(2)
      }
    };
  }
}
function formatAssociations(associations) {
  return associations.map((a, i) => {
    const summary = (a.summary ?? "").slice(0, 300);
    const score = a.normalized_score ?? a.score ?? 0;
    const keywords = (a.matched_keywords ?? []).slice(0, 8).join(", ");
    return `${i + 1}. [${a.type}] ${a.source} (keyword score: ${score.toFixed(2)})\n   Summary: ${summary}\n   Matched: ${keywords}`;
  }).join("\n\n");
}
async function filterAssociations(rawAssociations, eventText, opusContext, topK = 4) {
  const metrics = { raw_count: rawAssociations.length };
  const t0 = performance.now();
  if (rawAssociations.length === 0) {
    metrics.total_ms = 0;
    return { filtered: [], metrics, raw_count: 0 };
  }
  const formatted = formatAssociations(rawAssociations);
  const promptParts = [`## Event\n${eventText}`];
  if (opusContext)
    promptParts.push(`## Current Conversation Context\n${opusContext}`);
  promptParts.push(`## Raw Associations (${rawAssociations.length} keyword matches)\n${formatted}`);
  promptParts.push(`\nFilter to the ${topK} most genuinely relevant associations given the event and conversation context. Return JSON only.`);
  const prompt = promptParts.join("\n\n");
  metrics.prompt_tokens_est = prompt.split(/\s+/).length;
  const { text: responseText, metrics: callMetrics } = await callSonnet(prompt, SYSTEM_PROMPT);
  metrics.sonnet_call = callMetrics;
  if (responseText === null) {
    metrics.total_ms = +(performance.now() - t0).toFixed(2);
    metrics.fallback = "no_response";
    const top = [...rawAssociations].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
    return { filtered: top.slice(0, topK), metrics, raw_count: rawAssociations.length };
  }
  try {
    let text = responseText.trim();
    if (text.startsWith("```")) {
      text = text.includes("\n") ? text.split("\n", 2)[1] : text.slice(3);
      if (text.endsWith("```"))
        text = text.slice(0, -3);
      text = text.trim();
    }
    const result = JSON.parse(text);
    const sourceMap = new Map(rawAssociations.map((a) => [a.source, a]));
    const filtered = [];
    for (const ref of (result.filtered ?? []).slice(0, topK)) {
      const original = sourceMap.get(ref.source);
      if (original) {
        filtered.push({
          ...original,
          sonnet_relevance: ref.relevance ?? "unknown",
          sonnet_reason: ref.reason ?? ""
        });
      }
    }
    metrics.filtered_count = filtered.length;
    metrics.dropped_reason = result.dropped_reason ?? "";
    metrics.total_ms = +(performance.now() - t0).toFixed(2);
    return { filtered, metrics, raw_count: rawAssociations.length };
  } catch (e) {
    metrics.parse_error = String(e);
    metrics.raw_response = responseText.slice(0, 200);
    metrics.total_ms = +(performance.now() - t0).toFixed(2);
    const top = [...rawAssociations].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
    return { filtered: top.slice(0, topK), metrics, raw_count: rawAssociations.length };
  }
}
var API_URL = "https://api.anthropic.com/v1/messages";
var MODEL = process.env.ASSOCIATION_FILTER_MODEL ?? "claude-sonnet-4-6";
var MAX_TOKENS = 1024;
var TIMEOUT = parseInt(process.env.ASSOCIATION_FILTER_TIMEOUT ?? "25", 10) * 1000;
var apiKeyCache = null;
var SYSTEM_PROMPT = `You are a relevance filter for an AI agent's associative memory system.

You receive:
1. An event (new message or trigger) that the agent needs to respond to
2. A summary of what the agent is currently discussing (conversation context)
3. Raw keyword-matched associations from the agent's memory (journal entries, vault files)

Your job: identify which 1-4 associations are genuinely relevant given the CURRENT conversation context. Not just keyword matches \u2014 thematic connections, useful background, things that would change how the agent responds.

Return ONLY valid JSON (no markdown fencing, no commentary):
{
  "filtered": [
    {
      "source": "journal:123",
      "relevance": "high",
      "reason": "One sentence explaining WHY this connects to the current context"
    }
  ],
  "dropped_reason": "Brief note on why the rest were noise"
}

Be aggressive about filtering. 15 keyword hits should become 2-3 genuine connections. If nothing is truly relevant, return an empty filtered array. Speed over thoroughness \u2014 make fast judgment calls.`;
export {
  vaultDirs,
  vaultDir,
  update,
  stats,
  setVaultDir,
  searchVectors,
  searchSemanticIndex,
  searchJournal,
  searchAssociations,
  search,
  scan,
  saveIndex,
  logMiss,
  loadIndex,
  loadConfig,
  listVaultFiles,
  journalStats,
  searchEntries as journalSearch,
  refs as journalRefs,
  recent as journalRecent,
  rebuild as journalRebuild,
  init as journalInit,
  get as journalGet,
  formatEntry as journalFormatEntry,
  dump as journalDump,
  byTag as journalByTag,
  byCategory as journalByCategory,
  backup as journalBackup,
  add as journalAdd,
  filterAssociations,
  extractKeywords,
  expandKeywords,
  contentHash
};
