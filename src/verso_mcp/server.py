# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 N. V. Lang
"""MCP server for Verso-generated documentation sites.

Verso (https://github.com/leanprover/verso) is Lean's documentation authoring
tool. Every Verso *Manual*-genre site publishes a machine-readable
cross-reference index, `xref.json`, at its root. This server consumes that
index plus the rendered HTML — no changes to Verso or the site are needed — and
exposes:

  * `list_sites`  — enumerate the configured Verso sites
  * `list_kinds`  — entry kinds for a site (tactics, terms, sections, …), with counts
  * `search`      — name-ranked search over a site's cross-reference index (paginated)
  * `fetch_page`  — fetch a page (or a single `#anchor` entry) as Markdown

Configure sites via the `VERSO_MCP_SITES` environment variable, a comma-separated
list of `alias=url` pairs (a bare URL gets an auto-derived alias). When unset,
the server defaults to the Lean Language Reference.

  VERSO_MCP_SITES="lean-reference=https://lean-lang.org/doc/reference/latest/,
                   fpil=https://lean-lang.org/functional_programming_in_lean/"

Hardening (assumes a possibly-hostile or confused agent driving these tools):
  * network access is restricted to the configured site roots — enforced on the
    request URL AND the post-redirect response URL; the configured site list IS
    the allowlist;
  * the URL check rejects path traversal in any encoding (`..`, `%2e%2e`, `..\\`);
  * each host's robots.txt is fetched and obeyed for this server's User-Agent;
  * responses are streamed with an 8 MB cap (defuses decompression bombs);
  * outbound requests are gated by a shared token-bucket rate limiter;
  * each site's cache is bounded by 200 MB LRU eviction; ETag/304 revalidation;
  * Markdown output is capped at 200 KB with a clear truncation marker.

Run a smoke check:        verso-mcp --smoke   (or python -m verso_mcp --smoke)
Run as MCP stdio server:  verso-mcp           (or python -m verso_mcp)
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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

import html2text
import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

# --------------------------------------------------------------------- constants

__version__ = "0.4.0"

CACHE_DIR = Path(os.environ.get("VERSO_MCP_CACHE", str(Path.home() / ".cache" / "verso-mcp")))
XREF_TTL_SECONDS = 24 * 3600
PAGE_TTL_SECONDS = 24 * 3600
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # cap any single HTTP body
MAX_MARKDOWN_BYTES = 200 * 1024  # cap Markdown returned to the agent
MAX_ANCHOR_HTML_BYTES = 80 * 1024  # cap anchor-extraction fallback
MAX_PAGE_CACHE_BYTES = 200 * 1024 * 1024  # LRU-evict a site's page cache above this
MAX_ROBOTS_BYTES = 512 * 1024  # cap parsed robots.txt size
USER_AGENT = f"verso-mcp/{__version__}"
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/json"})

# Used when VERSO_MCP_SITES is unset or yields no usable entries.
DEFAULT_SITE_SPEC = "lean-reference=https://lean-lang.org/doc/reference/latest/"

log = logging.getLogger("verso-mcp")


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
            f"warning: {name}={raw!r} must be {'>= 0' if allow_zero else '> 0'}; "
            f"using default {default}",
            file=sys.stderr,
        )
        return default
    return val


# Outbound-request rate limit (token bucket), shared across all sites so the
# server stays a polite client of every origin. Overridable via env.
RATE_BURST = _read_float_env("VERSO_MCP_RATE_BURST", 5.0)
RATE_REFILL_PER_SEC = _read_float_env("VERSO_MCP_RATE_PER_SEC", 2.0)
RATE_MAX_WAIT_SEC = _read_float_env("VERSO_MCP_RATE_MAX_WAIT", 3.0, allow_zero=True)


class ResponseFormat(StrEnum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


# --------------------------------------------------------------------- site registry


@dataclass(frozen=True)
class Site:
    """A configured Verso documentation site."""

    alias: str  # short selector, e.g. "lean-reference"
    root: str  # normalized site root URL, always ends with "/"

    @property
    def xref_url(self) -> str:
        return self.root + "xref.json"

    @property
    def cache_dir(self) -> Path:
        digest = hashlib.sha256(self.root.encode("utf-8")).hexdigest()[:16]
        return CACHE_DIR / f"site-{digest}"


def _normalize_root(url: str) -> str:
    """Normalize a site root: require https, lowercase host, ensure trailing slash."""
    url = url.strip()
    if url.lower().startswith("http://"):
        raise ValueError(f"site root must use https://: {url!r}")
    if not url.lower().startswith("https://"):
        raise ValueError(f"site root must be an absolute https:// URL: {url!r}")
    p = urlparse(url)
    if not p.netloc:
        raise ValueError(f"site root has no host: {url!r}")
    path = p.path if p.path.endswith("/") else p.path + "/"
    return f"https://{p.netloc.lower()}{path}"


def _alias_from_url(url: str) -> str:
    """Derive a short alias from a site URL (last meaningful path segment)."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    chosen = next((s for s in reversed(segs) if s not in ("latest", "doc", "docs")), None)
    chosen = chosen or (segs[-1] if segs else p.netloc.split(".")[0])
    return re.sub(r"[^a-z0-9]+", "-", chosen.lower()).strip("-") or "site"


def _parse_sites(spec: str) -> dict[str, Site]:
    """Parse a `VERSO_MCP_SITES`-style spec into an ordered {alias: Site} map."""
    sites: dict[str, Site] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        # Split on the first '=' for an `alias=url` pair — but not when the part
        # before '=' is itself a URL (a bare URL may legitimately contain '=').
        head = item.partition("=")[0].strip().lower()
        if "=" in item and not head.startswith(("http://", "https://")):
            raw_alias, _, raw_url = item.partition("=")
        else:
            raw_alias, raw_url = "", item
        try:
            root = _normalize_root(raw_url)
        except ValueError as exc:
            print(f"warning: skipping invalid VERSO_MCP_SITES entry — {exc}", file=sys.stderr)
            continue
        alias = re.sub(r"\s+", "-", raw_alias.strip().lower()) or _alias_from_url(root)
        base, n = alias, 2
        while alias in sites:
            alias = f"{base}-{n}"
            n += 1
        sites[alias] = Site(alias=alias, root=root)
    return sites


_sites_spec = os.environ.get("VERSO_MCP_SITES", "").strip()
SITES: dict[str, Site] = _parse_sites(_sites_spec) if _sites_spec else {}
if not SITES:
    if _sites_spec:
        print(
            "warning: VERSO_MCP_SITES had no usable entries; using the default site",
            file=sys.stderr,
        )
    SITES = _parse_sites(DEFAULT_SITE_SPEC)
DEFAULT_ALIAS = next(iter(SITES))


# --------------------------------------------------------------------- entry model


@dataclass(frozen=True)
class Entry:
    kind: str  # friendly kind slug (e.g. "tactic")
    name: str  # canonical key from xref `contents`
    display: str  # userName / term / title — what a human types/reads
    url: str  # absolute URL with anchor
    section: str | None  # section number (sections only)
    context: str | None  # parent breadcrumb (e.g. "Tactic Proofs > Tactic Reference")


@dataclass
class SiteIndex:
    """A site's loaded cross-reference index."""

    entries: list[Entry]
    kinds: dict[str, str] = field(default_factory=dict)  # kind slug -> human label


def _entry_to_dict(e: Entry) -> dict[str, Any]:
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
        follow_redirects=True,  # post-fetch scope check below keeps us in bounds
    )


def _site_for_url(url: str) -> Site | None:
    """Return the configured Site whose root contains `url`, else None.

    This is the SSRF allowlist: only URLs under a configured site root pass.
    Host comparison is case-insensitive (RFC 3986 §3.2.2).
    """
    p = urlparse(url.split("#", 1)[0])
    if p.scheme != "https":
        return None
    host = p.netloc.lower()
    for site in SITES.values():
        sp = urlparse(site.root)
        if host == sp.netloc.lower() and (
            p.path == sp.path.rstrip("/") or p.path.startswith(sp.path)
        ):
            return site
    return None


# --------------------------------------------------------------------- rate limit


class _RateLimited(RuntimeError):
    """Raised when the outbound token bucket can't be replenished within the deadline."""


# Token-bucket state. The critical section (refill + check + decrement) contains
# no `await`, so under asyncio's single-threaded scheduling it is atomic across
# concurrent callers — no lock needed.
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
                RATE_REFILL_PER_SEC,
                RATE_BURST,
                RATE_MAX_WAIT_SEC,
                _rate_rejected_count,
            )
            raise _RateLimited(
                f"outbound rate limit: >{RATE_REFILL_PER_SEC:g} req/s sustained "
                f"(burst {RATE_BURST:g}); waited {RATE_MAX_WAIT_SEC:g}s without a token"
            )
        await asyncio.sleep(min(wait_for, 0.25))


# --------------------------------------------------------------------- robots.txt

_robots: dict[str, RobotFileParser] = {}
_robots_locks: dict[str, asyncio.Lock] = {}


def _robots_lock_for(host: str) -> asyncio.Lock:
    lock = _robots_locks.get(host)
    if lock is None:
        lock = _robots_locks[host] = asyncio.Lock()
    return lock


async def _robots_for_host(host: str) -> RobotFileParser:
    """Fetch and parse a host's robots.txt once per process; cache the result.

    A missing robots.txt (404) or an unreachable one resolves to "allow"; an
    explicit 401/403 resolves to "disallow all" (the host is gating access).
    """
    rp = _robots.get(host)
    if rp is not None:
        return rp
    async with _robots_lock_for(host):
        rp = _robots.get(host)
        if rp is not None:
            return rp
        rp = RobotFileParser()
        try:
            await _acquire_request_token()
            resp = await _http().get(f"https://{host}/robots.txt")
            if resp.status_code == 200:
                rp.parse(resp.text[:MAX_ROBOTS_BYTES].splitlines())
            elif resp.status_code in (401, 403):
                rp.disallow_all = True
            else:
                rp.allow_all = True
        except (httpx.HTTPError, _RateLimited):
            rp.allow_all = True  # robots unreachable — don't block doc access
        _robots[host] = rp
        return rp


async def _robots_allowed(url: str) -> bool:
    """Whether this server's User-Agent may fetch `url`, per the host's robots.txt."""
    rp = await _robots_for_host(urlparse(url).netloc.lower())
    return rp.can_fetch(USER_AGENT, url)


# --------------------------------------------------------------------- cached fetch


async def _cached_get(url: str, cache_path: Path, ttl_seconds: int) -> tuple[bytes, str]:
    """Fetch `url` with disk cache + ETag revalidation, streamed with a byte cap.

    Returns (body_bytes, source); source is "cache" / "revalidated" / "fresh" / "stale".
    Raises RuntimeError on a robots.txt disallowal, out-of-scope redirects, an
    unexpected content-type, a body over MAX_RESPONSE_BYTES, or unrecoverable
    network failure.
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

    if not await _robots_allowed(url):
        raise RuntimeError(
            f"{urlparse(url).netloc}'s robots.txt disallows fetching this URL "
            f"for user-agent {USER_AGENT!r}"
        )

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
            if _site_for_url(str(resp.url)) is None:
                raise RuntimeError(f"refusing response from out-of-scope URL: {resp.url}")
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
    return body, "fresh"


def _enforce_page_cache_budget(pages_dir: Path) -> None:
    """If a site's page cache exceeds MAX_PAGE_CACHE_BYTES, LRU-evict oldest files."""
    if not pages_dir.exists():
        return
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for p in pages_dir.iterdir():
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


# --------------------------------------------------------------------- index building


async def _load_xref(site: Site) -> Any:
    """Load and parse a site's xref.json, recovering from a corrupt cache.

    Raises a clear RuntimeError (not a bare JSONDecodeError) when the response
    still isn't valid JSON after a refetch — typically because the configured
    URL is not a Verso Manual-genre site.
    """
    xref_cache = site.cache_dir / "xref.json"
    body, _ = await _cached_get(site.xref_url, xref_cache, XREF_TTL_SECONDS)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        for p in (xref_cache, xref_cache.with_suffix(xref_cache.suffix + ".meta")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        body, _ = await _cached_get(site.xref_url, xref_cache, XREF_TTL_SECONDS)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{site.xref_url} did not return valid JSON; '{site.alias}' may not "
                f"be a Verso Manual-genre site (those publish xref.json at their root)"
            ) from exc


def _domain_slug(domain_key: str) -> str:
    """Derive a short kebab-case kind slug from a Verso domain key.

    e.g. 'Verso.Genre.Manual.doc.tactic' -> 'tactic',
         'Manual.lakeCommand' -> 'lake-command'.
    """
    last = domain_key.rsplit(".", 1)[-1]
    kebab = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", last)
    kebab = re.sub(r"[^a-zA-Z0-9]+", "-", kebab).strip("-").lower()
    return kebab or "misc"


def _build_index(site: Site, xref: Any) -> SiteIndex:
    """Build a SiteIndex from a parsed xref.json.

    Kinds are derived dynamically: every top-level domain becomes a kind, slugged
    from its key and labelled from its `title`. Domains with no usable entries are
    omitted. This means the server works on any Verso Manual-genre site, including
    project-specific domains it has never seen.
    """
    if not isinstance(xref, dict):
        raise RuntimeError(f"{site.xref_url} is not a JSON object (not a Verso xref.json?)")

    entries: list[Entry] = []
    labels: dict[str, str] = {}
    used_slugs: set[str] = set()
    root = site.root.rstrip("/")
    saw_contents = False  # did any domain offer cross-reference entries to index?

    for domain_key, block in xref.items():
        if not isinstance(block, dict):
            continue
        slug = _domain_slug(domain_key)
        base, n = slug, 2
        while slug in used_slugs:
            slug = f"{base}-{n}"
            n += 1
        used_slugs.add(slug)
        title = block.get("title")
        labels[slug] = (
            title
            if isinstance(title, str) and title and title != domain_key
            else slug.replace("-", " ")
        )
        contents = block.get("contents")
        if contents:
            saw_contents = True
        if not isinstance(contents, dict):
            continue
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
                section = (
                    data.get("sectionNum") if isinstance(data.get("sectionNum"), str) else None
                )
                ctx_list = data.get("context")
                context = None
                if isinstance(ctx_list, list):
                    titles = [c.get("title") for c in ctx_list if isinstance(c, dict)]
                    titles = [t for t in titles if t]
                    if titles:
                        context = " > ".join(titles)
                addr_s = str(addr)
                if addr_s.lower().startswith(("http://", "https://")):
                    # Real Verso addresses are site-relative; an absolute one is
                    # kept only if it stays within the owning site's root, so
                    # every indexed Entry URL is guaranteed fetchable.
                    if not addr_s.startswith(site.root):
                        continue
                    page = addr_s
                else:
                    page = root + "/" + addr_s.lstrip("/")
                entries.append(
                    Entry(
                        kind=slug,
                        name=str(name),
                        display=str(display),
                        url=f"{page}#{anchor}",
                        section=section,
                        context=context,
                    )
                )

    # Schema-drift guard: the JSON parsed, but if domains offered entries and
    # none survived, Verso's xref.json format has very likely changed. Fail
    # loudly here instead of letting the tools quietly serve an empty index.
    if saw_contents and not entries:
        raise RuntimeError(
            f"{site.xref_url} parsed as JSON but yielded no usable entries — "
            "the Verso xref.json format may have changed; verso-mcp likely "
            "needs an update."
        )

    present = {e.kind for e in entries}
    kinds = {slug: labels[slug] for slug in labels if slug in present}
    return SiteIndex(entries=entries, kinds=kinds)


_indexes: dict[str, SiteIndex] = {}
_index_locks: dict[str, asyncio.Lock] = {}


def _lock_for(alias: str) -> asyncio.Lock:
    lock = _index_locks.get(alias)
    if lock is None:
        lock = _index_locks[alias] = asyncio.Lock()
    return lock


async def ensure_index(site: Site) -> SiteIndex:
    """Return a site's index, loading and caching xref.json on first use.

    Guarded by a per-site lock so concurrent tool calls trigger at most one load.
    """
    cached = _indexes.get(site.alias)
    if cached is not None:
        return cached
    async with _lock_for(site.alias):
        cached = _indexes.get(site.alias)
        if cached is None:
            cached = _build_index(site, await _load_xref(site))
            _indexes[site.alias] = cached
    return cached


# --------------------------------------------------------------------- scoring

_WORD_SPLIT = re.compile(r"[\W_]+", re.UNICODE)


def _score(entry: Entry, q_lower: str) -> int:
    name_l = entry.name.lower()
    disp_l = entry.display.lower()
    s = 0
    if name_l == q_lower:
        s += 1000
    if disp_l == q_lower:
        s += 900
    if name_l.startswith(q_lower):
        s += 200
    if disp_l.startswith(q_lower):
        s += 150
    if q_lower in name_l:
        s += 50
    if q_lower in disp_l:
        s += 40
    tokens = set(filter(None, _WORD_SPLIT.split(f"{name_l} {disp_l}")))
    if q_lower in tokens:
        s += 100
    s -= min(len(entry.name), 80) // 10  # gentle preference for shorter names
    return s


def search_index(
    index: SiteIndex, query: str, kind: str | None, limit: int, offset: int
) -> tuple[list[Entry], int]:
    """Return (page_of_hits, total_match_count) for a scored search over one site."""
    q_lower = query.strip().lower()
    if not q_lower:
        return [], 0
    entries = index.entries
    if kind:
        k = kind.strip().lower()
        if k not in index.kinds:
            if k.endswith("s") and k[:-1] in index.kinds:
                k = k[:-1]  # forgive a plural ("tactics" -> "tactic")
            else:
                valid = ", ".join(sorted(index.kinds)) or "(none)"
                raise ValueError(f"unknown kind {kind!r}; valid kinds for this site: {valid}")
        entries = [e for e in entries if e.kind == k]
    ranked = sorted(
        ((e, _score(e, q_lower)) for e in entries),
        key=lambda es: (-es[1], len(es[0].name), es[0].name),
    )
    ranked = [es for es in ranked if es[1] > 0]
    total = len(ranked)
    page = [e for e, _ in ranked[offset : offset + limit]]
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


# --------------------------------------------------------------------- URL safety


def _path_has_traversal(path: str) -> bool:
    """True iff `path` decodes to anything containing `.` or `..` segments."""
    decoded = unquote(path).replace("\\", "/")
    return any(seg in (".", "..") for seg in decoded.split("/"))


def _resolve_site(alias: str | None) -> Site:
    """Resolve a site alias (or None for the default) to a Site."""
    if not alias or not alias.strip():
        return SITES[DEFAULT_ALIAS]
    key = alias.strip().lower()
    if key in SITES:
        return SITES[key]
    raise ValueError(f"unknown site {alias!r}; configured sites: {', '.join(sorted(SITES))}")


def _resolve_page_url(url_or_path: str, default_site: Site) -> tuple[str, str, Site]:
    """Resolve a URL or site-relative path to (absolute_url, anchor, owning_site).

    Absolute URLs must fall under a configured site root. Relative paths resolve
    against `default_site`. Plain http://, off-site URLs, and path traversal
    (`..`, `%2e%2e`, backslash variants) are rejected.
    """
    raw = url_or_path.strip()
    if not raw:
        raise ValueError("empty URL")
    loc, _, anchor = raw.partition("#")
    if loc.lower().startswith("http://"):
        raise ValueError(f"refusing plain-http URL, use https://: {loc!r}")
    if loc.lower().startswith("https://"):
        url = loc
    else:
        url = default_site.root + loc.lstrip("/")
    site = _site_for_url(url)
    if site is None:
        raise ValueError(f"refusing URL outside the configured Verso sites: {loc!r}")
    if _path_has_traversal(urlparse(url).path):
        raise ValueError(f"refusing URL with traversal segments: {loc!r}")
    return url, anchor, site


def _page_cache_path(site: Site, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return site.cache_dir / "pages" / f"{digest}.html"


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

    Verso wraps each anchorable entry in an element with an `id` — `<section>`
    for chapter sections, `<div class="namedocs">` for individual entries, etc.
    Balancing opens/closes of that element's tag keeps a `#anchor` request to one
    entry. Falls back to a bounded slice on unbalanced markup, or the whole page
    if the anchor isn't found.
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
                    return html[m.start() : close + 1]
                break
        else:  # opening tag
            depth += 1
        pos = t.end()
    return html[m.start() : m.start() + MAX_ANCHOR_HTML_BYTES]


def _cap_output(text: str, limit: int, what: str) -> tuple[str, bool]:
    """Return (possibly-truncated text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    marker = (
        f"\n\n[truncated: capped at {limit} bytes of {what}; "
        "supply a tighter `#anchor` for less content]"
    )
    return text[:limit] + marker, True


def _error(message: str, fmt: ResponseFormat) -> str:
    """Format an error message according to the requested response format."""
    if fmt is ResponseFormat.JSON:
        return json.dumps({"error": message}, ensure_ascii=False)
    return message


# --------------------------------------------------------------------- MCP server


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    """Close the shared HTTP client on shutdown if it was ever created."""
    try:
        yield {}
    finally:
        if _http.cache_info().currsize:
            await _http().aclose()


mcp = FastMCP("verso", lifespan=_lifespan)

_READONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,  # data comes from live Verso documentation sites
}

_SITE_FIELD = Field(
    description="Which configured Verso site to use — an alias from `list_sites`. "
    "Omit to use the default site.",
)


@mcp.tool(annotations={"title": "List configured Verso sites", **_READONLY_ANNOTATIONS})
async def list_sites(
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (human-readable) or 'json' (structured)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """List the Verso documentation sites this server is configured to serve.

    Each site has an `alias` — pass it as the `site` argument to `search`,
    `list_kinds`, or `fetch_page` to target that site. Sites are configured via
    the `VERSO_MCP_SITES` environment variable. Read-only.

    Args:
        response_format: "markdown" (default) or "json".

    Returns:
        markdown: one line per site (alias, default marker, root URL).
        json: {"default": str, "sites": [{"alias","root","indexed"}, ...]}
    """
    rows = []
    for alias, site in SITES.items():
        rows.append(
            {
                "alias": alias,
                "root": site.root,
                "indexed": alias in _indexes,
                "default": alias == DEFAULT_ALIAS,
            }
        )
    if response_format is ResponseFormat.JSON:
        return json.dumps({"default": DEFAULT_ALIAS, "sites": rows}, indent=2, ensure_ascii=False)
    width = max((len(r["alias"]) for r in rows), default=0)
    lines = [f"{len(rows)} configured Verso site(s):"]
    for r in rows:
        mark = "  [default]" if r["default"] else "           "
        lines.append(f"  {r['alias']:<{width}}{mark}  {r['root']}")
    lines.append("\nPass `site=<alias>` to `search`, `list_kinds`, or `fetch_page`.")
    return "\n".join(lines)


@mcp.tool(annotations={"title": "List a Verso site's entry kinds", **_READONLY_ANNOTATIONS})
async def list_kinds(
    site: Annotated[str | None, _SITE_FIELD] = None,
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (human-readable) or 'json' (structured)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """List the kinds of entries indexed for a Verso site, with counts.

    Kinds are derived from the site's cross-reference index — they vary per site
    (a language reference has tactics and options; a textbook has sections and
    terms). Use the returned `kind` values to filter `search`. Read-only.

    Args:
        site: which configured site (alias from `list_sites`); omit for the default.
        response_format: "markdown" (default) or "json".

    Returns:
        markdown: a table of `kind`, count, and human-readable description.
        json: {"site": str, "root": str, "total_entries": int,
               "kinds": [{"kind","count","description"}, ...]}
    """
    try:
        s = _resolve_site(site)
    except ValueError as exc:
        return _error(str(exc), response_format)
    try:
        index = await ensure_index(s)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _error(f"Failed to load index for site '{s.alias}': {exc}", response_format)

    counts: dict[str, int] = {}
    for e in index.entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    ordered = sorted(index.kinds, key=lambda k: (-counts.get(k, 0), k))

    if response_format is ResponseFormat.JSON:
        return json.dumps(
            {
                "site": s.alias,
                "root": s.root,
                "total_entries": len(index.entries),
                "kinds": [
                    {"kind": k, "count": counts.get(k, 0), "description": index.kinds[k]}
                    for k in ordered
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    width = max((len(k) for k in ordered), default=4)
    rows = [f"  {k:<{width}}  {counts.get(k, 0):>5d}  {index.kinds[k]}" for k in ordered]
    return (
        f"Verso site '{s.alias}' — {len(index.entries)} entries from {s.root}\n\n"
        + f"  {'kind':<{width}}  count  description\n"
        + "\n".join(rows)
    )


@mcp.tool(annotations={"title": "Search a Verso site", **_READONLY_ANNOTATIONS})
async def search(
    query: Annotated[
        str,
        Field(
            description="Free-text query, matched against canonical and display "
            "names (e.g. 'simp', 'Nat.add', 'monad').",
            min_length=1,
        ),
    ],
    site: Annotated[str | None, _SITE_FIELD] = None,
    kind: Annotated[
        str | None,
        Field(
            description="Optional kind filter from `list_kinds` "
            "(e.g. 'tactic', 'section', 'option')."
        ),
    ] = None,
    limit: Annotated[int, Field(description="Maximum results per page.", ge=1, le=100)] = 20,
    offset: Annotated[
        int, Field(description="Number of results to skip, for pagination.", ge=0)
    ] = 0,
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (human-readable) or 'json' (structured)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """Search a Verso documentation site's cross-reference index by name.

    Matches entry names and display names (not free text inside pages — use
    `fetch_page` to read a page). Results are ranked by match quality and
    paginated. Read-only.

    Args:
        query: free-text query matched against canonical and user-facing names.
        site: which configured site (alias from `list_sites`); omit for the default.
        kind: optional kind filter — one of the values from `list_kinds`.
        limit: maximum results per page, 1-100 (default 20).
        offset: number of results to skip, for pagination (default 0).
        response_format: "markdown" (default) or "json".

    Returns:
        markdown: a header ("N matches … showing X-Y") then one bullet per hit
            ("- [kind] display — url"), plus a hint to re-call with a higher offset.
        json: {"site","query","kind","total","count","offset","has_more",
               "next_offset","results":[{"kind","name","display","url",...}]}

    Examples:
        - "Find the simp tactic"             -> search(query="simp", kind="tactic")
        - "Search the FPiL book for monads"  -> search(query="monad", site="fpil")
        - "Next page of results"             -> search(query=..., offset=20)
    """
    try:
        s = _resolve_site(site)
    except ValueError as exc:
        return _error(str(exc), response_format)
    try:
        index = await ensure_index(s)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _error(f"Failed to load index for site '{s.alias}': {exc}", response_format)
    try:
        page, total = search_index(index, query, kind, limit, offset)
    except ValueError as exc:
        return _error(str(exc), response_format)
    has_more = offset + len(page) < total
    next_offset = offset + len(page) if has_more else None

    if response_format is ResponseFormat.JSON:
        return json.dumps(
            {
                "site": s.alias,
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
        return f"No matches for {query!r}{suffix} in site '{s.alias}'."
    header = (
        f"{total} match(es) for {query!r} in site '{s.alias}'"
        + (f", kind={kind}" if kind else "")
        + f"; showing {offset + 1}-{offset + len(page)}."
    )
    lines = [header, *(_format_hit(e) for e in page)]
    if has_more:
        lines.append(f"\n(more results — call again with offset={next_offset})")
    return "\n".join(lines)


@mcp.tool(annotations={"title": "Fetch a Verso documentation page", **_READONLY_ANNOTATIONS})
async def fetch_page(
    url_or_path: Annotated[
        str,
        Field(
            description="An absolute URL on a configured Verso site, or a site-relative "
            "path like '/Tactic-Proofs/Tactic-Reference/'. Append '#anchor' "
            "to focus on one section/entry.",
            min_length=1,
        ),
    ],
    site: Annotated[str | None, _SITE_FIELD] = None,
    response_format: Annotated[
        ResponseFormat, Field(description="'markdown' (page text) or 'json' (text + metadata)")
    ] = ResponseFormat.MARKDOWN,
) -> str:
    """Fetch a page from a Verso documentation site and return it as Markdown.

    Converts the site's HTML to Markdown. With an `#anchor`, only that single
    entry/section is returned (not the whole chapter). A *relative* path is
    resolved against `site`; an *absolute* URL is accepted only if it falls under
    a configured site root. Plain http://, off-site URLs, and path-traversal
    segments are rejected. Read-only.

    Args:
        url_or_path: absolute URL on a configured site, or a site-relative path.
            May include an "#anchor" (e.g. ".../Tactic-Reference/#induction").
        site: site to resolve a *relative* path against (alias from `list_sites`);
            omit for the default. Ignored when `url_or_path` is absolute.
        response_format: "markdown" (default) for the page text, or "json" for
            text plus metadata.

    Returns:
        markdown: a "<url>" header line, then the page/section as Markdown
            (capped at ~200 KB with a truncation marker).
        json: {"site","url","anchor","content","truncated"}
        On failure: an error string, or {"error": "..."} when response_format="json".

    Examples:
        - Read one entry  -> fetch_page(url_or_path=".../Tactic-Reference/#induction")
        - Read a chapter  -> fetch_page(url_or_path="/Tactic-Proofs/Tactic-Reference/")
        - Resolve a hit   -> pass the `url` field of a `search` result here.
    """
    try:
        default_site = _resolve_site(site)
        url, anchor, owning_site = _resolve_page_url(url_or_path, default_site)
    except ValueError as exc:
        return _error(f"Refusing to fetch: {exc}", response_format)
    cache_path = _page_cache_path(owning_site, url)
    try:
        body, source = await _cached_get(url, cache_path, PAGE_TTL_SECONDS)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _error(f"Failed to fetch {url}: {exc}", response_format)
    if source == "fresh":
        _enforce_page_cache_budget(owning_site.cache_dir / "pages")

    html = body.decode("utf-8", errors="replace")
    body_html = _extract_anchor_element(html, anchor) if anchor else html
    markdown = _make_h2t().handle(body_html)
    content, truncated = _cap_output(markdown, MAX_MARKDOWN_BYTES, "Markdown")
    full_url = f"{url}#{anchor}" if anchor else url

    if response_format is ResponseFormat.JSON:
        return json.dumps(
            {
                "site": owning_site.alias,
                "url": full_url,
                "anchor": anchor or None,
                "content": content,
                "truncated": truncated,
            },
            indent=2,
            ensure_ascii=False,
        )
    return f"<{full_url}>\n\n{content}"


# --------------------------------------------------------------------- entrypoint


async def _smoke() -> int:
    """Offline self-check: registry, index load, searches, URL safety, anchors."""
    print(f"Configured sites: {', '.join(SITES)} (default: {DEFAULT_ALIAS})", file=sys.stderr)
    print(await list_sites())
    print()
    s = SITES[DEFAULT_ALIAS]
    index = await ensure_index(s)
    print(
        f"default site '{s.alias}' index: {len(index.entries)} entries, {len(index.kinds)} kinds",
        file=sys.stderr,
    )
    print(await list_kinds())
    print()
    for q, k in [("simp", "tactic"), ("induction", "tactic"), ("inductive", None)]:
        print(f"--- search({q!r}, kind={k!r}) ---")
        print(await search(q, kind=k, limit=4))
        print()
    print("--- search('List', kind='doc', json, paginated) ---")
    # 'doc' is the slug for the Lean-constant domain on the Lean reference site
    print(
        (await search("List", kind="doc", limit=2, offset=2, response_format=ResponseFormat.JSON))[
            :300
        ],
        "…",
    )
    print()
    print("--- unknown site error ---")
    print(" ", await search("simp", site="does-not-exist"))
    print()
    print("--- URL safety ---")
    for bad in [
        "http://lean-lang.org/doc/reference/x",
        "https://evil.com/doc/reference/x",
        "https://lean-lang.org/doc/reference/latest/%2e%2e/private",
        "https://lean-lang.org/some-other-site/",
        "",
    ]:
        print(f"  reject {bad!r}: {(await fetch_page(bad)).splitlines()[0]}")
    print()
    print("--- anchor precision: fetch_page('.../Tactic-Reference/#induction') ---")
    out = await fetch_page("/Tactic-Proofs/Tactic-Reference/#induction")
    print(
        f"  {len(out)} chars; mentions 'fun_induction' {out.count('fun_induction')}x "
        f"(want small + 0)"
    )
    return 0


def main() -> None:
    """Console entry point: run the stdio MCP server, or `--smoke` for a self-check."""
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        raise SystemExit(asyncio.run(_smoke()))
    mcp.run()


if __name__ == "__main__":
    main()
