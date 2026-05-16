# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 N. V. Lang
"""Network-facing tests, driven entirely through `httpx.MockTransport` — no real
sockets are opened.

Covers the cached-fetch path, robots.txt obedience, the outbound rate limiter,
and the four MCP tools end to end. Several of these pin *security invariants*
(see AGENTS.md): the byte cap, the post-redirect scope check, robots.txt
obedience, and rate limiting. Do not weaken them without understanding the
guard.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from verso_mcp import server

# --- canned site content -----------------------------------------------------

# Mirrors the xref.json shape: site-relative `address`, on-page `id` anchor.
XREF = {
    "Verso.Genre.Manual.doc.tactic": {
        "title": "Tactic Reference",
        "contents": {
            "Lean.Parser.Tactic.simp": {
                "address": "Tactics/",
                "id": "simp",
                "data": {"userName": "simp"},
            },
            "Lean.Parser.Tactic.induction": {
                "address": "Tactics/",
                "id": "induction",
                "data": {"userName": "induction"},
            },
        },
    },
    "Verso.Genre.Manual.section": {
        "title": "Manual Sections",
        "contents": {
            "tactic-proofs": {
                "address": "Tactics/",
                "id": "sec-tactics",
                "data": {"title": "Tactic Proofs", "sectionNum": "2.1."},
            },
        },
    },
}

PAGE_HTML = (
    b"<html><body>"
    b'<div id="simp"><h2>simp</h2><p>The simp tactic.</p></div>'
    b'<div id="induction"><h2>induction</h2><p>The other tactic.</p></div>'
    b"</body></html>"
)

CONTENT_URL = "https://example.com/doc/reference/latest/page"


def _doc_handler(request: httpx.Request) -> httpx.Response:
    """A site that serves robots.txt, xref.json, and one HTML page."""
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(404)
    if path == "/doc/reference/latest/xref.json":
        return httpx.Response(200, json=XREF)
    if path == "/doc/reference/latest/Tactics/":
        return httpx.Response(200, content=PAGE_HTML, headers={"content-type": "text/html"})
    return httpx.Response(404)


# --- _cached_get --------------------------------------------------------------


async def test_cached_get_fresh_then_cache(install_http, tmp_path):
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(
            200, content=b"<html>hi</html>", headers={"content-type": "text/html"}
        )

    install_http(handler)
    cache_path = tmp_path / "c.html"

    body, source = await server._cached_get(CONTENT_URL, cache_path, server.PAGE_TTL_SECONDS)
    assert body == b"<html>hi</html>"
    assert source == "fresh"
    assert calls["n"] == 1

    # Within TTL the second call is served from disk — no second network hit.
    body2, source2 = await server._cached_get(CONTENT_URL, cache_path, server.PAGE_TTL_SECONDS)
    assert body2 == b"<html>hi</html>"
    assert source2 == "cache"
    assert calls["n"] == 1


async def test_cached_get_rejects_bad_content_type(install_http, tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    install_http(handler)
    with pytest.raises(RuntimeError, match="content-type"):
        await server._cached_get(CONTENT_URL, tmp_path / "c.html", server.PAGE_TTL_SECONDS)


async def test_cached_get_byte_cap(install_http, tmp_path, monkeypatch):
    # SECURITY INVARIANT: an oversized body is refused (decompression-bomb guard).
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 64)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 4096, headers={"content-type": "text/html"})

    install_http(handler)
    with pytest.raises(RuntimeError, match="cap"):
        await server._cached_get(CONTENT_URL, tmp_path / "c.html", server.PAGE_TTL_SECONDS)


async def test_cached_get_rejects_offsite_redirect(install_http, tmp_path):
    # SECURITY INVARIANT: a redirect that lands off the configured sites is
    # refused even though the *request* URL was in scope.
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "evil.com":
            return httpx.Response(200, content=b"pwned", headers={"content-type": "text/html"})
        return httpx.Response(302, headers={"location": "https://evil.com/gotcha"})

    install_http(handler)
    with pytest.raises(RuntimeError, match="out-of-scope"):
        await server._cached_get(CONTENT_URL, tmp_path / "c.html", server.PAGE_TTL_SECONDS)


async def test_cached_get_stale_fallback_on_network_error(install_http, tmp_path):
    state = {"fail": False}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if state["fail"]:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=b"cached-body", headers={"content-type": "text/html"})

    install_http(handler)
    cache_path = tmp_path / "c.html"

    body, source = await server._cached_get(CONTENT_URL, cache_path, server.PAGE_TTL_SECONDS)
    assert source == "fresh"

    # ttl=0 forces revalidation; the network now fails, so the stale copy is served.
    state["fail"] = True
    body2, source2 = await server._cached_get(CONTENT_URL, cache_path, 0)
    assert body2 == b"cached-body"
    assert source2 == "stale"


async def test_cached_get_obeys_robots(install_http, tmp_path):
    # SECURITY INVARIANT: a blanket robots.txt disallow blocks the fetch.
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, content=b"secret", headers={"content-type": "text/html"})

    install_http(handler)
    with pytest.raises(RuntimeError, match="robots"):
        await server._cached_get(CONTENT_URL, tmp_path / "c.html", server.PAGE_TTL_SECONDS)


# --- robots.txt ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("robots_body", "status", "expected"),
    [
        ("User-agent: *\nDisallow: /", 200, False),  # blanket disallow
        ("User-agent: *\nDisallow: /private/", 200, True),  # unrelated path
        ("User-agent: verso-mcp\nDisallow: /", 200, False),  # targets this server
        ("", 404, True),  # missing robots.txt -> allow
        ("", 403, False),  # host gating access -> disallow
    ],
)
async def test_robots_allowed(install_http, robots_body, status, expected):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text=robots_body)
        return httpx.Response(200, text="ok")

    install_http(handler)
    url = "https://example.com/doc/reference/latest/Tactics/"
    assert await server._robots_allowed(url) is expected


# --- rate limiter -------------------------------------------------------------


async def test_acquire_request_token_succeeds_then_rejects(monkeypatch):
    # SECURITY INVARIANT: the outbound token bucket caps request bursts.
    monkeypatch.setattr(server, "_rate_last_refill", time.monotonic())
    monkeypatch.setattr(server, "_rate_tokens", 3.0)
    monkeypatch.setattr(server, "RATE_MAX_WAIT_SEC", 0.0)

    for _ in range(3):  # three tokens in the bucket
        await server._acquire_request_token()

    before = server._rate_rejected_count
    with pytest.raises(server._RateLimited):  # bucket empty, no wait budget
        await server._acquire_request_token()
    assert server._rate_rejected_count == before + 1


# --- _load_xref ---------------------------------------------------------------


async def test_load_xref_non_verso_site_raises(install_http):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        )

    install_http(handler)
    with pytest.raises(RuntimeError, match="valid JSON"):
        await server._load_xref(server.SITES["ref"])


# --- the four MCP tools, end to end ------------------------------------------


async def test_list_sites_tool():
    out = await server.list_sites()
    assert "ref" in out
    assert "book" in out
    assert "[default]" in out


async def test_list_kinds_tool(install_http):
    install_http(_doc_handler)
    out = await server.list_kinds(site="ref")
    assert "tactic" in out
    assert "section" in out


async def test_search_tool(install_http):
    install_http(_doc_handler)
    out = await server.search("simp", site="ref")
    assert "simp" in out

    out_json = await server.search("simp", site="ref", response_format=server.ResponseFormat.JSON)
    data = json.loads(out_json)
    assert data["total"] >= 1
    assert data["site"] == "ref"


async def test_fetch_page_tool(install_http):
    install_http(_doc_handler)
    out = await server.fetch_page("Tactics/", site="ref")
    assert "simp" in out
    assert "The simp tactic." in out


async def test_fetch_page_anchor_precision(install_http):
    # An #anchor must return only that entry, not the whole page.
    install_http(_doc_handler)
    out = await server.fetch_page("Tactics/#simp", site="ref")
    assert "The simp tactic." in out
    assert "The other tactic." not in out


async def test_unknown_site_clean_error():
    out = await server.search("simp", site="does-not-exist")
    assert "unknown site" in out
