"""Fetch and parse Artificial Analysis Coding Agent Index data via Firecrawl API.

Reads the published structured dataset embedded behind the
"Artificial Analysis Coding Agent Index vs. Cost per Task" chart.
Never estimates scores or costs from chart images.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import (
    ALLOWED_EFFORTS,
    DEFAULT_SOURCE_URL,
    MAX_HTTP_ERROR_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    NO_REDIRECT_OPENER,
    normalize_cost as _normalize_cost,
    normalize_score as _normalize_score,
    redact_secrets,
    resolve_secret,
    sanitize_single_line,
    sanitize_url_for_log,
    validate_https_url as _validate_https_url,
)
from .models import BenchmarkSnapshot, VariantRecord

FIRECRAWL_EXTRACT_JS = r"""
(() => {
  const scripts = [...document.querySelectorAll('script')].map(s => s.textContent || '');
  const flightChunks = [];
  for (const t of scripts) {
    if (!t.includes('__next_f.push')) continue;
    const m = t.match(/push\(\[\d+,"([\s\S]*)"\]\)/);
    if (!m) continue;
    try { flightChunks.push(JSON.parse('"' + m[1] + '"')); } catch (e) {}
  }
  const full = flightChunks.join('');
  const chunkMap = new Map();
  for (const line of full.split('\n')) {
    const colon = line.indexOf(':');
    if (colon <= 0 || colon > 8) continue;
    const id = line.slice(0, colon);
    if (!/^[0-9a-fA-F]+$/.test(id)) continue;
    const body = line.slice(colon + 1);
    if (body.startsWith('{') || body.startsWith('[')) {
      try { chunkMap.set(id, JSON.parse(body)); } catch (e) {}
    }
  }
  function resolve(node, seen = new Set()) {
    if (typeof node === 'string' && /^\$[0-9a-fA-F]+$/.test(node)) {
      const id = node.slice(1);
      if (chunkMap.has(id) && !seen.has(id)) {
        const nextSeen = new Set(seen);
        nextSeen.add(id);
        return resolve(chunkMap.get(id), nextSeen);
      }
      return node;
    }
    if (Array.isArray(node)) return node.map(x => resolve(x, seen));
    if (node && typeof node === 'object') {
      const out = {};
      for (const [k, v] of Object.entries(node)) out[k] = resolve(v, seen);
      return out;
    }
    return node;
  }
  const byId = new Map();
  function walk(node, depth = 0) {
    if (depth > 15 || !node || typeof node !== 'object') return;
    if (Array.isArray(node)) { for (const x of node) walk(x, depth + 1); return; }
    if (typeof node.id === 'string' && typeof node.agentSlug === 'string' && 'indexComponentCount' in node) {
      if (!byId.has(node.id)) byId.set(node.id, node);
    }
    for (const v of Object.values(node)) walk(v, depth + 1);
  }
  for (const val of chunkMap.values()) walk(resolve(val));
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const verMatch = bodyText.match(/Coding Agent Index (v[0-9.]+)/i);
  const items = [...byId.values()].map(r => ({
    id: r.id,
    agentName: r.agentName || '',
    agentSlug: r.agentSlug || '',
    provider: r.provider || '',
    shortLabel: r.shortLabel || '',
    displayLabel: r.displayLabel || '',
    modelRelease: (r.model && r.model.release && r.model.release.shortName) || '',
    modelName: (r.model && r.model.name) || '',
    effortSlug: (r.model && r.model.effort && r.model.effort.slug) || '',
    effortLabel: (r.model && r.model.effort && r.model.effort.label) || '',
    creators: Array.isArray(r.modelCreators)
      ? r.modelCreators.map(c => ({ slug: c.slug || '', name: c.name || '' }))
      : [],
    isUnavailable: Boolean(r.isUnavailable),
    isRouter: Boolean(r.isRouter),
    hasTieredPricing: Boolean(r.hasTieredPricing),
    hasReasoningNote: Boolean(r.hasReasoningNote),
    indexComponentCount: r.indexComponentCount ?? null,
    evalCount: r.evalCount ?? null,
    indexScore: typeof r.indexScore === 'number' ? r.indexScore : null,
    meanCostUsd: (r.mean && typeof r.mean.costUsd === 'number') ? r.mean.costUsd : null
  }));
  const payload = {
    indexVersion: verMatch ? verMatch[1] : 'unknown',
    count: items.length,
    items
  };
  document.body.innerHTML = '<pre id="aa-extracted">' + JSON.stringify(payload).replace(/</g, '&lt;') + '</pre>';
})();
"""


class ScrapeError(RuntimeError):
    """Raised when Artificial Analysis data cannot be fetched or parsed reliably."""


def _normalize_optional_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_effort(item: dict[str, Any]) -> str:
    """Extract and validate effort against ALLOWED_EFFORTS to prevent injection."""
    for key in ("effortSlug", "effortLabel", "effort"):
        val = str(item.get(key) or "").strip().lower()
        if val in ALLOWED_EFFORTS:
            return val
    label = str(item.get("shortLabel") or item.get("displayLabel") or "")
    m = re.search(r"\((low|medium|high|xhigh|max|minimal|none|ultra)\b", label, re.I)
    if m and m.group(1).lower() in ALLOWED_EFFORTS:
        return m.group(1).lower()
    return "default"


def parse_extracted_payload(
    payload: dict[str, Any],
    source_url: str = DEFAULT_SOURCE_URL,
    fetched_at_utc: str | None = None,
) -> BenchmarkSnapshot:
    """Convert extracted Artificial Analysis JSON payload into a validated BenchmarkSnapshot."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ScrapeError("Extracted payload missing 'items' list")

    ts = fetched_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_version = sanitize_single_line(
        str(payload.get("indexVersion") or payload.get("index_version") or "unknown"),
        max_len=40,
    )
    variants: list[VariantRecord] = []

    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        vid = sanitize_single_line(str(item.get("id") or item.get("variant_id") or ""), max_len=128)
        if not vid:
            continue

        creators = item.get("creators")
        if isinstance(creators, list) and creators and isinstance(creators[0], dict):
            creator_slug = sanitize_single_line(str(creators[0].get("slug") or ""), max_len=64).lower()
            creator_name = sanitize_single_line(str(creators[0].get("name") or creator_slug), max_len=80)
        else:
            creator_slug = sanitize_single_line(
                str(item.get("creator_slug") or item.get("provider") or "unknown"), max_len=64
            ).lower()
            creator_name = sanitize_single_line(str(item.get("creator_name") or creator_slug), max_len=80)

        agent_slug = sanitize_single_line(
            str(item.get("agentSlug") or item.get("agent_slug") or ""), max_len=64
        ).lower()
        agent_name = sanitize_single_line(
            str(item.get("agentName") or item.get("agent_name") or agent_slug), max_len=80
        )
        model_release = sanitize_single_line(
            str(item.get("modelRelease") or item.get("model_release") or ""), max_len=120
        )
        model_version = sanitize_single_line(
            str(item.get("modelName") or item.get("model_version") or model_release), max_len=160
        )
        effort = _extract_effort(item)

        score = _normalize_score(
            item.get("coding_agent_index")
            if "coding_agent_index" in item
            else item.get("indexScore")
        )
        cost = _normalize_cost(
            item.get("cost_per_task_usd")
            if "cost_per_task_usd" in item
            else item.get("meanCostUsd")
        )

        notes: list[str] = []
        if item.get("hasTieredPricing"):
            notes.append("tiered_pricing")
        if item.get("hasReasoningNote"):
            notes.append("reasoning_note")
        if item.get("isUnavailable"):
            notes.append("no_public_api")
        if item.get("isRouter"):
            notes.append("router_model")
        if cost is None:
            notes.append("missing_or_zero_cost")
        if score is None:
            notes.append("missing_score")

        variants.append(
            VariantRecord(
                variant_id=vid,
                creator_slug=creator_slug,
                creator_name=creator_name,
                model_release=model_release or model_version,
                model_version=model_version or model_release,
                agent_slug=agent_slug,
                agent_name=agent_name,
                effort=effort,
                coding_agent_index=score,
                cost_per_task_usd=cost,
                index_version=index_version,
                source_url=source_url,
                fetched_at_utc=ts,
                is_unavailable=bool(item.get("isUnavailable", False)),
                is_router=bool(item.get("isRouter", False)),
                eval_count=_normalize_optional_int(item.get("evalCount")),
                index_component_count=_normalize_optional_int(item.get("indexComponentCount")),
                cost_notes=notes,
                raw_label=sanitize_single_line(
                    str(item.get("shortLabel") or item.get("displayLabel") or ""), max_len=160
                ),
            )
        )

    if not variants:
        raise ScrapeError("Parsed zero variants from Artificial Analysis payload")

    return BenchmarkSnapshot(
        index_version=index_version,
        source_url=source_url,
        fetched_at_utc=ts,
        variants=variants,
        notes=[f"extracted_variants={len(variants)}"],
    )


def _extract_json_from_markdown(markdown_text: str) -> dict[str, Any]:
    clean = markdown_text.strip()
    if clean.startswith("```"):
        parts = clean.split("\n", 1)
        clean = parts[1] if len(parts) > 1 else clean
        if "```" in clean:
            clean = clean.rsplit("```", 1)[0]
    clean = clean.replace("\\\n", "\n").strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end <= start:
        raise ScrapeError("No JSON object found in Firecrawl markdown response")
    try:
        parsed = json.loads(clean[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"Invalid JSON in Firecrawl response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ScrapeError("Extracted Firecrawl JSON payload must be an object")
    return parsed


def _scrape_via_firecrawl_rest(
    source_url: str,
    api_url: str,
    api_key: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    source_url = _validate_https_url(source_url)
    api_url = _validate_https_url(api_url)
    body = {
        "url": source_url,
        "formats": ["markdown"],
        "onlyMainContent": False,
        "actions": [
            {"type": "wait", "milliseconds": 1200},
            {"type": "executeJavascript", "script": FIRECRAWL_EXTRACT_JS},
        ],
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with NO_REDIRECT_OPENER.open(req, timeout=timeout_seconds) as resp:
            raw_bytes = resp.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_HTTP_RESPONSE_BYTES:
                raise ScrapeError("Firecrawl API response exceeded size cap")
            raw = raw_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_bytes = exc.read(MAX_HTTP_ERROR_BYTES) if exc.fp else b""
        detail = redact_secrets(err_bytes.decode("utf-8", errors="replace")[:400])
        raise ScrapeError(f"Firecrawl API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ScrapeError(f"Firecrawl API request failed: {redact_secrets(str(exc))}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"Non-JSON response from Firecrawl API: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ScrapeError("Firecrawl API returned unexpected non-object JSON")
    if not parsed.get("success", True):
        raise ScrapeError(f"Firecrawl API returned error: {redact_secrets(str(parsed.get('error')))}")
    data = parsed.get("data")
    if data is None:
        data = parsed
    if not isinstance(data, dict):
        raise ScrapeError("Firecrawl API returned malformed 'data' field")
    md = data.get("markdown")
    if not isinstance(md, str) or not md.strip():
        raise ScrapeError("Firecrawl API returned empty markdown payload")
    return _extract_json_from_markdown(md)


def _scrape_via_bridge_command(
    source_url: str,
    bridge_command: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Invoke a user-configured Firecrawl bridge command (explicit argv list, no shell)."""
    source_url = _validate_https_url(source_url)
    if not isinstance(bridge_command, list) or not all(
        isinstance(p, str) and p for p in bridge_command
    ):
        raise ScrapeError("firecrawl_bridge_command must be a non-empty list of strings")

    call_payload = {
        "service": "firecrawl",
        "tool": "firecrawl_scrape",
        "arguments": {
            "url": source_url,
            "formats": ["markdown"],
            "onlyMainContent": False,
            "actions": [
                {"type": "wait", "milliseconds": 1200},
                {"type": "executeJavascript", "script": FIRECRAWL_EXTRACT_JS},
            ],
        },
    }
    cmd = [
        json.dumps(call_payload) if part == "{payload_json}" else part
        for part in bridge_command
    ]
    if "{payload_json}" not in bridge_command:
        cmd.append(json.dumps(call_payload))

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScrapeError(f"Firecrawl bridge command failed: {redact_secrets(str(exc))}") from exc

    if res.returncode != 0:
        raise ScrapeError(
            f"Firecrawl bridge exited {res.returncode}: {redact_secrets(res.stderr[-500:])}"
        )

    try:
        outer = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"Firecrawl bridge emitted non-JSON output: {exc}") from exc
    if not isinstance(outer, dict):
        raise ScrapeError("Firecrawl bridge returned non-object JSON")

    content = outer.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = str(content[0].get("text", ""))
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ScrapeError("Bridge wrapper did not contain JSON data")
        try:
            inner = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ScrapeError(f"Bridge wrapper inner JSON malformed: {exc}") from exc
        if not isinstance(inner, dict):
            raise ScrapeError("Bridge wrapper inner JSON is not an object")
        md = inner.get("markdown", "")
    else:
        data = outer.get("data")
        target = data if isinstance(data, dict) else outer
        md = target.get("markdown", "")

    if not isinstance(md, str) or not md.strip():
        raise ScrapeError("Firecrawl bridge returned empty markdown")
    return _extract_json_from_markdown(md)


def fetch_artificial_analysis_snapshot(config: dict[str, Any]) -> BenchmarkSnapshot:
    """Fetch live Coding Agent Index data from Artificial Analysis using Firecrawl."""
    src = config.get("source", {})
    source_url = _validate_https_url(str(src.get("url") or DEFAULT_SOURCE_URL))
    mode = str(src.get("firecrawl_mode") or "api").strip().lower()
    timeout_seconds = int(src.get("timeout_seconds", 90))

    if mode == "bridge_command":
        bridge_cmd = src.get("firecrawl_bridge_command")
        payload = _scrape_via_bridge_command(source_url, bridge_cmd, timeout_seconds)
    elif mode == "api":
        api_key = resolve_secret(
            env_var=src.get("firecrawl_api_key_env", "FIRECRAWL_API_KEY"),
            secret_command=src.get("firecrawl_api_key_command"),
        )
        if not api_key:
            raise ScrapeError(
                "Missing Firecrawl API key. Set FIRECRAWL_API_KEY or configure source.firecrawl_api_key_command."
            )
        api_url = str(src.get("firecrawl_api_url") or "https://api.firecrawl.dev/v2/scrape")
        payload = _scrape_via_firecrawl_rest(source_url, api_url, api_key, timeout_seconds)
    else:
        raise ScrapeError(f"Unsupported firecrawl_mode: {mode!r}")

    return parse_extracted_payload(payload, source_url=source_url)
