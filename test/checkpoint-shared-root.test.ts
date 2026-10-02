// checkpoint.sh must not write a SHARED root's vault that it reached only through the cwd (Brioche 649049).
// Refuse path AND accept paths: a guard proven only by refusals could be refusing everything.
import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, rmSync, realpathSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const CHECKPOINT = join(import.meta.dir, "..", "scripts", "checkpoint.sh");
const JOURNAL = join(import.meta.dir, "..", "scripts", "journal.py");

function git(cwd: string, ...args: string[]) {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`git ${args.join(" ")} failed: ${r.stderr}`);
  return r.stdout.trim();
}

function makeProject(root: string, name: string): string {
  const proj = join(root, name);
  mkdirSync(join(proj, ".knowledge", "meta"), { recursive: true });
  writeFileSync(join(proj, ".knowledge", "meta", "session-state.md"), "# state\n");
  git(proj, "init", "-q");
  git(proj, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init");
  writeFileSync(join(proj, ".knowledge", "note.md"), "new\n"); // something to commit
  return proj;
}

function commits(proj: string): number {
  return Number(git(proj, "rev-list", "--count", "HEAD"));
}

function run(cwd: string, env: Record<string, string | undefined>) {
  const e: Record<string, string> = { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "",
    GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
  for (const [k, v] of Object.entries(env)) if (v !== undefined) e[k] = v;
  return spawnSync("bash", [CHECKPOINT, "--cwd", cwd, "--no-push"], { cwd, env: e, encoding: "utf8" });
}

describe("checkpoint shared-root guard", () => {
  let root: string, shared: string, other: string, roots: string;
  beforeEach(() => {
    root = realpathSync(mkdtempSync(join(tmpdir(), "kt-sr-")));
    shared = makeProject(root, "shared");
    other = makeProject(root, "other");
    roots = join(root, "roots");
    writeFileSync(roots, `# shared checkouts\n${shared}  \n\n`);
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));

  test("REFUSES: cwd-resolved vault of a listed root — no commit, no export, one stderr line", () => {
    // A real journal.db, so the export step WOULD write journal.sql if the guard let it run.
    const jenv = { ...process.env, KNOWLEDGE_VAULT: join(shared, ".knowledge") };
    for (const a of [["init"], ["add", "test", "s", "c"]]) {
      const j = spawnSync("python3", [JOURNAL, ...a], { env: jenv, encoding: "utf8" });
      if (j.status !== 0) throw new Error(`journal.py ${a[0]} failed: ${j.stderr}`);
    }
    const r = run(shared, { KNOWLEDGE_SHARED_ROOTS_FILE: roots, KNOWLEDGE_VAULT: undefined });
    // Stateful assertions FIRST: with the guard removed these are what must fail, not just the message.
    expect(existsSync(join(shared, ".knowledge", "journal.sql"))).toBe(false);
    expect(commits(shared)).toBe(1);
    expect(git(shared, "diff", "--cached", "--name-only")).toBe(""); // nothing staged either
    expect(r.status).toBe(0);
    expect(r.stderr).toContain("SHARED root");
  });

  test("REFUSES via a subdirectory cwd? no — vault resolves under the subdir, which is not listed", () => {
    // The guard keys on the vault's PROJECT dir. A cwd below the root resolves a different (absent) vault
    // and checkpoint exits on the missing dir before the guard; this pins that the guard is not the reason.
    mkdirSync(join(shared, "sub"));
    const r = run(join(shared, "sub"), { KNOWLEDGE_SHARED_ROOTS_FILE: roots });
    expect(r.status).toBe(0);
    expect(r.stderr).not.toContain("SHARED root");
    expect(commits(shared)).toBe(1);
  });

  test("ACCEPTS: explicit absolute KNOWLEDGE_VAULT, even pointing into a listed root", () => {
    const r = run(other, { KNOWLEDGE_SHARED_ROOTS_FILE: roots, KNOWLEDGE_VAULT: join(shared, ".knowledge") });
    expect(r.stderr).not.toContain("SHARED root");
    expect(commits(shared)).toBe(2);
  });

  test("ACCEPTS: cwd-resolved vault of an UNLISTED project", () => {
    const r = run(other, { KNOWLEDGE_SHARED_ROOTS_FILE: roots });
    expect(r.stderr).not.toContain("SHARED root");
    expect(commits(other)).toBe(2);
  });

  test("ACCEPTS: no roots file at all (guard is opt-in host data)", () => {
    const r = run(shared, { KNOWLEDGE_SHARED_ROOTS_FILE: join(root, "absent") });
    expect(r.stderr).not.toContain("SHARED root");
    expect(commits(shared)).toBe(2);
  });

  test("REFUSES through a symlinked cwd (listed path compared physically)", () => {
    const link = join(root, "link");
    spawnSync("ln", ["-s", shared, link]);
    const r = run(link, { KNOWLEDGE_SHARED_ROOTS_FILE: roots });
    expect(commits(shared)).toBe(1);
    expect(r.stderr).toContain("SHARED root");
  });
});
