# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public
issue.

Use GitHub's private vulnerability reporting: open the repository's
**[Security tab](https://github.com/nvlang/verso-mcp/security)** and click
**"Report a vulnerability"**. The report stays visible only to you and the
maintainer.

`verso-mcp` is a small, personal open-source project maintained on a
best-effort basis (see the [disclaimer](README.md#disclaimer)) — there are no
formal response-time guarantees, but security reports are prioritised over
other work.

## Supported versions

Only the most recent release receives fixes. Because `verso-mcp` declares its
dependencies as version floors, a fix in a dependency reaches users on
reinstall, without a new `verso-mcp` release.

## Scope

`verso-mcp` is a read-only server that fetches and reformats public
documentation. Its security-relevant surfaces are the network-egress controls
(the SSRF allowlist, post-redirect re-checks, path-traversal rejection), the
resource bounds (response and output caps, the rate limiter), and `robots.txt`
handling.

*Indirect prompt injection* via fetched documentation is an inherent property
of any documentation-fetching tool, not a vulnerability in `verso-mcp` — see
the security note in the README.
