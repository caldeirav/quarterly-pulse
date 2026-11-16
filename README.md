# quarterly-pulse

AI agent CLI that automatically populates Markdown quarterly activity and
impact logs from GitHub, LinkedIn, and local contributor documents filtered by
a start date.

Give it your quarterly template (see
[`templates/quarterly-template.md`](templates/quarterly-template.md)), a
directory of documents you contributed, and a reporting window. The agent
extracts your contributor profile from Section 1 of the template, gathers
activity from all three sources concurrently, uses a LangChain-orchestrated
LLM to deduplicate and map every activity into the right section, and writes a
populated Markdown report with clean tables and a full audit footer.

## How it works

```
template (.md) ──► parser ──► contributor profile + section schemas (2-9)
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
   GitHub connector          LinkedIn connector         Local docs connector
   (GraphQL + Search,        (Portability API →          (md/txt/pdf/docx/pptx,
    gh CLI fallback)          export archive →            mtime or in-document
                              Apify/ScrapeCreators/       dates)
                              Exa → warn & skip)
        └──────────────────────────┼──────────────────────────┘
                                   ▼
                    LangChain synthesizer (LCEL, structured output,
                    deterministic dedup, chunked abatch for long quarters,
                    URL-grounding guardrail)
                                   ▼
                    renderer (template-preserving tables, pipe escaping,
                    <details> audit footer) ──► <template>_populated.md
```

- **Degradation, not failure**: if a data source is unconfigured or
  unavailable, the CLI logs a warning to stderr, records it in the audit
  footer, and continues with whatever sources are available.
- **Fail loudly on the LLM**: synthesis is the one hard dependency. Without a
  provider API key (or on a synthesis failure) the CLI exits non-zero with an
  actionable message and writes no partial output.
- **Grounding safety**: every URL in the generated rows is validated against
  the retrieved source items; anything ungrounded is stripped and reported.
  Pipes in cell text are escaped so tables always render.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill in your keys
```

## Usage

```bash
uv run quarterly-pulse \
  --template templates/quarterly-template.md \
  --docs-dir ./docs \
  --start-date 2026-07-01
```

| Flag | Required | Description |
| :--- | :--- | :--- |
| `--template` | yes | Input Markdown template file. |
| `--docs-dir` | yes | Directory of contributed documents (`.md`, `.txt`, `.pdf`, `.docx`, `.pptx`, plus optional LinkedIn export CSVs/ZIPs). |
| `--start-date` | yes | Start date (`YYYY-MM-DD`) for filtering activity and documents. |
| `--end-date` | no | End date (`YYYY-MM-DD`); defaults to today. |
| `--github` | no | Override the GitHub handle parsed from the template. |
| `--linkedin` | no | Override the LinkedIn profile URL/handle parsed from the template. |
| `--model` | no | LangChain model override (e.g. `openai:gpt-4o`, `anthropic:claude-sonnet-4-5`); defaults to `LLM_MODEL`. |
| `--output` | no | Output `.md` path; defaults to `<template_name>_populated.md` in the current directory. |

## Configuration

All configuration is environment-driven (process env or `.env`):

| Variable | Purpose |
| :--- | :--- |
| `LLM_MODEL` | LangChain model identifier (default `openai:gpt-4o`). |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Provider key matching `LLM_MODEL`. |
| `GITHUB_TOKEN` | GitHub PAT for the GraphQL + Search APIs. When unset, the connector falls back to your authenticated `gh` CLI (`gh api graphql`). |
| `LINKEDIN_ACCESS_TOKEN` | LinkedIn Member Data Portability API token (strategy a). |
| `APIFY_API_TOKEN` | Runs a LinkedIn profile-posts scraper Actor (strategy c, first match). Defaults to the `apimaestro/linkedin-profile-posts` Actor (no cookies, ~$5 per 1,000 posts). |
| `APIFY_LINKEDIN_ACTOR` | Override the Apify Actor ID (Store actors come and go). |
| `APIFY_LINKEDIN_INPUT` | Optional raw JSON run input for a custom Actor; supports `{username}` / `{profile_url}` placeholders. |
| `SCRAPECREATORS_API_KEY` | ScrapeCreators LinkedIn profile/posts endpoint (strategy c, second match). |
| `EXA_API_KEY` | Exa `site:linkedin.com` search for posts/articles (strategy c, last match). |

### LinkedIn fallback chain

1. **Member Data Portability API** — `GET /rest/memberSnapshotData`
   (`SHARE_HISTORY` / `ARTICLES` / `COMMENTS`), filtered by the start date.
2. **Local export archive** — a LinkedIn data export folder or `.zip` inside
   `--docs-dir` (`Shares.csv`, `Articles/*.html`, `Positions.csv`), parsed
   with the same schema mapper as the API.
3. **Third-party public post APIs** — Apify Actor → ScrapeCreators → Exa, in
   that order.
4. **Graceful degradation** — a warning explaining exactly which env var or
   export file to add; the run continues.

## Output guarantees

- Section 1 and the section headings, helper prompts, and table column
  schemas of Sections 2–9 are preserved verbatim.
- Italicized example rows (`*e.g., ...*`) and `*Copy/paste row to add item*`
  placeholders are replaced with generated rows; sections without grounded
  data keep their copy/paste placeholders for manual completion.
- Section 6's `*<Rename or Copy/Paste Table for Extra Categories>*` heading is
  renamed by the LLM when content lands there.
- Sections 8/9 are populated only when the sources contain explicit
  forward-looking commitments or escalations.
- A collapsible `<details>` audit footer records sources queried, strategies
  used, item counts, deduplication stats, the model, empty sections, and any
  fallback warnings or grounding fixes.

## Development

```bash
uv run pytest                        # full offline test suite
uv run quarterly-pulse --help        # CLI surface
```

The test suite is fully offline: `respx` mocks the HTTP connectors,
subprocess/Apify calls are monkeypatched, and LangChain chains run against a
`FakeListChatModel` subclass with scripted structured outputs. Fixtures live
in `tests/fixtures/` (GitHub GraphQL/Search responses, a LinkedIn export
archive, sample documents); `.docx`/`.pptx`/`.pdf` fixtures are generated at
runtime.

```
src/quarterly_pulse/
├── cli.py           # Typer entrypoint: flags, wiring, fail-loudly LLM setup
├── config.py        # pydantic-settings (env / .env)
├── models.py        # ActivityItem, ConnectorResult, TemplateProfile, audit schemas
├── parser.py        # Section 1 extraction; Sections 2-9 schema capture
├── synthesizer.py   # LangChain LCEL chains, QuarterReport, dedup, grounding
├── renderer.py      # template-preserving table population, audit footer
└── connectors/
    ├── github.py    # GraphQL contributionsCollection + Search API + gh CLI
    ├── linkedin.py  # prioritized fallback chain
    └── docs.py      # local document scanner (pypdf/python-docx/python-pptx)
```

## License

MIT — see [LICENSE](LICENSE).
