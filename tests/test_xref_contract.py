# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 N. V. Lang
"""Live contract test against the real Lean Language Reference — the drift alarm.

The offline suite (test_core.py, test_fetch.py) checks verso-mcp against a
*synthetic* xref.json fixture — i.e. against our assumption of the format — so it
keeps passing even if Verso changes the real format. This test exercises the
live site end to end and fails if the format no longer parses into a correct
index.

Marked `live`: excluded from the default run, executed weekly by
.github/workflows/canary.yml (see AGENTS.md, "Upstream format drift").
"""

from __future__ import annotations

import json

import pytest

from verso_mcp import server

pytestmark = pytest.mark.live


@pytest.fixture
def lean_reference(_isolate, monkeypatch: pytest.MonkeyPatch) -> server.Site:
    """Point the server at the real default site, overriding the offline fixture.

    `_isolate` (autouse) installs synthetic example.com sites; depending on it
    forces this fixture to run afterwards, so the real site wins.
    """
    sites = server._parse_sites(server.DEFAULT_SITE_SPEC)
    monkeypatch.setattr(server, "SITES", sites)
    monkeypatch.setattr(server, "DEFAULT_ALIAS", next(iter(sites)))
    return next(iter(sites.values()))


async def test_live_xref_still_builds_a_correct_index(lean_reference: server.Site):
    """The real xref.json still parses into a healthy, correctly-linked index."""
    # Builds the index from the live xref.json. If the format drifted badly,
    # `_build_index`'s schema-drift guard raises right here.
    index = await server.ensure_index(lean_reference)

    assert len(index.entries) > 100, (
        f"only {len(index.entries)} entries — the xref.json format may have changed"
    )
    assert "tactic" in index.kinds, f"no 'tactic' kind; kinds = {sorted(index.kinds)}"

    # `search` still finds a well-known entry...
    hits = json.loads(
        await server.search("simp", kind="tactic", response_format=server.ResponseFormat.JSON)
    )
    assert hits.get("total", 0) >= 1, f"search('simp', kind='tactic') found nothing: {hits}"

    # ...and the URL built from xref.json resolves to the right page content.
    url = hits["results"][0]["url"]
    page = json.loads(await server.fetch_page(url, response_format=server.ResponseFormat.JSON))
    assert "simp" in page.get("content", "").lower(), (
        f"fetch_page({url}) returned no simp-related content"
    )
