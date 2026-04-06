#!/usr/bin/env python3
"""Sonnet value-judgment filter for associative retrieval.

Takes raw keyword associations + optional conversation context, calls Sonnet
via direct API (stdlib only) to filter down to the 2-4 that actually connect.

The keyword layer casts a wide net (15+ hits). Sonnet makes the value call:
"Given what the agent is currently discussing, which of these raw hits are
actually relevant and why?"

Usage as library:
    from sonnet_filter import filter_associations
    filtered = filter_associations(
        raw_associations=[...],   # from association-search.py
        event_text="...",         # the triggering event
        opus_context="...",       # summary of current conversation (optional)
    )

Usage as CLI:
    python3 scripts/sonnet-filter.py "event text"
    python3 scripts/sonnet-filter.py "event text" "conversation context"

Requires ANTHROPIC_API_KEY in environment or .env file in CWD.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(os.getcwd(), '.env')

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ASSOCIATION_FILTER_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1024
TIMEOUT = int(os.environ.get("ASSOCIATION_FILTER_TIMEOUT", "25"))

# ---------------------------------------------------------------------------
# API key loading
# ---------------------------------------------------------------------------

_api_key_cache = None


def _get_api_key():
    """Load Anthropic API key from environment or .env file."""
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache

    # Check environment first
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        _api_key_cache = key
        return key

    # Fall back to .env file in CWD
    if os.path.exists(ENV_FILE):
        try:
            for line in open(ENV_FILE):
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                    if key:
                        _api_key_cache = key
                        return key
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Sonnet API call (direct HTTP, no SDK dependency)
# ---------------------------------------------------------------------------

def _call_sonnet(prompt, system_prompt=None):
    """Call Sonnet via direct API.

    Returns the text response or None on error, plus metrics dict.
    """
    api_key = _get_api_key()
    if not api_key:
        return None, {"error": "no_api_key"}

    messages = [{"role": "user", "content": prompt}]

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
    }

    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    t0 = time.time()
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(API_URL, data=data, headers=headers)
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        result = json.loads(resp.read())

        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        usage = result.get("usage", {})
        metrics = {
            "latency_ms": round((time.time() - t0) * 1000, 2),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "model": result.get("model", MODEL),
        }
        return text, metrics

    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return None, {
            "error": f"http_{e.code}",
            "detail": body[:200],
            "latency_ms": round((time.time() - t0) * 1000, 2),
        }
    except Exception as e:
        return None, {
            "error": str(e),
            "latency_ms": round((time.time() - t0) * 1000, 2),
        }


# ---------------------------------------------------------------------------
# Association formatting
# ---------------------------------------------------------------------------

def _format_associations(associations):
    """Format raw associations for Sonnet's consumption."""
    lines = []
    for i, a in enumerate(associations, 1):
        source = a.get("source", "?")
        atype = a.get("type", "?")
        summary = a.get("summary", "")[:300]
        score = a.get("score", a.get("normalized_score", 0))
        keywords = ", ".join(a.get("matched_keywords", [])[:8])
        lines.append(
            f"{i}. [{atype}] {source} (keyword score: {score:.2f})\n"
            f"   Summary: {summary}\n"
            f"   Matched: {keywords}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Main filter function
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a relevance filter for an AI agent's associative memory system.

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

Be aggressive about filtering. 15 keyword hits should become 2-3 genuine connections. If nothing is truly relevant, return an empty filtered array. Speed over thoroughness — make fast judgment calls."""


def filter_associations(raw_associations, event_text, opus_context=None, top_k=4):
    """Filter raw keyword associations through Sonnet value judgment.

    Args:
        raw_associations: List of dicts from association-search.py
        event_text: The triggering event text
        opus_context: Summary of current conversation (optional but valuable)
        top_k: Max associations to return (default 4)

    Returns:
        dict with 'filtered' (list), 'metrics' (dict), 'raw_count' (int)
        Each filtered item includes original association data plus
        sonnet_relevance and sonnet_reason fields.
    """
    metrics = {"raw_count": len(raw_associations)}
    t0 = time.time()

    if not raw_associations:
        metrics["total_ms"] = 0
        return {"filtered": [], "metrics": metrics, "raw_count": 0}

    # Build the prompt
    formatted = _format_associations(raw_associations)

    prompt_parts = [f"## Event\n{event_text}"]

    if opus_context:
        prompt_parts.append(f"## Current Conversation Context\n{opus_context}")

    prompt_parts.append(
        f"## Raw Associations ({len(raw_associations)} keyword matches)\n{formatted}"
    )

    prompt_parts.append(
        f"\nFilter to the {top_k} most genuinely relevant associations "
        f"given the event and conversation context. Return JSON only."
    )

    prompt = "\n\n".join(prompt_parts)
    metrics["prompt_tokens_est"] = len(prompt.split())

    # Call Sonnet
    response_text, call_metrics = _call_sonnet(prompt, system_prompt=SYSTEM_PROMPT)
    metrics["sonnet_call"] = call_metrics

    if response_text is None:
        metrics["total_ms"] = round((time.time() - t0) * 1000, 2)
        metrics["fallback"] = "no_response"
        # Fallback: return top raw associations by score
        top = sorted(raw_associations, key=lambda x: -x.get("score", x.get("normalized_score", 0)))
        return {
            "filtered": top[:top_k],
            "metrics": metrics,
            "raw_count": len(raw_associations),
        }

    # Parse Sonnet's response
    try:
        # Handle potential markdown fencing
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        result = json.loads(text)
        filtered_refs = result.get("filtered", [])
        dropped_reason = result.get("dropped_reason", "")

        # Match back to original associations by source
        source_map = {a["source"]: a for a in raw_associations}
        filtered = []
        for ref in filtered_refs[:top_k]:
            source = ref.get("source", "")
            if source in source_map:
                enriched = dict(source_map[source])
                enriched["sonnet_relevance"] = ref.get("relevance", "unknown")
                enriched["sonnet_reason"] = ref.get("reason", "")
                filtered.append(enriched)

        metrics["filtered_count"] = len(filtered)
        metrics["dropped_reason"] = dropped_reason
        metrics["total_ms"] = round((time.time() - t0) * 1000, 2)

        return {
            "filtered": filtered,
            "metrics": metrics,
            "raw_count": len(raw_associations),
        }

    except (json.JSONDecodeError, KeyError) as e:
        metrics["parse_error"] = str(e)
        metrics["raw_response"] = response_text[:200]
        metrics["total_ms"] = round((time.time() - t0) * 1000, 2)
        # Fallback: return top raw associations
        top = sorted(raw_associations, key=lambda x: -x.get("score", x.get("normalized_score", 0)))
        return {
            "filtered": top[:top_k],
            "metrics": metrics,
            "raw_count": len(raw_associations),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """\
Usage: sonnet-filter.py <event-text> [opus-context]

Runs association-search.py first to get raw keyword hits, then filters
through Sonnet for relevance.

Requires ANTHROPIC_API_KEY in environment or .env file in CWD.

Environment variables:
  ASSOCIATION_FILTER_MODEL    Model to use (default: claude-sonnet-4-6)
  ASSOCIATION_FILTER_TIMEOUT  API timeout in seconds (default: 25)
"""


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    event_text = sys.argv[1]
    opus_context = sys.argv[2] if len(sys.argv) > 2 else None

    # First get raw associations
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "association_search",
            os.path.join(SCRIPT_DIR, "association-search.py"),
        )
        if spec is None or spec.loader is None:
            print("Error: could not load association-search.py")
            sys.exit(1)
        assoc_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assoc_mod)
        raw = assoc_mod.search_associations(event_text, top_k=15)
    except Exception as e:
        print(f"Error loading association search: {e}")
        sys.exit(1)

    raw_assocs = raw.get("results", [])
    print(f"Raw keyword search: {len(raw_assocs)} hits in {raw['timing_ms']}ms")
    print(f"Keywords: {', '.join(raw.get('keywords', []))}")
    if raw.get("expanded_keywords"):
        print(f"Expanded: {', '.join(raw['expanded_keywords'])}")
    print()

    # Filter through Sonnet
    result = filter_associations(
        raw_associations=raw_assocs,
        event_text=event_text,
        opus_context=opus_context,
        top_k=4,
    )

    m = result["metrics"]
    sonnet = m.get("sonnet_call", {})

    if "error" in sonnet:
        print(f"Sonnet error: {sonnet['error']}")
        if "detail" in sonnet:
            print(f"  Detail: {sonnet['detail']}")
        print("\nFallback: returning top raw associations by keyword score")
    else:
        print(f"Sonnet filter: {m.get('filtered_count', '?')} kept from {m['raw_count']} raw")
        print(f"Sonnet latency: {sonnet.get('latency_ms', '?')}ms "
              f"({sonnet.get('input_tokens', '?')} in / {sonnet.get('output_tokens', '?')} out)")
        if m.get("dropped_reason"):
            print(f"Dropped because: {m['dropped_reason']}")

    print(f"Total pipeline: {m.get('total_ms', '?')}ms")
    print()

    for i, a in enumerate(result["filtered"], 1):
        source = a["source"]
        atype = a.get("type", "?")
        summary = a.get("summary", "")[:150]
        reason = a.get("sonnet_reason", "")
        relevance = a.get("sonnet_relevance", "")
        print(f"  {i}. [{atype}] {source}")
        print(f"     {summary}")
        if reason:
            print(f"     Sonnet: [{relevance}] {reason}")
        print()


if __name__ == "__main__":
    main()
