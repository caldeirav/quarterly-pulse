"""Typer CLI entrypoint for quarterly-pulse.

Kept lean per repository conventions: argument parsing, configuration, and
orchestration. The pipeline itself lives in :mod:`quarterly_pulse.parser`,
:mod:`quarterly_pulse.connectors`, :mod:`quarterly_pulse.synthesizer`, and
:mod:`quarterly_pulse.renderer`.

Behavioral contract:

* Secondary data sources (GitHub without a token, LinkedIn without any
  configured strategy, unreadable documents) degrade gracefully with warnings
  on stderr recorded in the audit footer.
* The LLM stage is the one hard dependency: with no provider API key, or on a
  synthesis failure, the CLI exits non-zero with an actionable message and
  writes no partial output.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Coroutine
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from quarterly_pulse.config import Settings
from quarterly_pulse.connectors.docs import fetch_docs_activity
from quarterly_pulse.connectors.github import fetch_github_activity
from quarterly_pulse.connectors.linkedin import (
    fetch_linkedin_activity,
    find_linkedin_export,
    linkedin_exclusions,
)
from quarterly_pulse.models import (
    AuditReport,
    ConnectorResult,
    SourceAudit,
    TemplateProfile,
)
from quarterly_pulse.parser import parse_template
from quarterly_pulse.renderer import SECTION_FIELDS, render_report
from quarterly_pulse.synthesizer import SynthesisResult, synthesize_report

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="quarterly-pulse",
    help=(
        "Populate a Markdown quarterly activity & impact log from GitHub, "
        "LinkedIn, and local contributor documents."
    ),
    no_args_is_help=True,
    add_completion=False,
)

TemplateOption = Annotated[
    Path,
    typer.Option(
        "--template",
        help="Path to the input Markdown template file.",
        exists=True,
        dir_okay=False,
        readable=True,
        show_default=False,
    ),
]

DocsDirOption = Annotated[
    Path,
    typer.Option(
        "--docs-dir",
        help="Directory containing contributed documents (.md, .txt, .pdf, .docx, .pptx, LinkedIn exports).",
        exists=True,
        file_okay=False,
        readable=True,
        show_default=False,
    ),
]

StartDateOption = Annotated[
    date,
    typer.Option(
        "--start-date",
        help="Start date (YYYY-MM-DD) for filtering activity and documents.",
        show_default=False,
    ),
]

EndDateOption = Annotated[
    Optional[date],
    typer.Option(
        "--end-date",
        help="End date (YYYY-MM-DD); defaults to today.",
    ),
]

GithubOption = Annotated[
    Optional[str],
    typer.Option(
        "--github",
        help="Override the GitHub handle parsed from the template.",
    ),
]

LinkedinOption = Annotated[
    Optional[str],
    typer.Option(
        "--linkedin",
        help="Override the LinkedIn profile URL/handle parsed from the template.",
    ),
]

ModelOption = Annotated[
    Optional[str],
    typer.Option(
        "--model",
        help="LangChain model identifier override (e.g. openai:gpt-4o, anthropic:claude-sonnet-4-5).",
    ),
]

OutputOption = Annotated[
    Optional[Path],
    typer.Option(
        "--output",
        help="Output .md path (default: <template_name>_populated.md in the current directory).",
    ),
]


@app.command()
def populate(
    template: TemplateOption,
    docs_dir: DocsDirOption,
    start_date: StartDateOption,
    end_date: EndDateOption = None,
    github: GithubOption = None,
    linkedin: LinkedinOption = None,
    model: ModelOption = None,
    output: OutputOption = None,
) -> None:
    """Populate a quarterly activity log template with gathered activity data."""
    _configure_logging()
    resolved_end = end_date or date.today()
    if resolved_end < start_date:
        typer.secho(
            f"--end-date ({resolved_end}) is before --start-date ({start_date}).",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    settings = Settings()
    model_id = model or settings.llm_model
    output_path = output or Path(f"{template.stem}_populated.md")

    parsed = parse_template(template)
    profile = _apply_overrides(parsed.profile, github=github, linkedin=linkedin)
    _report_profile(profile)

    chat_model = _init_model(model_id, settings)

    connector_results = asyncio.run(
        _gather_activity(profile, docs_dir, start_date, resolved_end, settings)
    )
    items = [item for result in connector_results for item in result.items]
    _report_connectors(connector_results)

    try:
        synthesis = asyncio.run(synthesize_report(profile, items, chat_model))
    except Exception as exc:  # fail loudly: no partial output on LLM failure
        typer.secho(f"LLM synthesis with '{model_id}' failed: {exc}", err=True, fg=typer.colors.RED)
        typer.secho(
            "Check your provider API key (OPENAI_API_KEY / ANTHROPIC_API_KEY) and LLM_MODEL, then re-run.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    audit = _build_audit(connector_results, synthesis, model_id)
    rendered = render_report(parsed, synthesis.report, audit)
    output_path.write_text(rendered, encoding="utf-8")

    typer.echo(
        f"Wrote {output_path} "
        f"({synthesis.items_received} items gathered, {synthesis.items_after_dedup} after dedup, "
        f"{len(synthesis.grounding_violations)} ungrounded URLs removed)."
    )


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------


async def _gather_activity(
    profile: TemplateProfile,
    docs_dir: Path,
    start_date: date,
    end_date: date,
    settings: Settings,
) -> list[ConnectorResult]:
    """Run the three connectors concurrently, degrading instead of failing."""
    archive = find_linkedin_export(docs_dir)
    exclusions = linkedin_exclusions(archive, docs_dir)
    contributor_name = (profile.name_role or "").split(",")[0].strip() or None

    async def _github() -> ConnectorResult:
        if not profile.github_handle:
            return ConnectorResult(
                source="github",
                strategy="skipped",
                warnings=[
                    "No GitHub handle in template Section 1 and no --github override; GitHub skipped."
                ],
            )
        return await fetch_github_activity(profile.github_handle, start_date, end_date, settings)

    github_result, linkedin_result, docs_result = await asyncio.gather(
        _safe_connector("github", _github()),
        _safe_connector(
            "linkedin",
            fetch_linkedin_activity(
                profile.linkedin_url, contributor_name, start_date, end_date, docs_dir, settings
            ),
        ),
        _safe_connector(
            "docs", fetch_docs_activity(docs_dir, start_date, end_date, exclude=exclusions)
        ),
    )
    return [github_result, linkedin_result, docs_result]


async def _safe_connector(
    source: str, coro: Coroutine[Any, Any, ConnectorResult]
) -> ConnectorResult:
    """Await one connector; convert any crash into a degraded result.

    Belt-and-braces for the repository's graceful-degradation contract: a bug
    inside any connector must never take the whole pipeline down.
    """
    try:
        return await coro
    except Exception as exc:
        logger.warning("%s connector crashed: %s", source, exc)
        return ConnectorResult(
            source=source,
            strategy="failed",
            warnings=[f"{source} connector crashed and was skipped: {exc}"],
        )


def _apply_overrides(
    profile: TemplateProfile, github: str | None, linkedin: str | None
) -> TemplateProfile:
    """Apply CLI handle/URL overrides on top of the parsed template profile."""
    data = profile.model_dump()
    if github:
        data["github_handle"] = github.lstrip("@").strip()
    if linkedin:
        data["linkedin_url"] = linkedin.strip()
    return TemplateProfile(**data)


def _init_model(model_id: str, settings: Settings) -> BaseChatModel:
    """Build the chat model, failing loudly with an actionable message."""
    _export_provider_keys(settings)
    provider = model_id.split(":", 1)[0].lower() if ":" in model_id else "openai"
    provider_keys = {"openai": settings.openai_api_key, "anthropic": settings.anthropic_api_key}
    if provider in provider_keys and not provider_keys[provider]:
        typer.secho(
            f"No API key configured for model '{model_id}': set {provider.upper()}_API_KEY "
            "in the environment or .env, or pick another provider via LLM_MODEL / --model.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    try:
        return init_chat_model(model_id)
    except Exception as exc:
        typer.secho(f"Failed to initialize model '{model_id}': {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


def _export_provider_keys(settings: Settings) -> None:
    """Make .env-provided keys visible to langchain provider SDKs (env vars)."""
    if settings.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
    if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key


def _build_audit(
    connector_results: list[ConnectorResult],
    synthesis: SynthesisResult,
    model_id: str,
) -> AuditReport:
    """Assemble the audit footer data."""
    report = synthesis.report
    empty_sections = [
        number for number, field in SECTION_FIELDS.items() if not getattr(report, field)
    ]
    section_row_counts = {
        number: len(getattr(report, field)) for number, field in SECTION_FIELDS.items()
    }
    return AuditReport(
        sources=[
            SourceAudit(
                source=result.source,
                strategy=result.strategy,
                item_count=len(result.items),
                warnings=result.warnings,
            )
            for result in connector_results
        ],
        model=model_id,
        total_items=synthesis.items_received,
        items_after_dedup=synthesis.items_after_dedup,
        items_for_synthesis=synthesis.items_for_synthesis,
        chunked=synthesis.chunked,
        backfilled_count=synthesis.backfilled_count,
        section_row_counts=section_row_counts,
        empty_sections=empty_sections,
        grounding_violations=synthesis.grounding_violations,
    )


def _report_profile(profile: TemplateProfile) -> None:
    typer.secho(
        f"Contributor: {profile.name_role or 'unknown'} | "
        f"GitHub: {profile.github_handle or 'n/a'} | "
        f"LinkedIn: {profile.linkedin_url or 'n/a'} | "
        f"Quarter: {profile.quarter or 'n/a'}",
        err=True,
    )


def _report_connectors(connector_results: list[ConnectorResult]) -> None:
    """Print connector outcomes; warnings always go to stderr."""
    for result in connector_results:
        typer.secho(
            f"[{result.source}] strategy: {result.strategy}, items: {len(result.items)}",
            err=True,
        )
        for warning in result.warnings:
            typer.secho(f"[{result.source}] warning: {warning}", err=True, fg=typer.colors.YELLOW)


def _configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Third-party noise: pypdf warns about malformed PDF font metrics that do
    # not affect text extraction, and httpx request logs duplicate the CLI's
    # own per-source summaries (everything is recorded in the audit footer).
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    app()
