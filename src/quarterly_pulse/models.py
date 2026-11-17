"""Shared Pydantic schemas for quarterly-pulse."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field


class ActivityItem(BaseModel):
    """Normalized activity record produced by every connector."""

    source: str = Field(
        description="Connector that produced the item: 'github', 'linkedin', or 'docs'."
    )
    date: datetime.date | None = Field(default=None, description="Activity date, if known.")
    title: str = Field(description="Short human-readable title.")
    summary: str = Field(default="", description="One-to-three sentence description.")
    impact: str = Field(default="", description="Measured or stated outcome / why it matters.")
    url: str | None = Field(default=None, description="Canonical URL for the item, if any.")
    raw_context: str = Field(
        default="", description="Compact raw context kept for LLM grounding and auditing."
    )


class ConnectorResult(BaseModel):
    """Outcome of a single connector run."""

    source: str = Field(description="Connector name: 'github', 'linkedin', or 'docs'.")
    strategy: str = Field(description="Strategy/backoff path that produced (or failed to produce) items.")
    items: list[ActivityItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TemplateProfile(BaseModel):
    """Section 1 (Contributor Profile & Quarter Context) extracted from the template."""

    name_role: str | None = None
    team_geo: str | None = None
    quarter: str | None = None
    github_handle: str | None = None
    linkedin_url: str | None = None
    okrs: str | None = None


class SectionSpec(BaseModel):
    """A content section (2-9) captured verbatim from the template."""

    number: int
    heading: str = Field(description="Verbatim heading line, e.g. '## 6\\\\. Custom Workstream: ...'.")
    title: str = Field(description="Unescaped heading title text.")
    helper_prompt: str | None = Field(default=None, description="Verbatim italic helper prompt line.")
    preamble: list[str] = Field(
        default_factory=list,
        description="Verbatim lines between the heading and the table (blank lines, helper prompt).",
    )
    columns: list[str] = Field(default_factory=list, description="Cleaned column header labels.")
    header_row: str = Field(description="Verbatim table header row.")
    separator_row: str | None = Field(default=None, description="Verbatim table separator row, if present.")
    body_rows: list[str] = Field(
        default_factory=list, description="Verbatim template body rows (examples and copy/paste placeholders)."
    )
    start_line: int = Field(description="0-based line index of the heading.")
    end_line: int = Field(description="0-based exclusive index one past the last table row.")


class ParsedTemplate(BaseModel):
    """Full parse result: Section 1 profile plus Sections 2-9 specs and raw lines."""

    profile: TemplateProfile
    sections: list[SectionSpec]
    lines: list[str]
    raw_text: str

    def get_section(self, number: int) -> SectionSpec | None:
        """Return the section spec with the given number, if present."""
        for section in self.sections:
            if section.number == number:
                return section
        return None


class GatherCache(BaseModel):
    """Gathered activity cached between runs so output can be regenerated
    without re-scraping GitHub, LinkedIn, or the local docs directory."""

    version: int = 1
    gathered_at: datetime.datetime
    start_date: datetime.date
    end_date: datetime.date
    profile: TemplateProfile
    connector_results: list[ConnectorResult] = Field(default_factory=list)


class SourceAudit(BaseModel):
    """Per-source audit entry for the output footer."""

    source: str
    strategy: str
    item_count: int
    warnings: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    """Aggregated audit information rendered as the collapsible output footer."""

    sources: list[SourceAudit] = Field(default_factory=list)
    model: str | None = None
    total_items: int = 0
    items_after_dedup: int = 0
    items_for_synthesis: int = 0
    chunked: bool = False
    backfilled_count: int = 0
    section_row_counts: dict[int, int] = Field(default_factory=dict)
    empty_sections: list[int] = Field(default_factory=list)
    grounding_violations: list[str] = Field(default_factory=list)
    cache_gathered_at: datetime.datetime | None = None
    generated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now().astimezone())
