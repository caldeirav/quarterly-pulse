"""Typer CLI entrypoint for quarterly-pulse.

Kept lean per repository conventions: argument parsing and orchestration only.
The actual pipeline lives in :mod:`quarterly_pulse.parser`,
:mod:`quarterly_pulse.connectors`, :mod:`quarterly_pulse.synthesizer`, and
:mod:`quarterly_pulse.renderer`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import typer

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
    _ = (template, docs_dir, start_date, end_date, github, linkedin, model, output)
    typer.secho(
        "Pipeline not implemented yet (Phase 0 scaffold); re-run after Phase 4.",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
