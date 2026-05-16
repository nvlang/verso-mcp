# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 N. V. Lang
"""Shared pytest fixtures.

The whole suite is offline: no test touches the network. Fixtures here

  * isolate every test from real module state and the real cache directory
    (`_isolate`, autouse), and
  * let a test serve canned HTTP responses through `httpx.MockTransport`
    (`install_http`).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from verso_mcp import server

# A two-site registry shared by every test. Both sites live on the same host so
# the suite can exercise same-host-but-out-of-scope rejection.
TEST_SITES = {
    "ref": server.Site(alias="ref", root="https://example.com/doc/reference/latest/"),
    "book": server.Site(alias="book", root="https://example.com/book/"),
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Give every test a clean module state and a private cache directory.

    `server` carries process-global state (the site registry, per-site indexes,
    robots caches, the rate-limiter bucket). Without this fixture, tests would
    leak state into one another and depend on `VERSO_MCP_SITES` in the
    environment. Everything is restored automatically by `monkeypatch`.
    """
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "SITES", dict(TEST_SITES))
    monkeypatch.setattr(server, "DEFAULT_ALIAS", "ref")
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_index_locks", {})
    monkeypatch.setattr(server, "_robots", {})
    monkeypatch.setattr(server, "_robots_locks", {})
    monkeypatch.setattr(server, "_rate_tokens", server.RATE_BURST)
    monkeypatch.setattr(server, "_rate_last_refill", time.monotonic())
    monkeypatch.setattr(server, "_rate_rejected_count", 0)


@pytest.fixture
async def install_http(_isolate, monkeypatch: pytest.MonkeyPatch) -> Callable:
    """Return `install(handler)`, which routes the server's HTTP through a mock.

    `handler` is an `httpx.MockTransport` request handler: it takes an
    `httpx.Request` and returns an `httpx.Response`. The installed client keeps
    `follow_redirects=True` so redirect-scope checks can be exercised.
    """
    clients: list[httpx.AsyncClient] = []

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        )
        clients.append(client)
        monkeypatch.setattr(server, "_http", lambda: client)
        return client

    yield install

    for client in clients:
        await client.aclose()
