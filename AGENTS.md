# Agent Directives & Repository Guidelines

Welcome to the `quarterly-pulse` project. When operating in this repository, you must adhere to the following tools, conventions, and architectural guidelines.

## 1. Package Management: `uv`

This project strictly uses **`uv`** as its package manager and Python environment resolver.

* **NEVER** use `pip`, `poetry`, `conda`, or `pipenv`.
* To add dependencies, use: `uv add <package>`
* To add development dependencies, use: `uv add --dev <package>`
* To run scripts or tests, prefix with `uv run`: e.g., `uv run pytest` or `uv run quarterly-pulse`
* To sync or lock dependencies, use: `uv sync` or `uv lock`
* Target Python `3.12+` using a standard `src/quarterly_pulse/` package layout in `pyproject.toml`.

## 2. Core Tech Stack

* **CLI Framework:** `typer` (keep CLI entrypoints lean in `src/quarterly_pulse/cli.py`; delegate orchestration to core modules).
* **Configuration & Schemas:** `pydantic` and `pydantic-settings` (V2 syntax required).
* **LLM Framework (LangChain):** `langchain`, `langchain-core`, and provider packages (`langchain-openai`, `langchain-anthropic`).
  * Initialize models via `langchain.chat_models.init_chat_model` so model/provider selection is configurable via environment variables without code changes.
  * Compose prompts and chains using LCEL (`ChatPromptTemplate`) and bind Pydantic schemas via `.with_structured_output(...)` for deterministic classification and table row synthesis.
  * Use async LangChain execution (`.ainvoke()`, `.abatch()`) for all LLM calls.
* **HTTP Client:** `httpx` (use `httpx.AsyncClient` for GitHub GraphQL and LinkedIn connectors).
* **Document Parsing:** `pypdf`, `python-docx`, `python-pptx` (wrapped cleanly in the local docs connector; handle unreadable or corrupt files gracefully).
* **Testing:** `pytest`, `pytest-asyncio`, `respx` (for mocking `httpx` calls), and `GenericFakeChatModel` / `FakeListChatModel` from `langchain_core.language_models.fake_chat_models` for deterministic offline unit tests.

## 3. Coding Standards & Architecture

* **Type Hinting:** All functions, methods, and variables must have strict type hints.
* **Async First:** I/O-bound tasks (GitHub GraphQL, LinkedIn APIs, LangChain model invocations) must be implemented asynchronously (`async`/`await`) and executed concurrently via `asyncio.gather` where independent.
* **Error Handling & Graceful Degradation:** The CLI must not crash if a secondary data source (such as LinkedIn) fails or is unconfigured. Log a clear warning to `stderr`, record it for the output audit footer, and proceed with available data.
* **Markdown & Grounding Safety:**
  * Aggressively escape pipe characters (`|`) in cell strings before injecting them into Markdown tables so table rendering never breaks.
  * Enforce strict source grounding in LangChain system prompts: never fabricate customer names, metrics, or URLs that are absent from retrieved `ActivityItem` records.

## 4. Repository Layout & Workflow

* **Modular Structure:** Keep responsibilities cleanly separated under `src/quarterly_pulse/`:
  * `connectors/` — Async GitHub, LinkedIn, and local document extractors returning normalized `ActivityItem` models.
  * `parser.py` — Markdown template parser extracting Section 1 metadata and preserving Sections 2–9 table schemas.
  * `synthesizer.py` — LangChain runnables for deduplicating items, routing them to template sections, and generating concise table entries.
  * `formatter.py` — Safe Markdown table population and `<details>` audit footer generation.
  * `cli.py` — Typer CLI entrypoint wired to `[project.scripts]` as `quarterly-pulse`.
* **Read Before Writing:** Always inspect `templates/quarterly-template.md` (or the user-provided template) to verify exact headings and column schemas before modifying parser or formatter logic.
* **Test-Driven:** When adding or updating a connector, parser rule, or LangChain chain, add mock fixtures in `tests/fixtures/` and verify with `uv run pytest`.

## 5. Execution Context

When running or testing the CLI locally, formulate commands using:
`uv run quarterly-pulse --template templates/quarterly-template.md --docs-dir ./docs --start-date 2026-07-01`
(Adjust paths and dates based on the current task context).