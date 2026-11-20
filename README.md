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
    paginated, aggregated    export archive →            mtime or in-document
    per repository,          Apify/ScrapeCreators/       dates)
    gh CLI fallback)         Exa → warn & skip)
        └──────────────────────────┼──────────────────────────┘
                                   ▼
                    LangChain synthesizer (LCEL, structured output,
                    deterministic dedup, chunked abatch for long quarters,
                    completeness retry pass, URL-grounding guardrail)
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
- **Nothing gathered gets dropped**: a deterministic coverage check verifies
  that every item's URLs, document path, repository, or title appear in the
  draft; left-out records get a batched model retry pass against the live
  draft, and anything still unrepresented is deterministically appended to
  Section 6 as an auto-added row — every gathered item, including every
  LinkedIn post sharing attached content, lands somewhere.
- **Grounding safety**: every URL in the generated rows is validated against
  the retrieved source items (prefix-tolerant, so long links the model
  slightly truncated still count as grounded); anything ungrounded is stripped
  and reported. Pipes in cell text are escaped so tables always render.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill in your keys
```

## Operational files

Inputs and generated artifacts never belong in git; each has a designated
directory (all gitignored), and the commands default to them so the
repository root stays clean:

| Directory | Role |
| :--- | :--- |
| `docs/` | Input documents for `populate` (`.md`, `.txt`, `.pdf`, `.docx`, `.pptx`, LinkedIn exports). |
| `out/` | Generated artifacts: populated reports (`out/<template>_populated.md`), gather caches, QBR decks with companion records, and the agent-written `qbr-content.json`. |
| `qbr-reports/` | Populated reports aggregated by `qbr`, plus an optional `qbr-config.yaml`. |

## Usage

Two commands: `populate` fills a quarterly activity log; `qbr` builds a
leader QBR deck from populated reports.

```bash
uv run quarterly-pulse populate \
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
| `--cache` | no | JSON file caching everything the connectors extracted. When it exists, connectors are skipped and only the report is regenerated. |
| `--refresh` | no | With `--cache`: re-gather from all sources, overwriting the cache. |

## Caching: regenerate output without re-scraping

Gathering from GitHub, LinkedIn (Apify), and your docs directory is the slow,
costly part; iterating on output rules is not. Use `--cache` to keep what was
extracted and regenerate only the report:

```bash
# First run: gathers all sources, writes the cache, generates the report.
uv run quarterly-pulse populate --template templates/quarterly-template.md \
  --docs-dir ./docs --start-date 2026-07-01 --cache out/qp-cache.json

# Later runs: connectors are skipped entirely; only synthesis + rendering run
# (seconds, no Apify credit, no GitHub API calls).
uv run quarterly-pulse populate --template templates/quarterly-template.md \
  --docs-dir ./docs --start-date 2026-07-01 --cache out/qp-cache.json

# When your sources actually changed: re-gather and overwrite the cache.
uv run quarterly-pulse populate --template templates/quarterly-template.md \
  --docs-dir ./docs --start-date 2026-07-01 --cache out/qp-cache.json --refresh
```

The cache stores every gathered `ActivityItem`, the strategy and warnings per
source, the reporting window, and the contributor profile, so the audit footer
stays accurate for cached runs (it notes the gather timestamp). If the
requested window or handles differ from the cached gather, the CLI warns and
suggests `--refresh`.

## QBR Architect: leader deck from populated reports

`quarterly-pulse qbr` implements the
[`skills/qbr-architect/SKILL.md`](skills/qbr-architect/SKILL.md) contract: it
aggregates every populated quarterly report (`*.md`) in a source directory
into an editable four-slide executive QBR deck — Executive Readout, Execution
Deep-Dive, Looking Ahead, and Executive Escalations & Radar — filled in place
into `skills/qbr-architect/assets/qbr-template.pptx`. Slot text is replaced
run-by-run, so bold titles, regular details, and the lowlights' red
`Mitigation:` runs keep the template's fonts and colors.

```bash
uv run quarterly-pulse qbr make --reports-dir ./qbr-reports
```

| Command | Purpose |
| :--- | :--- |
| `qbr make` | End-to-end: interview (LLM), synthesize, render. |
| `qbr rows` | Emit the aggregated row inventory (`row_id` per row) and resolved deck config as JSON. |
| `qbr render` | Fill the template from a validated `qbr-content.json` — no LLM. |
| `qbr schema` | Emit the JSON Schema for `qbr-content.json`. |

| Flag | Required | Description |
| :--- | :--- | :--- |
| `--reports-dir` | yes | Directory of populated reports (`*.md`), plus an optional `qbr-config.yaml`. |
| `--pptx` | no | QBR PPTX template; defaults to `skills/qbr-architect/assets/qbr-template.pptx`. |
| `--output` | no | Output `.pptx`; defaults to `out/<org>_<review-quarter>_QBR.pptx`. |
| `--model` | no | LangChain model override (`make` only); defaults to `LLM_MODEL`. |
| `--leader` / `--org` | no | Override the leader / organization from `qbr-config.yaml`. |
| `--review-quarter` / `--preview-quarter` | no | Quarter labels (e.g. `Q3 2026`); preview rolls over from review (Q4 → Q1). |
| `--no-interview` | no | `make` only: skip the clarifying interview and draft with gaps noted. |

Deck metadata comes from a `qbr-config.yaml` beside the reports (leader, org,
review/preview quarters); flags override, and the review quarter falls back to
the reports' Section 1 consensus when every report agrees. Nothing is inferred
from filenames or today's date.

**Clarifying interview (`make`).** By default the CLI plans up to 10
prioritized questions from the gaps the reports leave against the four-slide
contract and asks them one at a time (`unknown`, `skip`, and `draft` are
valid answers; after 10 it offers continue-or-draft). `--no-interview`
drafts directly, and a non-interactive stdin skips the interview with a
warning.

**Agent-driven execution (OpenCode).** The skill is registered with OpenCode
via `opencode.jsonc` (`"skills": ["./skills"]`), so in an OpenCode session you
can say `@qbr-architect produce my QBR` and the agent runs the judgment steps
itself — no API key needed:

1. `quarterly-pulse qbr rows --reports-dir ./qbr-reports` — the agent reads
   the row inventory; `row_id`s are the only citation vocabulary.
2. It asks the clarifying questions in chat, following the skill's interview
   rules.
3. It writes `out/qbr-content.json` (validated against `qbr schema`) citing
   `row_id`s — never URLs, which the renderer resolves into locators.
4. `quarterly-pulse qbr render --content out/qbr-content.json
   --reports-dir ./qbr-reports` fills the deck deterministically and prints
   warnings (unknown citations, unfilled prompts) the agent fixes and
   re-renders until clean.

**Grounding.** The synthesizer (LLM or agent) cites source row IDs, never
URLs; the renderer resolves those IDs into locators from the parsed reports,
so slide links cannot be fabricated. Speaker notes carry source locators,
objective/KR mappings, stop linkages, and contributor attribution; a
companion `<deck>_sources.md` record lists unresolved gaps by slide.

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

## Synthesis: completeness and routing rules

The synthesizer's contract is that **every gathered item lands in the
report**:

1. **First pass** — all records go to the model in one structured-output call
   (chunked pre-summarization first when the quarter exceeds the context
   budget).
2. **Coverage check** — a deterministic pass looks for each item's
   identifying markers (URLs, document path, repository name, title) in the
   generated rows.
3. **Retry pass** — left-out records are re-sent to the model in small
   sequential batches, each seeing the draft updated by the previous batch
   (one huge retry call invites the model to consolidate distinct posts back
   into topic-level rows). Coverage is judged strictly per item: a record
   counts as covered only when a row cites the same link or is literally the
   same item (same post URL, same document, same repository) — two posts
   about the same event are still two rows. Retry rows that only re-cite
   URLs the draft already has are dropped as duplicates.
4. **Backstop** — whatever the retry still leaves out is deterministically
   appended to Section 6 as an `auto-added` row for manual re-placement, so
   no gathered item — including any LinkedIn post sharing attached content —
   is ever dropped from the report.

The model follows — and the connectors pre-tag records for — these routing
and row-quality rules:

- GitHub activity arrives aggregated **one item per repository**, with the
  repo description and the individual PR/issue URLs kept for citation; the
  report renders one row per repo, leading with what the repo is and what the
  contributor did there.
- **Every LinkedIn post and every local document becomes its own row.** Posts
  that announce, follow, or recap an event (keyword-tagged by the connector)
  route to Section 5, or Section 4 when the event is customer-specific —
  never to the Section 6 catch-all. Posts sharing attached slides, articles,
  or documents are never merged into other rows.
- Event series (a summit touring several cities, repeated briefings) produce
  **one row per event or location**.
- Exported emails in the docs directory are detected by their headers and
  correlated with the events they concern instead of being listed one by one.
- Row titles are concise contributor-POV names of the activity; the
  activity/outcome cell is a 2-3 sentence external assessment of what was
  accomplished and why it matters, not a clipped quote of the source text.

## Output guarantees

- Section 1 and the section headings, helper prompts, and table column
  schemas of Sections 2–9 are preserved verbatim.
- Italicized example rows (`*e.g., ...*`) and `*Copy/paste row to add item*`
  placeholders are replaced with generated rows; sections without grounded
  data keep their copy/paste placeholders for manual completion.
- Section 6's `*<Rename or Copy/Paste Table for Extra Categories>*` heading is
  renamed by the LLM when content lands there.
- Sections 8/9 contain LLM-proposed recommendations for next quarter and for
  help needed, derived from the gathered activity, for manual editing.
- A collapsible `<details>` audit footer records sources queried, strategies
  used, item counts, deduplication stats, the model, per-section row counts,
  empty sections, backstop details, and any fallback warnings or grounding
  fixes.

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
├── cli.py           # Typer entrypoint: populate + qbr commands, fail-loudly LLM setup
├── config.py        # pydantic-settings (env / .env)
├── models.py        # ActivityItem, ConnectorResult, TemplateProfile, audit schemas
├── parser.py        # Section 1 extraction; Sections 2-9 schema capture
├── synthesizer.py   # LangChain LCEL chains, QuarterReport, dedup, coverage retry, grounding
├── renderer.py      # template-preserving table population, audit footer
├── qbr.py           # QBR Architect core: aggregation, interview planning, QbrContent synthesis
├── qbr_renderer.py  # in-place PPTX slot filling, speaker notes, companion record
└── connectors/
    ├── github.py    # GraphQL contributionsCollection + Search API + gh CLI
    ├── linkedin.py  # prioritized fallback chain
    └── docs.py      # local document scanner (pypdf/python-docx/python-pptx)
```

## License

MIT — see [LICENSE](LICENSE).
