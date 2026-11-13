// checkpoint.sh: a REFUSED commit (pre-commit hook, e.g. gitleaks) must not strand commits that already exist.
// 2026-11-13: under `set -e` the refused commit exited before the push step; baguette's vault went 22 commits ahead.
import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync, chmodSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const CHECKPOINT = join(import.meta.dir, "..", "scripts", "checkpoint.sh");
const ENV: Record<string, string> = { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "",
  GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t",
  GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1" };

function git(cwd: string, ...args: string[]) {
  const r = spawnSync("git", args, { cwd, env: ENV, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`git ${args.join(" ")} failed: ${r.stderr}`);
  return r.stdout.trim();
}
const run = (proj: string) => spawnSync("bash", [CHECKPOINT, "--cwd", proj], { cwd: proj, env: ENV, encoding: "utf8" });
const remoteHead = (bare: string) => git(bare, "rev-parse", "main");
const ahead = (proj: string) => Number(git(proj, "rev-list", "--count", "@{upstream}..HEAD"));

describe("checkpoint: refused commit", () => {
  let root: string, bare: string, proj: string;
  beforeEach(() => {
    root = realpathSync(mkdtempSync(join(tmpdir(), "kt-cr-")));
    bare = join(root, "remote.git");
    git(root, "init", "-q", "--bare", "-b", "main", bare);
    proj = join(root, "proj");
    mkdirSync(join(proj, ".knowledge", "meta"), { recursive: true });
    writeFileSync(join(proj, ".knowledge", "meta", "session-state.md"), "# state\n");
    git(proj, "init", "-q", "-b", "main");
    git(proj, "add", "-A");
    git(proj, "commit", "-q", "-m", "init");
    git(proj, "remote", "add", "origin", bare);
    git(proj, "push", "-q", "-u", "origin", "main");
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));

  function refuseCommits() {
    const hook = join(proj, ".git", "hooks", "pre-commit");
    writeFileSync(hook, "#!/bin/sh\necho 'BLOCKED: fake gitleaks' >&2\nexit 1\n");
    chmodSync(hook, 0o755);
  }
  function earlierCommits(n: number) {
    for (let i = 0; i < n; i++) {
      writeFileSync(join(proj, ".knowledge", `old${i}.md`), `${i}\n`);
      git(proj, "add", "-A");
      git(proj, "commit", "-q", "-m", `old ${i}`);
    }
  }

  test("refused commit still pushes the earlier unpushed commits, exits 1, says COMMIT REFUSED", () => {
    earlierCommits(2);
    const local = git(proj, "rev-parse", "HEAD");
    refuseCommits();
    writeFileSync(join(proj, ".knowledge", "secret.md"), "new\n");
    const r = run(proj);
    // Stateful first: with the fix reverted the remote stays at init and these fail.
    expect(remoteHead(bare)).toBe(local);
    expect(ahead(proj)).toBe(0);
    expect(git(proj, "diff", "--cached", "--name-only")).toContain(".knowledge/secret.md");
    expect(r.status).toBe(1);
    expect(r.stdout).toContain("COMMIT REFUSED; 2 earlier commit(s)");
    expect(r.stdout).toContain("pushed");
    expect(r.stderr).toContain("git commit REFUSED");
  });

  test("refused commit with nothing earlier: no push, exits 1", () => {
    const before = remoteHead(bare);
    refuseCommits();
    writeFileSync(join(proj, ".knowledge", "secret.md"), "new\n");
    const r = run(proj);
    expect(remoteHead(bare)).toBe(before);
    expect(r.status).toBe(1);
    expect(r.stdout).toContain("COMMIT REFUSED; 0 earlier commit(s)");
    expect(r.stdout).toContain("nothing to push");
  });

  test("accept path: a normal commit commits, pushes, exits 0", () => {
    writeFileSync(join(proj, ".knowledge", "note.md"), "new\n");
    const r = run(proj);
    expect(r.status).toBe(0);
    expect(remoteHead(bare)).toBe(git(proj, "rev-parse", "HEAD"));
    expect(r.stdout).toContain("vault committed at");
    expect(r.stdout).toContain("- pushed");
  });
});
