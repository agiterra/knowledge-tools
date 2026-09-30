import { describe, expect, test } from "bun:test";
import { isVectorServiceAbsent, vectorErrorLine } from "../src/association-search";

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
