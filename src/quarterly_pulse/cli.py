"""Typer CLI entrypoint for quarterly-pulse.

Kept lean per repository conventions: argument parsing, configuration, and
orchestration. The pipeline itself lives in :mod:`quarterly_pulse.parser`,
:mod:`quarterly_pulse.connectors`, :mod:`quarterly_pulse.synthesizer`,
:mod:`quarterly_pulse.renderer`, :mod:`quarterly_pulse.qbr`, and
:mod:`quarterly_pulse.qbr_renderer`.

Commands:

* ``populate`` — populate a quarterly activity log template from GitHub,
  LinkedIn, and local documents.
* ``qbr`` — aggregate populated reports into a leader QBR deck following the
  ``skills/qbr-architect/SKILL.md`` contract.

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
from datetime import date, datetime
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
    GatherCache,
    SourceAudit,
    TemplateProfile,
)
from quarterly_pulse.parser import parse_template
from quarterly_pulse.qbr import (
    INTERVIEW_BUDGET,
    QbrConfig,
    load_team_reports,
    plan_interview,
    resolve_deck_config,
    source_manifest,
    synthesize_qbr,
)
from quarterly_pulse.qbr_renderer import default_output_path, render_qbr_deck
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

CacheOption = Annotated[
    Optional[Path],
    typer.Option(
        "--cache",
        help=(
            "Cache gathered activity to this JSON file. When the file exists, connectors "
            "are skipped and only the output is regenerated — ideal for iterating on "
            "output rules without re-scraping GitHub/LinkedIn/docs."
        ),
    ),
]

RefreshOption = Annotated[
    bool,
    typer.Option(
        "--refresh",
        help="With --cache: re-gather from all sources, overwriting the cache file.",
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
    cache: CacheOption = None,
    refresh: RefreshOption = False,
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

    cache_loaded: GatherCache | None = None
    if cache is not None and cache.exists() and not refresh:
        cache_loaded = _load_gather_cache(cache, start_date, resolved_end, profile)
        connector_results = cache_loaded.connector_results
    else:
        if refresh and cache is None:
            typer.secho(
                "--refresh has no effect without --cache.",
                err=True,
                fg=typer.colors.YELLOW,
            )
        connector_results = asyncio.run(
            _gather_activity(profile, docs_dir, start_date, resolved_end, settings)
        )
        if cache is not None:
            _write_gather_cache(cache, start_date, resolved_end, profile, connector_results)

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

    audit = _build_audit(connector_results, synthesis, model_id, cache_gathered_at=cache_loaded.gathered_at if cache_loaded else None)
    rendered = render_report(parsed, synthesis.report, audit)
    output_path.write_text(rendered, encoding="utf-8")

    typer.echo(
        f"Wrote {output_path} "
        f"({synthesis.items_received} items gathered, {synthesis.items_after_dedup} after dedup, "
        f"{len(synthesis.grounding_violations)} ungrounded URLs removed)."
    )


# ---------------------------------------------------------------------------
# qbr-architect: aggregate populated reports into a leader QBR deck
# ---------------------------------------------------------------------------

ReportsDirOption = Annotated[
    Path,
    typer.Option(
        "--reports-dir",
        help=(
            "Directory of populated quarterly reports (*.md) to aggregate, "
            "plus an optional qbr-config.yaml with leader/org/quarters."
        ),
        exists=True,
        file_okay=False,
        readable=True,
        show_default=False,
    ),
]

PptxOption = Annotated[
    Optional[Path],
    typer.Option(
        "--pptx",
        help="QBR PPTX template (default: skills/qbr-architect/assets/qbr-template.pptx).",
    ),
]

QbrOutputOption = Annotated[
    Optional[Path],
    typer.Option(
        "--output",
        help="Output .pptx path (default: <org>_<review-quarter>_QBR.pptx in the current directory).",
    ),
]

LeaderOption = Annotated[
    Optional[str],
    typer.Option("--leader", help="Override the leader name from qbr-config.yaml."),
]

OrgOption = Annotated[
    Optional[str],
    typer.Option("--org", help="Override the organization/team from qbr-config.yaml."),
]

ReviewQuarterOption = Annotated[
    Optional[str],
    typer.Option("--review-quarter", help="Review quarter label, e.g. 'Q3 2026'."),
]

PreviewQuarterOption = Annotated[
    Optional[str],
    typer.Option("--preview-quarter", help="Preview quarter label, e.g. 'Q4 2026'."),
]

NoInterviewOption = Annotated[
    bool,
    typer.Option(
        "--no-interview",
        help="Skip the clarifying interview and draft directly with gaps noted.",
    ),
]

_DEFAULT_TEMPLATE = Path("skills/qbr-architect/assets/qbr-template.pptx")


@app.command()
def qbr(
    reports_dir: ReportsDirOption,
    pptx: PptxOption = None,
    output: QbrOutputOption = None,
    model: ModelOption = None,
    leader: LeaderOption = None,
    org: OrgOption = None,
    review_quarter: ReviewQuarterOption = None,
    preview_quarter: PreviewQuarterOption = None,
    no_interview: NoInterviewOption = False,
) -> None:
    """Aggregate populated quarterly reports into a leader QBR deck (qbr-architect skill)."""
    _configure_logging()
    settings = Settings()
    model_id = model or settings.llm_model

    template_path = pptx or _DEFAULT_TEMPLATE
    if not template_path.is_file():
        typer.secho(
            f"QBR template not found at {template_path}; pass --pptx with the path to "
            "the qbr-template.pptx you want to fill.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    reports = load_team_reports(reports_dir)
    if not reports:
        typer.secho(
            f"No Markdown reports found in {reports_dir}; populate quarterly reports first "
            "(quarterly-pulse populate) and point --reports-dir at them.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    try:
        config = resolve_deck_config(
            reports,
            QbrConfig.load(reports_dir),
            leader=leader,
            org=org,
            review_quarter=review_quarter,
            preview_quarter=preview_quarter,
        )
    except ValueError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.secho(
        f"QBR: {len(reports)} report(s) | leader: {config.leader or 'Leader to confirm'} | "
        f"org: {config.org or 'Team to confirm'} | review: {config.review_quarter} | "
        f"preview: {config.preview_quarter}",
        err=True,
    )

    chat_model = _init_model(model_id, settings)

    # One event loop for the whole command: the provider SDK's async client
    # binds its connection pool to the loop of the first awaited call, so
    # separate asyncio.run() calls (interview planning, then synthesis — or
    # re-planning rounds) would reuse a client bound to an already-closed
    # loop and fail with "Event loop is closed".
    loop = asyncio.new_event_loop()
    try:
        answers: list[dict[str, str]] = []
        if no_interview:
            typer.secho("Interview skipped (--no-interview); drafting with gaps noted.", err=True)
        elif _stdin_is_interactive():
            answers = _run_interview(config, reports, chat_model, loop)
        else:
            typer.secho(
                "stdin is not interactive; skipping the interview (use --no-interview to silence).",
                err=True,
                fg=typer.colors.YELLOW,
            )

        try:
            synthesis = loop.run_until_complete(
                synthesize_qbr(config, reports, answers, chat_model)
            )
        except Exception as exc:  # fail loudly: no partial output on LLM failure
            typer.secho(f"QBR synthesis with '{model_id}' failed: {exc}", err=True, fg=typer.colors.RED)
            typer.secho(
                "Check your provider API key (OPENAI_API_KEY / ANTHROPIC_API_KEY) and LLM_MODEL, then re-run.",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
    finally:
        loop.close()

    output_path = output or default_output_path(config, Path.cwd())
    outputs = render_qbr_deck(template_path, synthesis, config, source_manifest(reports), output_path)
    for warning in outputs.warnings:
        typer.secho(f"[qbr] warning: {warning}", err=True, fg=typer.colors.YELLOW)

    typer.echo(
        f"Wrote {outputs.deck} and {outputs.companion.name} "
        f"({synthesis.rows_received} source rows, {synthesis.rows_for_synthesis} synthesized, "
        f"{len(synthesis.content.gaps)} gaps recorded)."
    )


def _stdin_is_interactive() -> bool:
    """True when stdin is a terminal (the interview can prompt safely).

    Separated as a module function so tests can force the interactive path
    under typer's CliRunner, which replaces ``sys.stdin`` during invocation.
    """
    return sys.stdin.isatty()


def _run_interview(
    config: QbrConfig,
    reports: list,
    chat_model: BaseChatModel,
    loop: asyncio.AbstractEventLoop,
) -> list[dict[str, str]]:
    """Sequential clarifying interview per the SKILL.md contract (max 10).

    Every model call runs on *loop* — the same loop the later synthesis uses
    — so the provider client stays bound to a single, open event loop.
    """
    answers: list[dict[str, str]] = []
    while True:
        plan = loop.run_until_complete(plan_interview(config, reports, chat_model, answers))
        typer.secho(f"\n{plan.context_summary}", err=True)
        if not plan.questions:
            typer.secho("No clarifying questions needed — generating the draft.", err=True)
            return answers
        total = len(plan.questions)
        typer.secho(
            f"I have {total} question(s). Answer plainly, or with 'unknown' / 'skip'; "
            "say 'draft' to stop and generate now.\n",
            err=True,
        )
        stopped = False
        for index, question in enumerate(plan.questions, 1):
            answer = typer.prompt(f"Question {index} of up to {total}: {question.question}")
            if answer.strip().lower() in {"draft", "draft now", "stop"}:
                stopped = True
                break
            answers.append({"question": question.question, "answer": answer.strip()})
        if stopped or total < INTERVIEW_BUDGET:
            return answers
        continue_interview = typer.prompt(
            "Continue with more questions, or produce the draft now with the remaining "
            "gaps noted? (more/draft)"
        )
        if continue_interview.strip().lower() != "more":
            return answers


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
    cache_gathered_at: datetime | None = None,
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
        cache_gathered_at=cache_gathered_at,
    )


def _load_gather_cache(
    path: Path, start_date: date, end_date: date, profile: TemplateProfile
) -> GatherCache:
    """Load a gather cache, warning about window or profile mismatches."""
    try:
        data = GatherCache.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        typer.secho(
            f"Cache file {path} is unreadable ({exc}); re-gather with --refresh or delete it.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if (data.start_date, data.end_date) != (start_date, end_date):
        typer.secho(
            f"[cache] warning: cache was gathered for {data.start_date}..{data.end_date} but this run "
            f"asked for {start_date}..{end_date}; using the cached window (use --refresh to re-gather).",
            err=True,
            fg=typer.colors.YELLOW,
        )
    if (data.profile.github_handle, data.profile.linkedin_url) != (
        profile.github_handle,
        profile.linkedin_url,
    ):
        typer.secho(
            "[cache] warning: contributor handles differ from the cached gather "
            "(use --refresh to re-gather).",
            err=True,
            fg=typer.colors.YELLOW,
        )
    items = sum(len(result.items) for result in data.connector_results)
    typer.secho(
        f"[cache] loaded {items} items gathered at {data.gathered_at:%Y-%m-%d %H:%M}; "
        "skipping GitHub/LinkedIn/docs connectors.",
        err=True,
    )
    return data


def _write_gather_cache(
    path: Path,
    start_date: date,
    end_date: date,
    profile: TemplateProfile,
    connector_results: list[ConnectorResult],
) -> None:
    """Persist gathered items so the output can be regenerated without re-scraping."""
    data = GatherCache(
        gathered_at=datetime.now().astimezone(),
        start_date=start_date,
        end_date=end_date,
        profile=profile,
        connector_results=connector_results,
    )
    path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    typer.secho(f"[cache] wrote {path}", err=True)


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
