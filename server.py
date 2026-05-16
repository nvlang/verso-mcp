#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.2",
#   "httpx>=0.27",
#   "html2text>=2024.2.26",
#   "pydantic>=2",
# ]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 N. V. Lang
"""MCP server for the Lean Language Reference (https://lean-lang.org/doc/reference/latest/).

Indexes the manual's published `xref.json` (cross-reference index) so an agent can:
  * `search`     — find tactics, terms, sections, options, etc. by name (paginated)
  * `list_kinds` — enumerate the indexed entry kinds with counts
  * `fetch_page` — fetch a manual page (or a single `#anchor` entry) as Markdown

Every tool accepts `response_format` ("markdown" default, or "json" for structured output).

Hardening (assumes a possibly-hostile or confused agent driving these tools):
  * all network access restricted to https://lean-lang.org/doc/reference/ — enforced on
    the request URL AND the post-redirect response URL;
  * URL allowlist rejects path traversal in any encoding (`..`, `%2e%2e`, `..\\`, …);
  * responses streamed with an 8 MB cap (defuses decompression bombs);
  * outbound requests gated by a token-bucket rate limiter (default 2 req/s, burst 5);
  * page cache bounded by 200 MB LRU eviction; conditional revalidation via ETag/304;
  * Markdown output capped at 200 KB with a clear truncation marker.

Run standalone for a smoke check:  uv run --script server.py --smoke
Run as MCP stdio server:           uv run --script server.py
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote, urlparse

import html2text
import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

# --------------------------------------------------------------------- constants

REF_ROOT = "https://lean-lang.org/doc/reference/latest"
XREF_URL = f"{REF_ROOT}/xref.json"
CACHE_DIR = Path(
    os.environ.get(
        "LEAN_REF_MCP_CACHE",
        str(Path.home() / ".cache" / "lean-reference-mcp"),
    )
)
XREF_CACHE = CACHE_DIR / "xref.json"
PAGE_CACHE_DIR = CACHE_DIR / "pages"
XREF_TTL_SECONDS = 24 * 3600
PAGE_TTL_SECONDS = 24 * 3600
MAX_RESPONSE_BYTES = 8 * 1024 * 1024        # cap any single HTTP body (xref is ~3 MB)
MAX_MARKDOWN_BYTES = 200 * 1024             # cap Markdown returned to the agent
MAX_ANCHOR_HTML_BYTES = 80 * 1024           # cap anchor-extraction fallback
MAX_PAGE_CACHE_BYTES = 200 * 1024 * 1024    # LRU-evict the page cache above this size
USER_AGENT = "lean-reference-mcp/0.2 (+local; uv-run)"
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/json"})

log = logging.getLogger("lean-reference-mcp")


def _read_float_env(name: str, default: float, *, allow_zero: bool = False) -> float:
    """Read a finite float from an env var; fall back to `default` on bad input."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not a number; using default {default}", file=sys.stderr)
        return default
    if math.isnan(val) or math.isinf(val):
        print(f"warning: {name}={raw!r} is not finite; using default {default}", file=sys.stderr)
        return default
    floor = 0.0 if allow_zero else 1e-9
    if val < floor:
        print(
            f"warning: {name}={raw!r} must be {'>= 0' if allow_zero else '> 0'}; using default {default}",
            file=sys.stderr,
        )
        return default
    return val


# Outbound-request rate limit (token bucket). Defaults stay well below anything a
# polite scraper would do; a full corpus walk of the ~30 chapter pages still
# completes in ~15 s without queueing. Overridable via env.
RATE_BURST = _read_float_env("LEAN_REF_MCP_RATE_BURST", 5.0)
RATE_REFILL_PER_SEC = _read_float_env("LEAN_REF_MCP_RATE_PER_SEC", 2.0)
RATE_MAX_WAIT_SEC = _read_float_env("LEAN_REF_MCP_RATE_MAX_WAIT", 3.0, allow_zero=True)

# Verso domain key -> (friendly kind, human-readable label).
# `Verso.Genre.Manual.example` and `Manual.examples` are intentionally omitted:
# their schemas are awkward and the entries don't carry useful identifiers.
KIND_MAP: dict[str, tuple[str, str]] = {
    "Verso.Genre.Manual.doc.tactic":      ("tactic",          "Tactic"),
    "Verso.Genre.Manual.doc.tactic.conv": ("conv-tactic",     "Conversion tactic"),
    "Verso.Genre.Manual.doc":             ("term",            "Lean constant"),
    "Verso.Genre.Manual.doc.tech":        ("glossary",        "Glossary term"),
    "Verso.Genre.Manual.doc.option":      ("option",          "Compiler option"),
    "Verso.Genre.Manual.doc.syntaxKind":  ("syntax-kind",     "Syntax kind"),
    "Verso.Genre.Manual.doc.suggestion":  ("suggestion",      "Search suggestion"),
    "Verso.Genre.Manual.section":         ("section",         "Manual section"),
    "Manual.Syntax.production":           ("syntax",          "Syntax production"),
    "Manual.errorExplanation":            ("error",           "Error explanation"),
    "Manual.envVar":                      ("envvar",          "Environment variable"),
    "Manual.configFile":                  ("config-file",     "Configuration file"),
    "Manual.parserAlias":                 ("parser-alias",    "Parser alias"),
    "Manual.lakeCommand":                 ("lake-cmd",        "Lake command"),
    "Manual.lakeOpt":                     ("lake-opt",        "Lake option"),
    "Manual.lakeTomlTable":               ("lake-toml-table", "Lake TOML table"),
    "Manual.lakeTomlField":               ("lake-toml-field", "Lake TOML field"),
    "Manual.elanCommand":                 ("elan-cmd",        "Elan command"),
    "Manual.elanOpt":                     ("elan-opt",        "Elan option"),
}
KINDS = {kind for kind, _ in KIND_MAP.values()}


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


@dataclass(frozen=True)
class Entry:
    kind: str            # friendly kind (e.g. "tactic")
    name: str            # canonical key from xref `contents`
    display: str         # userName / term / title — what a human types/reads
    url: str             # absolute URL with anchor
    section: str | None  # section number (sections only)
    context: str | None  # parent breadcrumb (e.g. "Tactic Proofs > Tactic Reference")


def _entry_to_dict(e: Entry) -> dict[str, Any]:
    """Structured form of an Entry for JSON responses."""
    d: dict[str, Any] = {"kind": e.kind, "name": e.name, "display": e.display, "url": e.url}
    if e.section:
        d["section"] = e.section.rstrip(".")
    if e.context:
        d["context"] = e.context
    return d


# --------------------------------------------------------------------- HTTP client

@functools.cache
def _http() -> httpx.AsyncClient:
    """Single shared async client: reuses TLS, sends a consistent identifying UA."""
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,  # post-fetch host check below keeps us in scope
    )


def _check_response_in_scope(final_url: str) -> None:
    """After a request (incl. redirects), require the final URL on lean-lang.org/doc/reference/."""
    p = urlparse(final_url)
    # Host comparison is case-insensitive per RFC 3986 §3.2.2 (urlparse only lowercases scheme).
    if p.scheme != "https" or p.netloc.lower() != "lean-lang.org" or not p.path.startswith("/doc/reference/"):
        raise RuntimeError(f"refusing response from out-of-scope URL: {final_url}")


# --------------------------------------------------------------------- rate limit

class _RateLimited(RuntimeError):
    """Raised when the outbound token bucket can't be replenished within the deadline."""


# Token-bucket state. The critical section (refill + check + decrement) contains no
# `await`, so under asyncio's single-threaded cooperative scheduling it is atomic
# across concurrent callers — no lock needed.
_rate_tokens: float = RATE_BURST
_rate_last_refill: float = time.monotonic()
_rate_rejected_count = 0


async def _acquire_request_token() -> None:
    """Token-bucket gate for outbound HTTP requests.

    Sleeps up to RATE_MAX_WAIT_SEC; raises `_RateLimited` if a token still isn't
    available. Cache hits never call this; 304 revalidations do (they hit origin).
    """
    global _rate_tokens, _rate_last_refill, _rate_rejected_count
    deadline = time.monotonic() + RATE_MAX_WAIT_SEC
    while True:
        now = time.monotonic()
        elapsed = now - _rate_last_refill
        if elapsed > 0:
            _rate_tokens = min(RATE_BURST, _rate_tokens + elapsed * RATE_REFILL_PER_SEC)
            _rate_last_refill = now
        if _rate_tokens >= 1.0:
            _rate_tokens -= 1.0
            return
        shortfall = 1.0 - _rate_tokens
        wait_for = shortfall / RATE_REFILL_PER_SEC if RATE_REFILL_PER_SEC > 0 else float("inf")
        if now + wait_for > deadline:
            _rate_rejected_count += 1
            log.warning(
                "rate-limit hit (sustained %.2g req/s, burst %g); rejecting after %.1fs wait "
                "(total rejections this process: %d)",
                RATE_REFILL_PER_SEC, RATE_BURST, RATE_MAX_WAIT_SEC, _rate_rejected_count,
            )
            raise _RateLimited(
                f"outbound rate limit: >{RATE_REFILL_PER_SEC:g} req/s sustained "
                f"(burst {RATE_BURST:g}); waited {RATE_MAX_WAIT_SEC:g}s without a token"
            )
        await asyncio.sleep(min(wait_for, 0.25))


# --------------------------------------------------------------------- cached fetch

async def _cached_get(url: str, cache_path: Path, ttl_seconds: int) -> tuple[bytes, str]:
    """Fetch `url` with disk cache + ETag revalidation, streamed with a byte cap.

    Returns (body_bytes, source); source is "cache" / "revalidated" / "fresh" / "stale".
    Raises RuntimeError on out-of-scope redirects, unexpected content-type, body over
    MAX_RESPONSE_BYTES, or unrecoverable network failure.
    """
    meta_path = cache_path.with_suffix(cache_path.suffix + ".meta")

    cached_body: bytes | None = None
    etag: str | None = None
    cache_age = float("inf")
    if cache_path.exists():
        try:
            cached_body = cache_path.read_bytes()
            cache_age = time.time() - cache_path.stat().st_mtime
        except OSError:
            cached_body = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                etag = meta.get("etag") if isinstance(meta, dict) else None
            except (OSError, json.JSONDecodeError):
                etag = None

    if cached_body is not None and cache_age < ttl_seconds:
        return cached_body, "cache"

    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag

    try:
        await _acquire_request_token()
        async with _http().stream("GET", url, headers=headers) as resp:
            if resp.status_code == 304 and cached_body is not None:
                try:
                    os.utime(cache_path, None)  # reset TTL clock
                except OSError:
                    pass
                return cached_body, "revalidated"
            resp.raise_for_status()
            _check_response_in_scope(str(resp.url))
            ct = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ct and ct not in ALLOWED_CONTENT_TYPES:
                raise RuntimeError(f"refusing unexpected content-type: {ct!r}")
            content_length = resp.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        raise RuntimeError(
                            f"declared Content-Length {content_length} exceeds "
                            f"{MAX_RESPONSE_BYTES}-byte cap"
                        )
                except ValueError:
                    pass  # malformed header — fall through to streaming cap
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise RuntimeError(
                        f"response body exceeded {MAX_RESPONSE_BYTES}-byte cap "
                        f"(possible decompression bomb)"
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            new_etag = resp.headers.get("etag")
    except (httpx.HTTPError, _RateLimited) as exc:
        if cached_body is not None:
            return cached_body, "stale"
        if isinstance(exc, _RateLimited):
            raise
        raise RuntimeError(f"network fetch failed and no cached copy: {exc}") from exc

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(cache_path)
    if new_etag:
        meta_path.write_text(json.dumps({"etag": new_etag}), encoding="utf-8")
    elif meta_path.exists():
        try:
            meta_path.unlink()
        except OSError:
            pass
    if cache_path.parent == PAGE_CACHE_DIR:
        _enforce_page_cache_budget()
    return body, "fresh"


def _enforce_page_cache_budget() -> None:
    """If total size of PAGE_CACHE_DIR exceeds MAX_PAGE_CACHE_BYTES, LRU-evict oldest files."""
    if not PAGE_CACHE_DIR.exists():
        return
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for p in PAGE_CACHE_DIR.iterdir():
        if not p.is_file() or p.name.endswith(".tmp"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    if total <= MAX_PAGE_CACHE_BYTES:
        return
    entries.sort()  # oldest first
    for _, size, p in entries:
        if total <= MAX_PAGE_CACHE_BYTES:
            break
        meta = p.with_suffix(p.suffix + ".meta")
        try:
            p.unlink()
            total -= size
        except OSError:
            continue
        if meta.exists():
            try:
                meta.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------- xref loading

async def _load_xref() -> dict[str, Any]:
    """Load and parse xref.json, recovering from a corrupt cache if necessary."""
    body, _ = await _cached_get(XREF_URL, XREF_CACHE, XREF_TTL_SECONDS)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # Cache is corrupt — drop it and try once more from the network.
        for p in (XREF_CACHE, XREF_CACHE.with_suffix(XREF_CACHE.suffix + ".meta")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        body, _ = await _cached_get(XREF_URL, XREF_CACHE, XREF_TTL_SECONDS)
        return json.loads(body)


def _build_entries(xref: dict[str, Any]) -> list[Entry]:
    out: list[Entry] = []
    for domain, (kind, _) in KIND_MAP.items():
        block = xref.get(domain) or {}
        contents = block.get("contents") or {}
        for name, raw in contents.items():
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                if not isinstance(item, dict):
                    continue
                addr, anchor = item.get("address"), item.get("id")
                if not addr or not anchor:
                    continue
                data = item.get("data")
                if not isinstance(data, dict):
                    data = {}
                display = (
                    data.get("userName")
                    or data.get("title")
                    or data.get("term")
                    or data.get("display")
                    or name
                )
                section = data.get("sectionNum") if isinstance(data.get("sectionNum"), str) else None
                ctx_list = data.get("context")
                context = None
                if isinstance(ctx_list, list):
                    titles = [c.get("title") for c in ctx_list if isinstance(c, dict)]
                    titles = [t for t in titles if t]
                    if titles:
                        context = " > ".join(titles)
                out.append(Entry(
                    kind=kind,
                    name=name,
                    display=str(display),
                    url=f"{REF_ROOT}{addr}#{anchor}",
                    section=section,
                    context=context,
                ))
    return out


# --------------------------------------------------------------------- scoring

_WORD_SPLIT = re.compile(r"[\W_]+", re.UNICODE)


def _score(entry: Entry, q_lower: str) -> int:
    name_l = entry.name.lower()
    disp_l = entry.display.lower()
    s = 0
    if name_l == q_lower:           s += 1000
    if disp_l == q_lower:           s += 900
    if name_l.startswith(q_lower):  s += 200
    if disp_l.startswith(q_lower):  s += 150
    if q_lower in name_l:           s += 50
    if q_lower in disp_l:           s += 40
    tokens = set(filter(None, _WORD_SPLIT.split(f"{name_l} {disp_l}")))
    if q_lower in tokens:
        s += 100
    s -= min(len(entry.name), 80) // 10  # gentle preference for shorter names
    return s


# --------------------------------------------------------------------- URL safety

def _path_has_traversal(path: str) -> bool:
    """True iff `path` decodes to anything containing `.` or `..` segments.

    Percent-decoded once because servers will; backslashes folded to forward
    slashes because some normalizers treat them as separators.
    """
    decoded = unquote(path).replace("\\", "/")
    return any(seg in (".", "..") for seg in decoded.split("/"))


def _resolve_ref_url(url_or_path: str) -> tuple[str, str]:
    """Resolve a user-supplied URL or site-relative path to (absolute_url, anchor).

    Refuses anything that isn't `https://lean-lang.org/doc/reference/…` or that
    contains path-traversal segments in any encoding we recognize.
    """
    raw = url_or_path.strip()
    if not raw:
        raise ValueError("empty URL")
    if "#" in raw:
        loc, anchor = raw.split("#", 1)
    else:
        loc, anchor = raw, ""
    if loc.lower().startswith("http://"):
        raise ValueError(f"refusing plain-http URL, use https://: {loc!r}")
    if loc.lower().startswith("https://"):
        url = loc
    else:
        url = REF_ROOT.rstrip("/") + "/" + loc.lstrip("/")
    p = urlparse(url)
    # Host comparison is case-insensitive per RFC 3986 §3.2.2 (urlparse only lowercases scheme).
    if p.scheme != "https" or p.netloc.lower() != "lean-lang.org":
        raise ValueError(f"refusing URL outside https://lean-lang.org: {loc!r}")
    if not p.path.startswith("/doc/reference/"):
        raise ValueError(f"refusing URL outside the Lean Reference: {loc!r}")
    if _path_has_traversal(p.path):
        raise ValueError(f"refusing URL with traversal segments: {loc!r}")
    return url, anchor


def _page_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return PAGE_CACHE_DIR / f"{digest}.html"


# --------------------------------------------------------------------- HTML helpers

def _make_h2t() -> html2text.HTML2Text:
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.ignore_emphasis = False
    h.unicode_snob = True
    h.protect_links = True
    return h


def _extract_anchor_element(html: str, anchor: str) -> str:
    """Return the single HTML element bearing id="anchor", balanced by tag depth.

    The Lean manual wraps each anchorable entry in an element with an `id` —
    `<section>` for chapter sections, `<div class="namedocs">` for individual
    tactic/term docs, etc. We balance opens/closes of *that* element's tag so a
    `#anchor` request returns exactly one entry, not its neighbours. Falls back
    to a bounded byte slice if the markup can't be balanced, or to the whole
    page if the anchor isn't found.
    """
    m = re.search(rf'<(\w+)\b[^>]*\bid="{re.escape(anchor)}"', html, re.I)
    if not m:
        return html
    tag = m.group(1)
    tag_re = re.compile(rf"<(/?){re.escape(tag)}\b", re.I)
    depth = 0
    pos = m.start()
    while pos < len(html):
        t = tag_re.search(html, pos)
        if not t:
            break
        if t.group(1):  # closing tag
            depth -= 1
            if depth == 0:
                close = html.find(">", t.end())
                if close != -1:
                    return html[m.start():close + 1]
                break
        else:           # opening tag
            depth += 1
        pos = t.end()
    # Unbalanced markup (or a void element) — bounded fallback.
    return html[m.start():m.start() + MAX_ANCHOR_HTML_BYTES]


def _cap_output(text: str, limit: int, what: str) -> tuple[str, bool]:
    """Return (possibly-truncated text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    marker = f"\n\n[truncated: capped at {limit} bytes of {what}; supply a tighter `#anchor` for less content]"
    return text[:limit] + marker, True


def _error(message: str, fmt: ResponseFormat) -> str:
    """Format an error message according to the requested response format."""
    if fmt is ResponseFormat.JSON:
        return json.dumps({"error": message}, ensure_ascii=False)
    return message


# --------------------------------------------------------------------- index cache

_ENTRIES: list[Entry] | None = None
_index_lock = asyncio.Lock()


async def ensure_index() -> list[Entry]:
    """Return the in-memory entry index, loading xref.json on first use.

    Guarded by a lock so concurrent tool calls trigger at most one network load.
    """
    global _ENTRIES
    if _ENTRIES is not None:
        return _ENTRIES
    async with _index_lock:
        if _ENTRIES is None:  # re-check inside the lock
            _ENTRIES = _build_entries(await _load_xref())
    return _ENTRIES


# --------------------------------------------------------------------- core search

async def search_entries(
    query: str, kind: str | None, limit: int, offset: int
) -> tuple[list[Entry], int]:
    """Return (page_of_hits, total_match_count) for a scored search."""
    q_lower = query.strip().lower()
    if not q_lower:
        return [], 0
    entries = await ensure_index()
    if kind:
        k = kind.strip().lower().rstrip("s")  # forgive "tactics" → "tactic"
        if k not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; try one of: {', '.join(sorted(KINDS))}")
        entries = [e for e in entries if e.kind == k]
    scored = [(e, _score(e, q_lower)) for e in entries]
    ranked = sorted(
        (es for es in scored if es[1] > 0),
        key=lambda es: (-es[1], len(es[0].name), es[0].name),
    )
    total = len(ranked)
    page = [e for e, _ in ranked[offset:offset + limit]]
    return page, total


def _format_hit(e: Entry) -> str:
    head = f"- [{e.kind}] {e.display} — {e.url}"
    tail_bits: list[str] = []
    if e.section:
        tail_bits.append(f"§{e.section.rstrip('.')}")
    if e.context and e.kind != "section":
        tail_bits.append(e.context)
    if tail_bits:
        head += f"  ({'; '.join(tail_bits)})"
    if e.display != e.name:
        head += f"\n    canonical: {e.name}"
    return head


# --------------------------------------------------------------------- MCP server

@asynccontextmanager
async def _lifespan(_server: FastMCP):
    """Close the shared HTTP client on shutdown if it was ever created."""
    try:
        yield {}
    finally:
        if _http.cache_info().currsize:
            await _http().aclose()


mcp = FastMCP("lean-reference", lifespan=_lifespan)

_READONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,  # data ultimately comes from the live lean-lang.org site
}


@mcp.tool(annotations={"title": "List Lean Reference entry kinds", **_READONLY_ANNOTATIONS})
async def list_kinds(
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (human-readable) or 'json' (structured)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """List the kinds of entries indexed from the Lean Language Reference, with counts.

    Use the returned `kind` values to filter `search` (e.g. kind="tactic"). This is a
    read-only lookup over the manual's cross-reference index; it makes no changes.

    Args:
        response_format: "markdown" for a human-readable table (default), or "json"
            for a structured object.

    Returns:
        markdown: a text table of `kind`, count, and description.
        json: {"reference_root": str, "total_entries": int,
               "kinds": [{"kind": str, "count": int, "description": str}, ...]}
    """
    entries = await ensure_index()
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    labels = {kind: label for _, (kind, label) in KIND_MAP.items()}
    ordered = sorted(counts, key=lambda k: (-counts[k], k))

    if response_format is ResponseFormat.JSON:
        return json.dumps(
            {
                "reference_root": REF_ROOT,
                "total_entries": sum(counts.values()),
                "kinds": [
                    {"kind": k, "count": counts[k], "description": labels[k]} for k in ordered
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    rows = [f"  {k:<17s} {counts[k]:>5d}  {labels[k]}" for k in ordered]
    return (
        f"Lean Language Reference — {sum(counts.values())} entries from {REF_ROOT}\n\n"
        + "  kind                count  description\n"
        + "\n".join(rows)
    )


@mcp.tool(annotations={"title": "Search the Lean Language Reference", **_READONLY_ANNOTATIONS})
async def search(
    query: Annotated[
        str, Field(description="Free-text query, matched against canonical and display names "
                               "(e.g. 'simp', 'Nat.add', 'induction').", min_length=1)
    ],
    kind: Annotated[
        str | None, Field(description="Optional kind filter from `list_kinds` "
                                      "(e.g. 'tactic', 'term', 'section', 'option').")
    ] = None,
    limit: Annotated[int, Field(description="Maximum results per page.", ge=1, le=100)] = 20,
    offset: Annotated[int, Field(description="Number of results to skip, for pagination.", ge=0)] = 0,
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (human-readable) or 'json' (structured)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """Search the Lean Language Reference for tactics, terms, sections, options, etc.

    Searches the manual's cross-reference index by name; it does NOT search free text
    inside pages (use `fetch_page` to read a page). Results are ranked by match quality
    and paginated. Read-only — makes no changes.

    Args:
        query: free-text query matched against canonical names and user-facing display
            names (e.g. "simp", "Nat.add", "induction").
        kind: optional kind filter — one of the values from `list_kinds`
            (e.g. "tactic", "term", "section", "option", "glossary", "syntax").
        limit: maximum results per page, 1-100 (default 20).
        offset: number of results to skip, for pagination (default 0).
        response_format: "markdown" (default) or "json".

    Returns:
        markdown: a header line ("N matches … showing X-Y") followed by one bullet per
            hit ("- [kind] display — url"), and a hint to re-call with a higher offset
            when more results exist.
        json: {"query": str, "kind": str|null, "total": int, "count": int,
               "offset": int, "has_more": bool, "next_offset": int|null,
               "results": [{"kind","name","display","url","section?","context?"}, ...]}

    Examples:
        - "Find the simp tactic"            -> search(query="simp", kind="tactic")
        - "Page 2 of term matches for List" -> search(query="List", kind="term", offset=20)
        - Don't use to read a page's prose  -> use `fetch_page` instead.
    """
    try:
        page, total = await search_entries(query, kind=kind, limit=limit, offset=offset)
    except ValueError as exc:
        return _error(str(exc), response_format)
    has_more = offset + len(page) < total
    next_offset = offset + len(page) if has_more else None

    if response_format is ResponseFormat.JSON:
        return json.dumps(
            {
                "query": query,
                "kind": kind,
                "total": total,
                "count": len(page),
                "offset": offset,
                "has_more": has_more,
                "next_offset": next_offset,
                "results": [_entry_to_dict(e) for e in page],
            },
            indent=2,
            ensure_ascii=False,
        )

    if not page:
        suffix = f" (kind={kind})" if kind else ""
        if total and offset >= total:
            return f"Offset {offset} is past the end ({total} matches for {query!r}{suffix})."
        return f"No matches for {query!r}{suffix}."
    header = (
        f"{total} match(es) for {query!r}"
        + (f" in kind={kind}" if kind else "")
        + f"; showing {offset + 1}-{offset + len(page)}."
    )
    lines = [header, *(_format_hit(e) for e in page)]
    if has_more:
        lines.append(f"\n(more results — call again with offset={next_offset})")
    return "\n".join(lines)


@mcp.tool(annotations={"title": "Fetch a Lean Reference page", **_READONLY_ANNOTATIONS})
async def fetch_page(
    url_or_path: Annotated[
        str,
        Field(
            description="Absolute URL inside https://lean-lang.org/doc/reference/, or a "
                        "site-relative path like '/Tactic-Proofs/Tactic-Reference/'. "
                        "Append '#anchor' to focus on one section/entry.",
            min_length=1,
        ),
    ],
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (page text) or 'json' (text + metadata)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """Fetch a Lean Language Reference page and return its content as Markdown.

    Converts the manual's HTML to Markdown. When the input includes an `#anchor`, only
    that single entry/section is returned (not the whole chapter). Plain http://,
    off-domain URLs, and path-traversal segments (incl. %2e%2e / backslash variants)
    are rejected. Read-only — makes no changes.

    Args:
        url_or_path: an absolute URL inside https://lean-lang.org/doc/reference/, or a
            site-relative path like "/Tactic-Proofs/Tactic-Reference/". May include an
            "#anchor" (e.g. ".../Tactic-Reference/#induction") to return one entry.
        response_format: "markdown" (default) for the page text, or "json" for the
            text plus metadata.

    Returns:
        markdown: "<url>" header line, then the page/section content as Markdown
            (capped at ~200 KB with a truncation marker).
        json: {"url": str, "anchor": str|null, "content": str, "truncated": bool}
        On failure: an error string ("Refusing to fetch: …" / "Failed to fetch …"),
            or {"error": "..."} when response_format="json".

    Examples:
        - Read the `induction` tactic -> fetch_page(url_or_path=".../Tactic-Reference/#induction")
        - Read a whole chapter        -> fetch_page(url_or_path="/Tactic-Proofs/Tactic-Reference/")
        - Resolve a `search` hit      -> pass the `url` field of a search result here.
    """
    try:
        url, anchor = _resolve_ref_url(url_or_path)
    except ValueError as exc:
        return _error(f"Refusing to fetch: {exc}", response_format)
    cache_path = _page_cache_path(url)
    try:
        body, _source = await _cached_get(url, cache_path, PAGE_TTL_SECONDS)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _error(f"Failed to fetch {url}: {exc}", response_format)

    html = body.decode("utf-8", errors="replace")
    body_html = _extract_anchor_element(html, anchor) if anchor else html
    markdown = _make_h2t().handle(body_html)
    content, truncated = _cap_output(markdown, MAX_MARKDOWN_BYTES, "Markdown")
    full_url = f"{url}#{anchor}" if anchor else url

    if response_format is ResponseFormat.JSON:
        return json.dumps(
            {"url": full_url, "anchor": anchor or None, "content": content, "truncated": truncated},
            indent=2,
            ensure_ascii=False,
        )
    return f"<{full_url}>\n\n{content}"


# --------------------------------------------------------------------- entrypoint

async def _smoke() -> int:
    """Offline self-check: load index, run searches, exercise URL-safety + formats."""
    print("Loading xref index...", file=sys.stderr)
    entries = await ensure_index()
    print(f"  loaded {len(entries)} entries", file=sys.stderr)
    print(await list_kinds())
    print()
    print("--- list_kinds(json) (first 200 chars) ---")
    print((await list_kinds(ResponseFormat.JSON))[:200], "…")
    print()
    for q, k in [("simp", "tactic"), ("induction", "tactic"), ("Nat.add", "term"), ("inductive", None)]:
        print(f"--- search({q!r}, kind={k!r}) ---")
        print(await search(q, kind=k, limit=5))
        print()
    print("--- search('List', kind='term', limit=3, offset=3, json) ---")
    print(await search("List", kind="term", limit=3, offset=3, response_format=ResponseFormat.JSON))
    print()
    print("--- URL safety ---")
    for bad in [
        "http://lean-lang.org/doc/reference/x",
        "https://evil.com/doc/reference/x",
        "https://lean-lang.org/doc/reference/%2e%2e/private",
        "https://lean-lang.org/doc/reference/..\\private",
        "https://lean-lang.org/not-reference/",
        "",
    ]:
        out = (await fetch_page(bad)).splitlines()[0]
        print(f"  reject {bad!r}: {out}")
    print()
    print("--- anchor precision: fetch_page('.../Tactic-Reference/#induction') ---")
    out = await fetch_page("/Tactic-Proofs/Tactic-Reference/#induction")
    fun = out.count("fun_induction")
    print(f"  output {len(out)} chars; mentions 'fun_induction' {fun}x "
          f"(should be small + 0 — i.e. just the `induction` entry)")
    print("  first 240 chars:", repr(out[:240]))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        sys.exit(asyncio.run(_smoke()))
    mcp.run()
