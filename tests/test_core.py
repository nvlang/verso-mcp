# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 N. V. Lang
"""Pure-logic tests: site parsing, the SSRF allowlist, URL safety, indexing,
scoring/search, HTML anchor extraction, output capping, env parsing.

No network, no async. Several of these pin *security invariants* — see
AGENTS.md; do not weaken a test here without understanding what it guards.
"""

from __future__ import annotations

import pytest

from verso_mcp import server

REF = server.Site(alias="ref", root="https://example.com/doc/reference/latest/")
BOOK = server.Site(alias="book", root="https://example.com/book/")


# --------------------------------------------------------------- _normalize_root


def test_normalize_root_adds_trailing_slash():
    assert server._normalize_root("https://example.com/x") == "https://example.com/x/"


def test_normalize_root_lowercases_host_only():
    # Host is case-insensitive (RFC 3986); the path is not — keep its case.
    assert server._normalize_root("https://Example.COM/Docs/") == "https://example.com/Docs/"


def test_normalize_root_rejects_plain_http():
    with pytest.raises(ValueError, match="https"):
        server._normalize_root("http://example.com/x/")


def test_normalize_root_rejects_relative():
    with pytest.raises(ValueError):
        server._normalize_root("example.com/x/")


# ------------------------------------------------------------------ _alias_from_url


def test_alias_from_url_skips_boilerplate_segments():
    # "latest" / "doc" are skipped in favour of a meaningful segment.
    assert server._alias_from_url("https://lean-lang.org/doc/reference/latest/") == "reference"


def test_alias_from_url_falls_back_to_host():
    assert server._alias_from_url("https://example.com/") == "example"


# -------------------------------------------------------------------- _parse_sites


def test_parse_sites_alias_equals_url():
    sites = server._parse_sites("ref=https://example.com/x/")
    assert list(sites) == ["ref"]
    assert sites["ref"].root == "https://example.com/x/"


def test_parse_sites_bare_url_derives_alias():
    sites = server._parse_sites("https://example.com/book/")
    assert list(sites) == ["book"]


def test_parse_sites_bare_url_with_equals_is_not_split():
    # A '=' inside a bare URL must not be mistaken for the alias separator.
    sites = server._parse_sites("https://example.com/search/?q=1")
    assert len(sites) == 1
    assert next(iter(sites.values())).root.startswith("https://example.com/")


def test_parse_sites_skips_invalid_entries():
    # The ftp:// entry is dropped; the valid one survives.
    sites = server._parse_sites("ok=https://example.com/x/,bad=ftp://example.com/")
    assert list(sites) == ["ok"]


def test_parse_sites_deduplicates_aliases():
    sites = server._parse_sites("a=https://example.com/x/,a=https://example.com/y/")
    assert set(sites) == {"a", "a-2"}


# ----------------------------------------------------- _site_for_url (SSRF allowlist)


def test_site_for_url_in_scope():
    site = server._site_for_url("https://example.com/doc/reference/latest/Tactics/")
    assert site is not None
    assert site.alias == "ref"


def test_site_for_url_root_without_trailing_slash():
    assert server._site_for_url("https://example.com/doc/reference/latest") is not None


def test_site_for_url_rejects_other_host():
    assert server._site_for_url("https://evil.com/doc/reference/latest/x") is None


def test_site_for_url_rejects_same_host_out_of_scope():
    # Same host, but not under any configured root.
    assert server._site_for_url("https://example.com/private/x") is None


def test_site_for_url_rejects_prefix_confusion():
    # "latest-evil" must not be accepted as a prefix match for "latest/".
    assert server._site_for_url("https://example.com/doc/reference/latest-evil/x") is None


def test_site_for_url_rejects_userinfo_trick():
    # The real host is evil.com; the netloc includes "@evil.com" and must not match.
    assert server._site_for_url("https://example.com@evil.com/doc/reference/latest/x") is None


def test_site_for_url_rejects_plain_http():
    assert server._site_for_url("http://example.com/doc/reference/latest/x") is None


# ------------------------------------------------------------------- _resolve_site


def test_resolve_site_default():
    assert server._resolve_site(None).alias == "ref"


def test_resolve_site_by_alias_is_case_insensitive():
    assert server._resolve_site("  BOOK ").alias == "book"


def test_resolve_site_unknown_raises():
    with pytest.raises(ValueError, match="unknown site"):
        server._resolve_site("nope")


# ----------------------------------------------------------------- _path_has_traversal


@pytest.mark.parametrize(
    "path",
    [
        "/doc/../secret",
        "/doc/%2e%2e/secret",
        "/doc/%2E%2E/secret",
        "/doc/..%5csecret",  # backslash-encoded
        "/doc/./here",
    ],
)
def test_path_has_traversal_detects(path):
    assert server._path_has_traversal(path) is True


@pytest.mark.parametrize("path", ["/doc/reference/latest/Tactics/", "/a/b/c", "/"])
def test_path_has_traversal_allows_clean_paths(path):
    assert server._path_has_traversal(path) is False


# ----------------------------------------------------------------- _resolve_page_url


def test_resolve_page_url_relative_against_default_site():
    url, anchor, site = server._resolve_page_url("Tactics/#simp", REF)
    assert url == "https://example.com/doc/reference/latest/Tactics/"
    assert anchor == "simp"
    assert site.alias == "ref"


def test_resolve_page_url_absolute_picks_owning_site():
    # An absolute URL resolves to *its* site, ignoring the passed default.
    url, anchor, site = server._resolve_page_url("https://example.com/book/ch1", REF)
    assert site.alias == "book"
    assert anchor == ""


def test_resolve_page_url_rejects_plain_http():
    with pytest.raises(ValueError, match="https"):
        server._resolve_page_url("http://example.com/book/ch1", REF)


def test_resolve_page_url_rejects_off_site():
    with pytest.raises(ValueError, match="outside"):
        server._resolve_page_url("https://evil.com/x", REF)


@pytest.mark.parametrize("raw", ["../secret", "%2e%2e/secret", "..%5csecret"])
def test_resolve_page_url_rejects_traversal(raw):
    with pytest.raises(ValueError, match="traversal"):
        server._resolve_page_url(raw, REF)


def test_resolve_page_url_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        server._resolve_page_url("   ", REF)


# -------------------------------------------------------------------- _domain_slug


@pytest.mark.parametrize(
    ("key", "slug"),
    [
        ("Verso.Genre.Manual.doc.tactic", "tactic"),
        ("Manual.lakeCommand", "lake-command"),
        ("Foo", "foo"),
        ("...", "misc"),
    ],
)
def test_domain_slug(key, slug):
    assert server._domain_slug(key) == slug


# --------------------------------------------------------------------- _build_index

# A minimal but realistic xref.json: addresses are site-relative page paths,
# `id` is the on-page anchor.
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


def test_build_index_basic():
    index = server._build_index(REF, XREF)
    assert len(index.entries) == 3
    assert index.kinds["tactic"] == "Tactic Reference"
    assert index.kinds["section"] == "Manual Sections"
    simp = next(e for e in index.entries if e.display == "simp")
    assert simp.kind == "tactic"
    assert simp.url == "https://example.com/doc/reference/latest/Tactics/#simp"
    section = next(e for e in index.entries if e.kind == "section")
    assert section.section == "2.1."
    assert section.display == "Tactic Proofs"


def test_build_index_rejects_non_dict():
    with pytest.raises(RuntimeError, match="JSON object"):
        server._build_index(REF, ["not", "a", "dict"])


def test_build_index_skips_off_site_absolute_address():
    # An absolute address outside the owning site's root is dropped, so every
    # indexed Entry URL is guaranteed to pass the allowlist later.
    xref = {
        "Manual.doc": {
            "title": "Docs",
            "contents": {
                "good": {"address": "page/", "id": "g", "data": {}},
                "evil": {"address": "https://evil.com/x", "id": "e", "data": {}},
            },
        }
    }
    index = server._build_index(REF, xref)
    names = {e.name for e in index.entries}
    assert "good" in names
    assert "evil" not in names


def test_build_index_raises_on_schema_drift():
    # Entries are present, but the field names changed (address/id -> path/anchor):
    # nothing is extractable. That must fail loudly rather than yield an empty
    # index — the guard against silent breakage when Verso changes xref.json.
    xref = {
        "Verso.Genre.Manual.doc.tactic": {
            "title": "Tactic Reference",
            "contents": {"simp": {"path": "Tactics/", "anchor": "simp"}},
        }
    }
    with pytest.raises(RuntimeError, match="format may have changed"):
        server._build_index(REF, xref)


# ------------------------------------------------------------ _score / search_index


def _entry(kind: str, name: str, display: str | None = None) -> server.Entry:
    return server.Entry(
        kind=kind,
        name=name,
        display=display or name,
        url=f"https://example.com/doc/reference/latest/p/#{name}",
        section=None,
        context=None,
    )


def _index(*entries: server.Entry) -> server.SiteIndex:
    kinds = {e.kind: e.kind.title() for e in entries}
    return server.SiteIndex(entries=list(entries), kinds=kinds)


def test_score_exact_beats_substring():
    exact = server._score(_entry("tactic", "simp"), "simp")
    substring = server._score(_entry("tactic", "dsimp"), "simp")
    assert exact > substring


def test_search_ranks_exact_match_first():
    index = _index(
        _entry("tactic", "dsimp"),
        _entry("tactic", "simp_all"),
        _entry("tactic", "simp"),
    )
    page, total = server.search_index(index, "simp", None, 20, 0)
    assert total == 3
    assert page[0].name == "simp"


def test_search_kind_filter():
    index = _index(_entry("tactic", "simp"), _entry("term", "simp"))
    page, total = server.search_index(index, "simp", "tactic", 20, 0)
    assert total == 1
    assert page[0].kind == "tactic"


def test_search_forgives_plural_kind():
    index = _index(_entry("tactic", "simp"))
    page, total = server.search_index(index, "simp", "tactics", 20, 0)
    assert total == 1


def test_search_unknown_kind_raises():
    index = _index(_entry("tactic", "simp"))
    with pytest.raises(ValueError, match="unknown kind"):
        server.search_index(index, "simp", "nonexistent", 20, 0)


def test_search_pagination():
    index = _index(
        _entry("tactic", "simp"),
        _entry("tactic", "simp_all"),
        _entry("tactic", "simp_arith"),
    )
    page1, total = server.search_index(index, "simp", None, 2, 0)
    assert total == 3
    assert len(page1) == 2
    page2, total2 = server.search_index(index, "simp", None, 2, 2)
    assert total2 == 3
    assert len(page2) == 1
    assert {e.name for e in page1}.isdisjoint({e.name for e in page2})


def test_search_empty_query():
    index = _index(_entry("tactic", "simp"))
    assert server.search_index(index, "   ", None, 20, 0) == ([], 0)


# ------------------------------------------------------------- _extract_anchor_element


def test_extract_anchor_element_balances_simple():
    html = '<body><div id="x"><p>inner</p>more</div><div id="y">other</div></body>'
    assert server._extract_anchor_element(html, "x") == '<div id="x"><p>inner</p>more</div>'


def test_extract_anchor_element_balances_nested_same_tag():
    html = '<section id="a">A<section>B</section>C</section>D'
    out = server._extract_anchor_element(html, "a")
    assert out == '<section id="a">A<section>B</section>C</section>'
    assert "D" not in out


def test_extract_anchor_element_missing_anchor_returns_whole():
    html = '<div id="x">y</div>'
    assert server._extract_anchor_element(html, "absent") == html


# --------------------------------------------------------------------- _cap_output


def test_cap_output_under_limit():
    text, truncated = server._cap_output("hello", 100, "Markdown")
    assert text == "hello"
    assert truncated is False


def test_cap_output_over_limit():
    text, truncated = server._cap_output("x" * 500, 50, "Markdown")
    assert truncated is True
    assert text.startswith("x" * 50)
    assert "truncated" in text


# ------------------------------------------------------------------- _entry_to_dict


def test_entry_to_dict_strips_trailing_dot_from_section():
    entry = server.Entry(
        kind="section",
        name="sec",
        display="A Section",
        url="https://example.com/doc/reference/latest/p/#sec",
        section="1.2.",
        context="Top > Mid",
    )
    d = server._entry_to_dict(entry)
    assert d["section"] == "1.2"
    assert d["context"] == "Top > Mid"
    assert d["display"] == "A Section"


# -------------------------------------------------------------------- _read_float_env


def test_read_float_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VERSO_MCP_TEST_FLOAT", raising=False)
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 1.5

    monkeypatch.setenv("VERSO_MCP_TEST_FLOAT", "2.5")
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 2.5

    monkeypatch.setenv("VERSO_MCP_TEST_FLOAT", "not-a-number")
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 1.5

    monkeypatch.setenv("VERSO_MCP_TEST_FLOAT", "-3")
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 1.5

    monkeypatch.setenv("VERSO_MCP_TEST_FLOAT", "nan")
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 1.5

    monkeypatch.setenv("VERSO_MCP_TEST_FLOAT", "inf")
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 1.5

    monkeypatch.setenv("VERSO_MCP_TEST_FLOAT", "0")
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5) == 1.5
    assert server._read_float_env("VERSO_MCP_TEST_FLOAT", 1.5, allow_zero=True) == 0.0
