<br>
<div align="center">
<picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/nvlang/verso-mcp/main/res/logotype-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/nvlang/verso-mcp/main/res/logotype-light.svg">
    <img alt="verso-mcp" src="https://raw.githubusercontent.com/nvlang/verso-mcp/main/res/logotype-light.svg" width="70%">
</picture>
<br>
<br>
<div>

[<picture><source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/pypi/v/verso-mcp?style=flat-square&logo=pypi&logoColor=a3acb7&label=&labelColor=21262d&color=21262d"><source media="(prefers-color-scheme: light)" srcset="https://img.shields.io/pypi/v/verso-mcp?style=flat-square&logo=pypi&logoColor=24292f&label=&labelColor=eaeef2&color=eaeef2"><img alt="PyPI version" src="https://img.shields.io/pypi/v/verso-mcp?style=flat-square&logo=pypi&logoColor=24292f&label=&labelColor=eaeef2&color=eaeef2"></picture>](https://pypi.org/project/verso-mcp/)
[<picture><source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fpypi.org%2Fpypi%2Fverso-mcp%2Fjson&query=%24.info.requires_python&style=flat-square&logo=python&logoColor=a3acb7&label=&labelColor=21262d&color=21262d"><source media="(prefers-color-scheme: light)" srcset="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fpypi.org%2Fpypi%2Fverso-mcp%2Fjson&query=%24.info.requires_python&style=flat-square&logo=python&logoColor=24292f&label=&labelColor=eaeef2&color=eaeef2"><img alt="Supported Python versions" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fpypi.org%2Fpypi%2Fverso-mcp%2Fjson&query=%24.info.requires_python&style=flat-square&logo=python&logoColor=24292f&label=&labelColor=eaeef2&color=eaeef2"></picture>](https://pypi.org/project/verso-mcp/)
[<picture><source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/ossf-scorecard/github.com/nvlang/verso-mcp?style=flat-square&labelColor=21262d&color=21262d&label=OpenSSF%20Scorecard"><source media="(prefers-color-scheme: light)" srcset="https://img.shields.io/ossf-scorecard/github.com/nvlang/verso-mcp?style=flat-square&labelColor=eaeef2&color=eaeef2&label=OpenSSF%20Scorecard"><img alt="OpenSSF Scorecard" src="https://img.shields.io/ossf-scorecard/github.com/nvlang/verso-mcp?style=flat-square&labelColor=eaeef2&color=eaeef2&label=OpenSSF%20Scorecard"></picture>](https://scorecard.dev/viewer/?uri=github.com/nvlang/verso-mcp)

</div>
</div>
<br>
<br>

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that
lets an AI agent search and read documentation built with
[Verso](https://github.com/leanprover/verso), Lean's documentation authoring
tool.

Verso powers most of the Lean ecosystem's reference docs and books — the
[Lean Language Reference](https://lean-lang.org/doc/reference/latest/),
[Functional Programming in Lean](https://lean-lang.org/functional_programming_in_lean/),
[Theorem Proving in Lean 4](https://lean-lang.org/theorem_proving_in_lean4/),
and more. Verso *Manual*-genre sites publish a machine-readable cross-reference
index (`xref.json`); this server consumes that index and the rendered HTML.

> [!WARNING]
> The format of the `xref.json` files that this server depends on is
> Verso-internal and may change at any time. Such changes could render this
> server non-functional. I'll try to keep up with any such changes, but can't
> make any promises.

Point it at one or more Verso sites and an agent gets four read-only tools:
`list_sites`, `list_kinds`, `search`, and `fetch_page`.

## Tools

| Tool         | Description                                                                                       |
| ------------ | ------------------------------------------------------------------------------------------------- |
| `list_sites` | Enumerate the configured Verso sites and their aliases.                                           |
| `list_kinds` | List a site's entry kinds (tactics, terms, sections, options, …) with counts.                     |
| `search`     | Name-ranked search over a site's cross-reference index, with `kind` filtering and pagination.     |
| `fetch_page` | Fetch a page — or a single `#anchor` entry — from a site and return it as Markdown.               |

`list_kinds`, `search`, and `fetch_page` take an optional `site` argument (an
alias from `list_sites`); omit it to use the default site. All tools are
read-only and accept a `response_format` of `markdown` (default) or `json`.

Entry "kinds" are derived dynamically from each site's `xref.json`, so
project-specific domains (Lake commands, error explanations, …) are picked up
automatically — nothing about a particular site is hard-coded.

## Configuring sites

Set the `VERSO_MCP_SITES` environment variable to a comma-separated list of
`alias=url` pairs. A bare URL (no `alias=`) gets an alias derived from its path.

```
VERSO_MCP_SITES="lean-reference=https://lean-lang.org/doc/reference/latest/,
                 fpil=https://lean-lang.org/functional_programming_in_lean/,
                 tpil=https://lean-lang.org/theorem_proving_in_lean4/"
```

The first site listed is the default. If `VERSO_MCP_SITES` is unset, the server
defaults to a single site, the Lean Language Reference.

A site URL must be the root directory that contains `xref.json` (Verso writes it
there for Manual- and Tutorial-genre sites). The configured site roots also
serve as the network allowlist — see [Safety](#safety--etiquette).

## Requirements

[uv](https://docs.astral.sh/uv/). `uv` fetches the package and its dependencies
automatically on first launch — no manual virtualenv or `pip install` step.

## Use with Claude Code / Claude Desktop

Add an entry to your MCP configuration (`.mcp.json`, `~/.claude.json`, or
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "verso": {
      "type": "stdio",
      "command": "uvx",
      "args": ["verso-mcp"],
      "env": {
        "VERSO_MCP_SITES": "lean-reference=https://lean-lang.org/doc/reference/latest/, fpil=https://lean-lang.org/functional_programming_in_lean/"
      }
    }
  }
}
```

## Environment variables

All optional:

| Variable                    | Default                       | Purpose                                                          |
| --------------------------- | ----------------------------- | ---------------------------------------------------------------- |
| `VERSO_MCP_SITES`           | Lean Language Reference        | Comma-separated `alias=url` site list (see above).               |
| `VERSO_MCP_CACHE`           | `~/.cache/verso-mcp`           | Cache directory (each site cached in its own subdirectory).      |
| `VERSO_MCP_RATE_PER_SEC`    | `2`                            | Sustained outbound request rate (requests/second).              |
| `VERSO_MCP_RATE_BURST`      | `5`                            | Token-bucket burst capacity.                                     |
| `VERSO_MCP_RATE_MAX_WAIT`   | `3`                            | Max seconds to wait for a token before refusing.                |

## Safety & etiquette

> [!WARNING]
> [MCP servers have a lot of
> risks](https://www.redhat.com/en/blog/model-context-protocol-mcp-understanding-security-risks-and-controls).
> As far as MCP servers go, this one (`verso-mcp`) should be relatively
> innocuous: it is read-only, runs no shell commands, and only reaches the
> documentation sites you configure (or just the Lean Language Reference, if
> left unconfigured). The main caveat is that a malicious or compromised
> documentation site could try to steer the model via indirect prompt injection.
> `verso-mcp` cannot prevent that, so don't let an agent that uses it take
> consequential actions without your review.

The server is built to be a well-behaved client of documentation sites:

- **Scoped network access** — fetches are restricted to the *configured site
  roots*; the site list doubles as the allowlist. Enforced on the request URL
  *and* the final post-redirect URL, so a same-host URL outside a configured
  root is still refused. Path traversal (`..`, `%2e%2e`, backslash variants) is
  rejected.
- **Obeys `robots.txt`** — each host's `robots.txt` is fetched and respected
  for this server's `User-Agent`; a host can target it specifically with a
  `User-agent: verso-mcp` group.
- **Rate limiting** — a shared token bucket caps outbound requests across all
  sites (default 2 req/s, burst 5); a hit falls back to cached content rather
  than hammering the origin.
- **Caching & revalidation** — `xref.json` and pages are cached on disk per
  site (24 h TTL) with `ETag`/`If-None-Match` conditional revalidation, so a
  repeated lookup costs at most a `304 Not Modified`.
- **Bounded responses** — HTTP bodies are streamed with an 8 MB cap; Markdown
  output is capped at 200 KB; each site's page cache is LRU-evicted at 200 MB.
- **Identifying `User-Agent`** on every request.

## Evaluation

`evaluation.xml` is a 10-question evaluation suite in the format used by
Anthropic's `mcp-builder` skill. The questions target the default site (the
Lean Language Reference); each is read-only, independent, and has a single
stable, verifiable answer.

## Limitations

- Works with Verso **Manual**-genre sites (those that publish `xref.json`).
  Blog-genre sites have no `xref.json`. Tutorial-genre sites also emit one and
  should work, but are untested.
- `search` is **name-based** — it matches entry names and titles in the
  cross-reference index, the same granularity as Verso's own on-site search. It
  does not do full-text search of page bodies.
- The `xref.json` schema is an undocumented Verso internal; it may shift between
  Verso releases, which could break this MCP server if it doesn't keep up.

## Disclaimer

This project was written almost entirely by
[Claude Opus 4.7 (1M)](https://www.anthropic.com/news/claude-opus-4-7), an AI
assistant. It was built for my own personal use and is shared here only in case
it is useful to others. It comes with **no warranty whatsoever**. Use it at your
own risk.

<!-- mcp-name: io.github.nvlang/verso -->
