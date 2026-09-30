import { describe, expect, test } from "bun:test";
import { isVectorServiceAbsent, vectorErrorLine, vectorServiceOff } from "../src/association-search";

const URL = "http://127.0.0.1:9801";
const refusedBun = Object.assign(new Error("Unable to connect. Is the computer able to access the url?"), { code: "ConnectionRefused" });
const refusedNode = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
const timeout = Object.assign(new Error("The operation timed out."), { name: "TimeoutError", code: 23 });

describe("vector service absence", () => {
  test("refused connection = absent (Bun and Node shapes)", () => {
    expect(isVectorServiceAbsent(refusedBun)).toBe(true);
    expect(isVectorServiceAbsent(refusedNode)).toBe(true);
  });
  test("timeouts, HTTP/JSON errors and non-errors are NOT absence", () => {
    expect(isVectorServiceAbsent(timeout)).toBe(false);
    expect(isVectorServiceAbsent(new SyntaxError("Unexpected token <"))).toBe(false);
    expect(isVectorServiceAbsent(null)).toBe(false);
    expect(isVectorServiceAbsent("ConnectionRefused")).toBe(false);
  });
  test("absence: said once, then silent", () => {
    expect(vectorErrorLine(refusedBun, URL, false)).toContain("no vector service at http://127.0.0.1:9801");
    expect(vectorErrorLine(refusedBun, URL, true)).toBeNull();
  });
  test("a real error logs every time, even after absence was noted", () => {
    expect(vectorErrorLine(timeout, URL, false)).toContain("vector service error");
    expect(vectorErrorLine(timeout, URL, true)).toContain("vector service error");
  });
});

test("live: searchVectors against a closed port says the absence once and returns []", async () => {
  const proc = Bun.spawn(["bun", "-e", `
    process.env.VECTOR_SERVICE_URL = "http://127.0.0.1:1";
    const m = await import("${import.meta.dir}/../src/association-search.ts");
    const a = await m.searchVectors("x", 3); const b = await m.searchVectors("y", 3);
    console.log(JSON.stringify([a.length, b.length]));
  `], { stderr: "pipe", stdout: "pipe", env: { ...process.env, KNOWLEDGE_VAULT: process.env.KNOWLEDGE_VAULT ?? "/Users/fondant/Fondant/.knowledge" } });
  const [out, err] = [await new Response(proc.stdout).text(), await new Response(proc.stderr).text()];
  await proc.exited;
  expect(out.trim()).toBe("[0,0]");
  expect(err.match(/no vector service at/g)?.length ?? 0).toBe(1);
  expect(err).not.toContain("vector service unavailable");
});

async function runSearch(url: string): Promise<{ out: string; err: string }> {
  const proc = Bun.spawn(["bun", "-e", `
    process.env.VECTOR_SERVICE_URL = ${JSON.stringify(url)};
    const m = await import("${import.meta.dir}/../src/association-search.ts");
    const v = await m.searchVectors("supervisor restart", 3);
    const all = await m.searchAssociations("supervisor restart host watches");
    console.log(JSON.stringify([v.length, all.results.length]));
  `], { stderr: "pipe", stdout: "pipe", env: { ...process.env, KNOWLEDGE_VAULT: process.env.KNOWLEDGE_VAULT ?? "/Users/fondant/Fondant/.knowledge" } });
  const [out, err] = [await new Response(proc.stdout).text(), await new Response(proc.stderr).text()];
  await proc.exited;
  return { out: out.trim(), err };
}

describe("VECTOR_SERVICE_URL=off", () => {
  test("switch parses off case-insensitively, nothing else", () => {
    expect(vectorServiceOff("off")).toBe(true);
    expect(vectorServiceOff(" OFF ")).toBe(true);
    expect(vectorServiceOff("http://127.0.0.1:9801")).toBe(false);
    expect(vectorServiceOff("")).toBe(false);
  });
  test("separate per-event processes: silent, keyword results kept", async () => {
    for (let i = 0; i < 3; i++) {
      const { out, err } = await runSearch("off");
      const [v, all] = JSON.parse(out);
      expect(v).toBe(0);
      expect(all).toBeGreaterThan(0);
      expect(err).not.toContain("[assoc]");
    }
  });
  test("a configured URL is still called (a live stub answers with a result)", async () => {
    const srv = Bun.serve({ port: 0, fetch: () => Response.json({ results: [{ source: "stub.md", type: "file", summary: "s", score: 0.9 }] }) });
    try {
      const { out, err } = await runSearch(`http://127.0.0.1:${srv.port}`);
      expect(JSON.parse(out)[0]).toBe(1);
      expect(err).not.toContain("[assoc]");
    } finally { srv.stop(true); }
  });
  test("other errors stay visible when not off (HTTP 500 -> bad JSON)", async () => {
    const srv = Bun.serve({ port: 0, fetch: () => new Response("<html>boom</html>", { status: 500 }) });
    try {
      const a = await runSearch(`http://127.0.0.1:${srv.port}`);
      expect(a.err).toContain("vector service error");
    } finally { srv.stop(true); }
  });
});
