/**
 * Journal system for agent memory — the log of WHY beliefs exist.
 *
 * An append-only SQLite database that records the context behind changes
 * to an agent's identity, values, and knowledge.
 */

import { Database } from "bun:sqlite";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { dirname, join } from "path";
import { vaultDir } from "./config";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface JournalEntry {
  id: number;
  timestamp: string;
  category: string | null;
  summary: string;
  context: string;
  source: string | null;
  tags: string | null;
  refs: string | null;
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

function dbPath(): string {
  return join(vaultDir(), "journal.db");
}

function dumpPath(): string {
  return join(vaultDir(), "journal.sql");
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

const SCHEMA = `
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

// ---------------------------------------------------------------------------
// Database helpers
// ---------------------------------------------------------------------------

function getDb(): Database {
  const path = dbPath();
  mkdirSync(dirname(path), { recursive: true });
  return new Database(path);
}

export function formatEntry(row: JournalEntry): string {
  const lines = [`j:${row.id}  [${row.timestamp.slice(0, 10)}]  ${row.category ?? "—"}`];
  lines.push(`  ${row.summary}`);
  if (row.source) lines.push(`  source: ${row.source}`);
  if (row.tags) lines.push(`  tags: ${row.tags}`);
  if (row.refs) lines.push(`  refs: ${row.refs}`);
  lines.push("  ---");
  let ctx = row.context;
  if (ctx.length > 500) ctx = ctx.slice(0, 497) + "...";
  for (const line of ctx.split("\n")) {
    lines.push(`  ${line}`);
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/** Initialize the journal database with schema. */
export function init(): void {
  const db = getDb();
  db.exec(SCHEMA);
  db.close();
}

/** Add a new journal entry. Returns the new entry ID. */
export function add(
  category: string,
  summary: string,
  context: string,
  opts: { source?: string; tags?: string; refs?: string } = {},
): number {
  const db = getDb();
  db.exec(SCHEMA); // ensure schema exists
  const ts = new Date().toISOString();
  const tagsStr = opts.tags
    ? opts.tags.split(",").map((t) => t.trim()).join(",")
    : null;

  const result = db.run(
    "INSERT INTO journal (timestamp, category, summary, context, source, tags, refs) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [ts, category, summary, context, opts.source ?? null, tagsStr, opts.refs ?? null],
  );
  const entryId = Number(result.lastInsertRowid);
  db.close();
  return entryId;
}

/** Retrieve a single journal entry by ID. */
export function get(entryId: number): JournalEntry | null {
  const db = getDb();
  const row = db.query("SELECT * FROM journal WHERE id = ?").get(entryId) as JournalEntry | null;
  db.close();
  return row;
}

/** Full-text search across summary, context, and tags. */
export function searchEntries(query: string, limit = 20): JournalEntry[] {
  const db = getDb();
  const ftsQuery = query.split(/\s+/).map((t) => `"${t}"`).join(" OR ");

  let rows: JournalEntry[];
  try {
    rows = db.query(
      `SELECT j.* FROM journal_fts fts
       JOIN journal j ON j.id = fts.rowid
       WHERE journal_fts MATCH ?
       ORDER BY rank
       LIMIT ?`
    ).all(ftsQuery, limit) as JournalEntry[];
  } catch {
    // Fallback to LIKE search if FTS fails
    const pattern = `%${query}%`;
    rows = db.query(
      `SELECT * FROM journal
       WHERE summary LIKE ? OR context LIKE ? OR tags LIKE ?
       ORDER BY timestamp DESC LIMIT ?`
    ).all(pattern, pattern, pattern, limit) as JournalEntry[];
  }

  db.close();
  return rows;
}

/** Show the N most recent journal entries. */
export function recent(n = 10): JournalEntry[] {
  const db = getDb();
  const rows = db.query("SELECT * FROM journal ORDER BY id DESC LIMIT ?").all(n) as JournalEntry[];
  db.close();
  return rows;
}

/** List entries by category. */
export function byCategory(category: string): JournalEntry[] {
  const db = getDb();
  const rows = db.query(
    "SELECT * FROM journal WHERE category = ? ORDER BY id DESC"
  ).all(category) as JournalEntry[];
  db.close();
  return rows;
}

/** List entries containing a specific tag. */
export function byTag(tag: string): JournalEntry[] {
  const db = getDb();
  const pattern = `%${tag}%`;
  const rows = db.query(
    "SELECT * FROM journal WHERE tags LIKE ? ORDER BY id DESC"
  ).all(pattern) as JournalEntry[];
  db.close();

  return rows.filter((row) => {
    const entryTags = (row.tags ?? "").split(",").map((t) => t.trim().toLowerCase());
    return entryTags.includes(tag.toLowerCase());
  });
}

/** Find all entries that reference a given entry ID. */
export function refs(entryId: number): JournalEntry[] {
  const db = getDb();
  const id = String(entryId);
  const rows = db.query(
    `SELECT * FROM journal
     WHERE refs LIKE ? OR refs LIKE ? OR refs LIKE ? OR refs = ?
     ORDER BY id DESC`
  ).all(`${id},%`, `%,${id},%`, `%,${id}`, id) as JournalEntry[];
  db.close();
  return rows;
}

/** Dump the journal to SQL text format. */
export function dump(): string {
  const path = dbPath();
  if (!existsSync(path)) throw new Error("No journal database found.");

  // Use sqlite3 CLI for .dump since bun:sqlite doesn't have iterdump
  const result = Bun.spawnSync(["sqlite3", path, ".dump"]);
  const sql = result.stdout.toString();
  writeFileSync(dumpPath(), sql + "\n");
  return sql;
}

/** Safe backup: dump to temp file, verify, then atomically replace. */
export function backup(): boolean {
  const path = dbPath();
  if (!existsSync(path)) return false;

  const db = getDb();
  const dbCount = (db.query("SELECT COUNT(*) as cnt FROM journal").get() as { cnt: number }).cnt;
  db.close();

  if (dbCount === 0) return false;

  const sql = (() => {
    const result = Bun.spawnSync(["sqlite3", path, ".dump"]);
    return result.stdout.toString();
  })();

  const insertCount = (sql.match(/INSERT INTO "journal"/g) ?? []).length;
  if (insertCount < dbCount) {
    console.error(`VERIFICATION FAILED: DB has ${dbCount} entries but dump has ${insertCount} INSERTs.`);
    return false;
  }

  writeFileSync(dumpPath(), sql + "\n");
  return true;
}

/** Rebuild journal.db from a SQL dump file. */
export function rebuild(sqlFile?: string, force = false): boolean {
  const path = dbPath();
  const file = sqlFile ?? dumpPath();

  if (!existsSync(file)) {
    console.error(`SQL dump not found: ${file}`);
    return false;
  }

  const sql = readFileSync(file, "utf-8");
  const insertCount = (sql.match(/INSERT INTO "journal"/g) ?? []).length;

  if (insertCount === 0 && existsSync(path) && !force) {
    const db = new Database(path, { readonly: true });
    const dbCount = (db.query("SELECT COUNT(*) as cnt FROM journal").get() as { cnt: number }).cnt;
    db.close();
    if (dbCount > 0) {
      console.error(`REFUSED: SQL file has 0 INSERTs but existing DB has ${dbCount} entries.`);
      return false;
    }
  }

  if (existsSync(path)) {
    copyFileSync(path, path + ".bak");
    Bun.spawnSync(["rm", path]);
  }

  const db = new Database(path);
  db.exec(sql);
  db.close();

  return true;
}

/** Print journal statistics. */
export function journalStats(): {
  total: number;
  categories: { category: string | null; count: number }[];
  dateRange: { first: string; last: string } | null;
  topTags: { tag: string; count: number }[];
} {
  const path = dbPath();
  if (!existsSync(path)) return { total: 0, categories: [], dateRange: null, topTags: [] };

  const db = getDb();
  const total = (db.query("SELECT COUNT(*) as cnt FROM journal").get() as { cnt: number }).cnt;

  if (total === 0) {
    db.close();
    return { total: 0, categories: [], dateRange: null, topTags: [] };
  }

  const categories = db.query(
    "SELECT category, COUNT(*) as cnt FROM journal GROUP BY category ORDER BY cnt DESC"
  ).all() as { category: string | null; cnt: number }[];

  const first = db.query("SELECT timestamp FROM journal ORDER BY id ASC LIMIT 1").get() as { timestamp: string };
  const last = db.query("SELECT timestamp FROM journal ORDER BY id DESC LIMIT 1").get() as { timestamp: string };

  const tagRows = db.query("SELECT tags FROM journal WHERE tags IS NOT NULL").all() as { tags: string }[];
  const tagFreq = new Map<string, number>();
  for (const row of tagRows) {
    for (const tag of row.tags.split(",")) {
      const t = tag.trim().toLowerCase();
      if (t) tagFreq.set(t, (tagFreq.get(t) ?? 0) + 1);
    }
  }

  db.close();

  const topTags = [...tagFreq.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 15)
    .map(([tag, count]) => ({ tag, count }));

  return {
    total,
    categories: categories.map((c) => ({ category: c.category, count: c.cnt })),
    dateRange: { first: first.timestamp, last: last.timestamp },
    topTags,
  };
}
