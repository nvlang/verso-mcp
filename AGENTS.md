# AGENTS.md

Guidance for AI agents (and humans) asked to maintain or modify this repository.

This file follows the [AGENTS.md](https://agents.md) convention — a plain
Markdown file of instructions that agentic coding tools read before working in a
repo. It is the **canonical** maintenance guide for `verso-mcp`; if anything
here conflicts with a guess or a habit, this file wins.

The owner wants this repository to need **near-zero ongoing maintenance**. Most
"maintenance" (dependency bumps) is automated and should require no human or
agent action. Read the relevant section below before changing anything, keep
changes small, and do not invent work.

## What this project is

`verso-mcp` is a small, single-purpose [MCP](https://modelcontextprotocol.io)
server: it lets an AI agent search and read documentation built with
[Verso](https://github.com/leanprover/verso), Lean's documentation tool. It
exposes exactly four read-only tools — `list_sites`, `list_kinds`, `search`,
`fetch_page` — over stdio.

It is deliberately small. **Resist scope creep.** New tools, new dependencies,
new config surface, and "frameworks" are almost always the wrong call. A change
that makes the server bigger needs a strong, specific justification.

## Repository layout

| Path | Purpose |
| ---- | ------- |
| `src/verso_mcp/server.py` | The entire server. One file on purpose. |
| `src/verso_mcp/__init__.py`, `__main__.py` | Thin entry-point shims. |
| `tests/` | Offline pytest suite (`test_core.py`, `test_fetch.py`, `conftest.py`). |
| `pyproject.toml` | Packaging, dependencies, tool config (ruff, pytest, commitizen). |
| `server.json` | MCP registry manifest. |
| `evaluation.xml` | 10-question eval suite (mcp-builder format). |
| `.github/workflows/` | CI, release, Scorecard, Dependabot auto-merge. |

The version number lives in **one** place — `__version__` in `server.py` — and
is surfaced everywhere else dynamically (hatchling) or by `cz bump`. Never edit a
version string by hand.

## Development

Everything goes through [uv](https://docs.astral.sh/uv/). Python 3.11+.

```sh
uv sync                       # install deps + dev tools
uv run pytest                 # run the test suite (offline, <1s)
uv run ruff check .           # lint
uv run ruff format .          # format
uv run verso-mcp --smoke      # offline self-check (hits the live default site)
```

`pre-commit` is configured (`.pre-commit-config.yaml`); `uv run pre-commit
install` once to enable it locally.

## Security invariants — DO NOT REGRESS

This server is built to be driven by a *possibly hostile or confused* agent. The
behaviours below are load-bearing. Each is pinned by a test; **if you change
code near one, the test must still pass, and you must not weaken the test.** If
a change genuinely requires altering one of these, stop and surface it to the
owner rather than deciding alone.

| Invariant | Where | Pinned by |
| --------- | ----- | --------- |
| Network access is allowlisted to configured site roots (SSRF guard). | `_site_for_url` | `test_core.py::test_site_for_url_*` |
| The allowlist is re-checked on the **post-redirect** URL. | `_cached_get` | `test_fetch.py::test_cached_get_rejects_offsite_redirect` |
| Path traversal is rejected in every encoding (`..`, `%2e%2e`, `..\`). | `_path_has_traversal`, `_resolve_page_url` | `test_core.py::test_path_has_traversal_detects`, `test_resolve_page_url_rejects_traversal` |
| `https://` only — plain HTTP is refused. | `_normalize_root`, `_site_for_url`, `_resolve_page_url` | `test_core.py::test_*_rejects_plain_http` |
| `robots.txt` is fetched and obeyed for this server's User-Agent. | `_robots_allowed`, `_cached_get` | `test_fetch.py::test_robots_allowed`, `test_cached_get_obeys_robots` |
| Outbound requests pass a shared token-bucket rate limiter. | `_acquire_request_token` | `test_fetch.py::test_acquire_request_token_succeeds_then_rejects` |
| HTTP bodies are capped (decompression-bomb guard). | `_cached_get` | `test_fetch.py::test_cached_get_byte_cap` |
| Only `text/html` / `application/json` responses are accepted. | `_cached_get` | `test_fetch.py::test_cached_get_rejects_bad_content_type` |
| Output returned to the agent is size-capped. | `_cap_output` | `test_core.py::test_cap_output_over_limit` |
| Off-site addresses in `xref.json` are dropped from the index. | `_build_index` | `test_core.py::test_build_index_skips_off_site_absolute_address` |

When you add a feature that makes a network request or handles a URL, add a test
in the same style and, if it introduces a new invariant, add a row here.

## Making a change

1. Make the change in `src/verso_mcp/server.py` (or `tests/`).
2. `uv run ruff check . && uv run ruff format . && uv run pytest` — all must pass.
3. Commit using **Conventional Commits** (see below). CI rejects commits that
   don't parse, and `cz bump` relies on the prefixes.
4. Do **not** bump the version or edit `CHANGELOG.md` by hand — `cz bump` does
   both (see Releasing).

Keep the public surface stable: the four tool names, their parameters, and the
`VERSO_MCP_*` environment variables are a contract with MCP clients. Renaming or
removing any of them is a **breaking change** (`feat!:` / `BREAKING CHANGE:`).

### Conventional Commits

Format: `type(optional-scope): summary`. Types that matter here:

- `feat:` — new behaviour. Triggers a **minor** release.
- `fix:` — bug fix. Triggers a **patch** release.
- `feat!:` / any `BREAKING CHANGE:` footer — breaking change.
- `docs:`, `test:`, `ci:`, `build:`, `refactor:`, `chore:` — **no release**.

Dependabot is configured to prefix its commits `build:` / `ci:`, so dependency
updates never trigger a release on their own.

## Releasing

A release happens when, and only when, `verso-mcp`'s **own code** changes in a
user-visible way (a `feat:` or `fix:`). There is no time-based release schedule
and you should not add one.

To cut a release (run on a clean `main`):

```sh
uv run cz bump            # bumps __version__ + CHANGELOG.md, commits, tags vX.Y.Z
git push --follow-tags    # pushing the tag triggers .github/workflows/release.yml
```

`cz bump` chooses the new version from the commit history. If it reports that no
commits would cause a bump, then there is nothing to release — that is the
correct outcome, not a problem to fix. The project is in `0.x`
(`major_version_zero = true`), so breaking changes bump the minor, not the
major.

The `vX.Y.Z` tag triggers `release.yml`, which builds the package, publishes it
to PyPI via Trusted Publishing with [PEP 740](https://peps.python.org/pep-0740/)
attestations, publishes the manifest to the MCP registry, and creates a GitHub
release. Pushing to `main` *without* a tag runs CI only — it never publishes.

## Dependency updates & publishing cadence

This is the most common maintenance question. The short answer: **you almost
never need to do anything, and dependency updates do not need a release.**

- **Dependencies are declared as floors** (`httpx>=0.27`, …) in
  `pyproject.toml`. When an end user installs `verso-mcp` (`uvx verso-mcp` /
  `pip install`), their resolver picks the newest compatible versions. So a
  security fix in `httpx`, `mcp`, `pydantic`, or `html2text` reaches users the
  moment they (re)install — **without** a `verso-mcp` release.
- **`uv.lock` is committed**, but it only pins versions for *this repo's* CI and
  local development. It is not shipped in the wheel and does not constrain end
  users.
- **Dependabot** (monthly, grouped, quiet) keeps `uv.lock` and the GitHub
  Actions current. Non-major updates are **auto-merged** once CI is green
  (`dependabot-auto-merge.yml`). The test suite is the safety net: a dependency
  update that breaks the server fails CI, and the auto-merge will not complete.
- **Major-version dependency bumps are *not* auto-merged.** Dependabot opens a
  PR and leaves it for review. That is the one recurring task that may need a
  human or a tasked agent: check the PR, let CI run, and merge if green (or fix
  the breakage first). This is rare.

**So: do not publish on every dependency update, and do not publish on a fixed
schedule.** Release only on a `feat:`/`fix:` to `verso-mcp`'s own code. A
security issue worth a release would be a bug in *this* code — and that is a
`fix:`, already covered by the normal release trigger.

## Before the first publish

The in-repo identifiers are set — `server.json`, `README.md`, and
`pyproject.toml` all point at `nvlang/verso-mcp`. What remains is one-time
account and repository setup that cannot live in the repo:

- **PyPI Trusted Publishing** — register this repository and the `release.yml`
  workflow as a trusted publisher for the `verso-mcp` PyPI project (no
  environment name). Until this is done the `pypi` release job cannot upload.
- **Repository settings** — enable "Allow auto-merge", add a branch-protection
  rule on `main` that requires the `quality` and `commits` CI checks, and allow
  GitHub Actions to approve pull requests. Without these the Dependabot
  auto-merge workflow cannot work (or would merge without waiting for CI).

`release.yml` still guards `server.json`: if the `YOUR_GITHUB_USERNAME`
placeholder is ever reintroduced, the MCP-registry publish is skipped rather
than publishing something broken.

## When unsure

Prefer the smallest change that works. Do not add dependencies, tools,
workflows, or configuration that the owner did not ask for. If a task seems to
require weakening a security invariant, expanding the tool surface, or setting
up a release cadence, that is a signal to stop and ask rather than proceed.
