# verso-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that
lets an AI agent search and read documentation built with
[Verso](https://github.com/leanprover/verso), Lean's documentation authoring
tool.

Verso powers the Lean ecosystem's reference docs and books — the
[Lean Language Reference](https://lean-lang.org/doc/reference/latest/),
[Functional Programming in Lean](https://lean-lang.org/functional_programming_in_lean/),
[Theorem Proving in Lean 4](https://lean-lang.org/theorem_proving_in_lean4/),
and more. Verso *Manual*-genre sites publish a machine-readable cross-reference
index (`xref.json`); this server consumes that index and the rendered HTML —
**no modifications to Verso or to the documentation site are required.**

## Status

`server.py` (v0.2) targets a **single** Verso site: the Lean Language
Reference. Generic, multi-site support is on the [roadmap](#roadmap).

## Tools

| Tool         | Description                                                                                          |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| `list_kinds` | List the kinds of indexed entries (tactics, terms, sections, options, …) with counts.                |
| `search`     | Name-ranked search over the manual's cross-reference index, with `kind` filtering and pagination.    |
| `fetch_page` | Fetch a manual page — or a single `#anchor` entry — and return it as Markdown.                       |

All three tools are read-only and accept a `response_format` of `markdown`
(default) or `json`.

## Requirements

- [uv](https://docs.astral.sh/uv/). The script declares its dependencies inline
  ([PEP 723](https://peps.python.org/pep-0723/)), so `uv run` fetches them on
  first launch — no virtualenv or `pip install` needed.

## Use with Claude Code / Claude Desktop

Add an entry to your MCP configuration (`.mcp.json`, `~/.claude.json`, or
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "lean-reference": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--script", "/absolute/path/to/verso-mcp/server.py"]
    }
  }
}
```

Use an **absolute path** so the server resolves regardless of the client's
working directory.

## Configuration

Environment variables (all optional):

| Variable                    | Default                      | Purpose                                                |
| --------------------------- | ---------------------------- | ------------------------------------------------------ |
| `LEAN_REF_MCP_CACHE`        | `~/.cache/lean-reference-mcp` | Cache directory for `xref.json` and fetched pages.     |
| `LEAN_REF_MCP_RATE_PER_SEC` | `2`                          | Sustained outbound request rate (requests/second).    |
| `LEAN_REF_MCP_RATE_BURST`   | `5`                          | Token-bucket burst capacity.                           |
| `LEAN_REF_MCP_RATE_MAX_WAIT`| `3`                          | Max seconds to wait for a token before refusing.       |

## Safety & etiquette

The server is built to be a well-behaved client of the documentation site:

- **Scoped network access** — fetches are restricted to
  `https://lean-lang.org/doc/reference/`, enforced on the request URL *and* the
  final post-redirect URL. Path traversal (`..`, `%2e%2e`, backslash variants)
  is rejected.
- **Rate limiting** — a token bucket caps outbound requests (default 2 req/s,
  burst 5); a hit falls back to cached content rather than hammering the origin.
- **Caching & revalidation** — `xref.json` and pages are cached on disk (24 h
  TTL) with `ETag`/`If-None-Match` conditional revalidation, so a repeated
  lookup costs at most a `304 Not Modified`.
- **Bounded responses** — HTTP bodies are streamed with an 8 MB cap; Markdown
  output is capped at 200 KB; the page cache is LRU-evicted at 200 MB.
- **Identifying `User-Agent`** on every request.

## Evaluation

`evaluation.xml` is a 10-question evaluation suite in the format used by
Anthropic's `mcp-builder` skill. Every question is read-only, independent, and
has a single stable, verifiable answer; collectively they exercise `search`
ranking/pagination and `fetch_page` anchor extraction.

## Roadmap

The goal is a **generic Verso documentation MCP**: point it at any Verso
Manual-genre site and get the same tools. Planned work:

1. **Site registry** — accept one or more Verso site roots via configuration
   (environment variable or launch args), each with an optional short alias.
2. **Per-site indexing** — fetch and cache each site's `xref.json` and build its
   entry index independently; cache directories namespaced per site.
3. **Dynamic kinds** — derive entry kinds and human-readable labels from each
   `xref.json`'s domain blocks (their `title` fields) instead of a hardcoded
   table, so project-specific domains are picked up automatically.
4. **Site selector** — tools gain an optional `site` argument (alias or URL),
   and a `list_sites` tool enumerates the configured sites.
5. **Allowlist & rebrand** — outbound fetches restricted to the configured site
   roots (preserving today's SSRF protections); server and tool names move from
   `lean-reference` to `verso`.

No upstream Verso changes are required: `xref.json` is already a published
artifact, and Verso's own on-site search consumes it the same way. An optional
future contribution could be a documented, versioned schema for `xref.json`,
which is currently undocumented.

## License

[Apache License 2.0](LICENSE) © 2026 N. V. Lang
