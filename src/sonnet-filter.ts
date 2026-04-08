/**
 * Sonnet value-judgment filter for associative retrieval.
 *
 * Takes raw keyword associations + optional conversation context, calls Sonnet
 * via direct API to filter down to the 2-4 that actually connect.
 */

import { existsSync, readFileSync } from "fs";
import { join } from "path";
import type { AssociationResult } from "./association-search";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const API_URL = "https://api.anthropic.com/v1/messages";
const MODEL = process.env.ASSOCIATION_FILTER_MODEL ?? "claude-sonnet-4-6";
const MAX_TOKENS = 1024;
const TIMEOUT = parseInt(process.env.ASSOCIATION_FILTER_TIMEOUT ?? "25", 10) * 1000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FilteredAssociation extends AssociationResult {
  sonnet_relevance?: string;
  sonnet_reason?: string;
}

export interface FilterResult {
  filtered: FilteredAssociation[];
  metrics: Record<string, unknown>;
  raw_count: number;
}

// ---------------------------------------------------------------------------
// API key loading
// ---------------------------------------------------------------------------

let apiKeyCache: string | null = null;

function getApiKey(): string | null {
  if (apiKeyCache) return apiKeyCache;

  const envKey = process.env.ANTHROPIC_API_KEY;
  if (envKey) {
    apiKeyCache = envKey;
    return envKey;
  }

  const envFile = join(process.cwd(), ".env");
  if (existsSync(envFile)) {
    try {
      for (const line of readFileSync(envFile, "utf-8").split("\n")) {
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
      // ignore
    }
  }

  return null;
}

// ---------------------------------------------------------------------------
// Sonnet API call
// ---------------------------------------------------------------------------

async function callSonnet(
  prompt: string,
  systemPrompt?: string,
): Promise<{ text: string | null; metrics: Record<string, unknown> }> {
  const apiKey = getApiKey();
  if (!apiKey) return { text: null, metrics: { error: "no_api_key" } };

  const payload: Record<string, unknown> = {
    model: MODEL,
    max_tokens: MAX_TOKENS,
    messages: [{ role: "user", content: prompt }],
  };
  if (systemPrompt) payload.system = systemPrompt;

  const t0 = performance.now();
  try {
    const resp = await fetch(API_URL, {
      method: "POST",
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(TIMEOUT),
    });

    const result = await resp.json() as {
      content?: { type: string; text: string }[];
      usage?: { input_tokens: number; output_tokens: number };
      model?: string;
    };

    const text = (result.content ?? [])
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");

    return {
      text,
      metrics: {
        latency_ms: +(performance.now() - t0).toFixed(2),
        input_tokens: result.usage?.input_tokens ?? 0,
        output_tokens: result.usage?.output_tokens ?? 0,
        model: result.model ?? MODEL,
      },
    };
  } catch (e) {
    return {
      text: null,
      metrics: {
        error: String(e),
        latency_ms: +(performance.now() - t0).toFixed(2),
      },
    };
  }
}

// ---------------------------------------------------------------------------
// Association formatting
// ---------------------------------------------------------------------------

function formatAssociations(associations: AssociationResult[]): string {
  return associations
    .map((a, i) => {
      const summary = (a.summary ?? "").slice(0, 300);
      const score = a.normalized_score ?? a.score ?? 0;
      const keywords = (a.matched_keywords ?? []).slice(0, 8).join(", ");
      return `${i + 1}. [${a.type}] ${a.source} (keyword score: ${score.toFixed(2)})\n   Summary: ${summary}\n   Matched: ${keywords}`;
    })
    .join("\n\n");
}

// ---------------------------------------------------------------------------
// System prompt
// ---------------------------------------------------------------------------

const SYSTEM_PROMPT = `You are a relevance filter for an AI agent's associative memory system.

You receive:
1. An event (new message or trigger) that the agent needs to respond to
2. A summary of what the agent is currently discussing (conversation context)
3. Raw keyword-matched associations from the agent's memory (journal entries, vault files)

Your job: identify which 1-4 associations are genuinely relevant given the CURRENT conversation context. Not just keyword matches — thematic connections, useful background, things that would change how the agent responds.

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

Be aggressive about filtering. 15 keyword hits should become 2-3 genuine connections. If nothing is truly relevant, return an empty filtered array. Speed over thoroughness — make fast judgment calls.`;

// ---------------------------------------------------------------------------
// Main filter function
// ---------------------------------------------------------------------------

export async function filterAssociations(
  rawAssociations: AssociationResult[],
  eventText: string,
  opusContext?: string,
  topK = 4,
): Promise<FilterResult> {
  const metrics: Record<string, unknown> = { raw_count: rawAssociations.length };
  const t0 = performance.now();

  if (rawAssociations.length === 0) {
    metrics.total_ms = 0;
    return { filtered: [], metrics, raw_count: 0 };
  }

  const formatted = formatAssociations(rawAssociations);
  const promptParts = [`## Event\n${eventText}`];
  if (opusContext) promptParts.push(`## Current Conversation Context\n${opusContext}`);
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
      if (text.endsWith("```")) text = text.slice(0, -3);
      text = text.trim();
    }

    const result = JSON.parse(text) as {
      filtered?: { source: string; relevance?: string; reason?: string }[];
      dropped_reason?: string;
    };

    const sourceMap = new Map(rawAssociations.map((a) => [a.source, a]));
    const filtered: FilteredAssociation[] = [];

    for (const ref of (result.filtered ?? []).slice(0, topK)) {
      const original = sourceMap.get(ref.source);
      if (original) {
        filtered.push({
          ...original,
          sonnet_relevance: ref.relevance ?? "unknown",
          sonnet_reason: ref.reason ?? "",
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
