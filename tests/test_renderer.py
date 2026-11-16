"""Unit tests for the Markdown renderer."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from quarterly_pulse.models import AuditReport, SourceAudit
from quarterly_pulse.parser import parse_template, parse_template_text
from quarterly_pulse.renderer import escape_cell, render_audit_footer, render_report
from quarterly_pulse.synthesizer import (
    CustomWorkstreamRow,
    CustomerFieldWorkRow,
    LookingAheadRow,
    QuarterReport,
    UpstreamContributionRow,
)


def _populated_report() -> QuarterReport:
    return QuarterReport(
        section_2_upstream=[
            UpstreamContributionRow(
                project_repo_contribution="upstream-project/fleet-operator",
                what_you_did_and_why="Merged PR adding GPU scheduling; adopted in v1.8.",
                links="https://github.com/upstream-project/fleet-operator/pull/42",
            )
        ],
        section_4_customers=[
            CustomerFieldWorkRow(
                customer_segment_opportunity="Acme Corp (Financial Services)",
                activity_and_outcome="3-week architecture sprint | technical win",
                links="customer-poc-readout.md",
            )
        ],
        section_6_custom=[
            CustomWorkstreamRow(
                initiative_activity_name="Vector DB benchmark spike",
                what_you_did_outcome="Benchmarks delivered to the platform team.",
                links="",
            )
        ],
        section_6_title="Research & Internal Tooling",
        section_8_looking_ahead=[
            LookingAheadRow(
                type="Must-Win Priority",
                initiative_activity="Ship v2.0 deployment template",
                target_outcome_timing_or_capacity="Target GA by mid-Q4.",
            )
        ],
    )


def _audit() -> AuditReport:
    return AuditReport(
        sources=[
            SourceAudit(source="github", strategy="graphql_api + search_api", item_count=9),
            SourceAudit(
                source="linkedin",
                strategy="local_export_archive",
                item_count=3,
                warnings=["LinkedIn Member Data Portability API skipped: LINKEDIN_ACCESS_TOKEN is not set."],
            ),
            SourceAudit(source="docs", strategy="local_documents", item_count=7),
        ],
        model="openai:gpt-4o",
        total_items=19,
        items_after_dedup=18,
        items_for_synthesis=17,
        chunked=False,
        empty_sections=[3, 5, 7, 9],
        grounding_violations=[
            "UpstreamContributionRow.links: removed ungrounded URL https://example.com/fake"
        ],
        generated_at=datetime(2026, 9, 30, 17, 0),
    )


def _section_block(text: str, number: int) -> str:
    """Slice the rendered text for one numbered section (headings kept escaped)."""
    start = text.index(f"## {number}\\.")
    match = re.search(r"^## \d\\\.", text[start + 3 :], re.MULTILINE)
    end = start + 3 + match.start() if match else len(text)
    return text[start:end]


class TestEscapeCell:
    def test_escapes_pipes(self) -> None:
        assert escape_cell("a | b") == "a \\| b"

    def test_collapses_newlines_and_whitespace(self) -> None:
        assert escape_cell("a\nb  c") == "a b c"

    def test_none_becomes_empty(self) -> None:
        assert escape_cell("") == ""


class TestRenderReport:
    def test_populated_sections_replace_examples_and_placeholders(
        self, template_path: Path
    ) -> None:
        text = render_report(parse_template(template_path), _populated_report(), _audit())

        # Section 1 keeps its own *e.g.,* placeholder (user input area).
        tail = text[text.index("## 2") :]
        assert "*e.g.," not in tail
        assert "| upstream-project/fleet-operator |" in text
        assert "Acme Corp (Financial Services)" in text

        section2 = _section_block(text, 2)
        assert "Copy/paste row to add item" not in section2
        assert "upstream-project/fleet-operator" in section2

    def test_empty_sections_keep_copy_paste_rows(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), _populated_report(), _audit())

        # Section 3 is empty in this report: keep copy/paste rows, drop examples.
        section3 = _section_block(text, 3)
        assert "Copy/paste row to add item" in section3
        assert "*e.g., Multi-Cluster RAG Reference Arch*" not in section3

        # Section 8 is populated in this report: placeholder fully replaced.
        section8 = _section_block(text, 8)
        assert "Copy/paste row to add priority" not in section8
        assert "*e.g., Ship v2.0 Automated Deployment Template*" not in section8

    def test_fully_empty_report_keeps_all_copy_paste_rows(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), QuarterReport(), _audit())

        for number in range(2, 10):
            block = _section_block(text, number)
            if number == 6:
                assert "Copy/paste row to add item" in block
            elif number == 8:
                assert "Copy/paste row to add priority" in block
            elif number == 9:
                assert "Copy/paste row to add ask" in block
            else:
                assert "Copy/paste row to add item" in block
            assert "*e.g.," not in block

    def test_section6_heading_renamed_when_populated(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), _populated_report(), _audit())
        assert "## 6\\. Custom Workstream: Research & Internal Tooling" in text
        assert "Rename or Copy/Paste" not in text
        assert "Vector DB benchmark spike" in _section_block(text, 6)

    def test_section6_heading_untouched_when_empty(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), QuarterReport(), _audit())
        assert "Rename or Copy/Paste" in text

    def test_pipe_characters_escaped_in_cells(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), _populated_report(), _audit())
        assert "3-week architecture sprint \\| technical win" in text

    def test_section8_labels_rendered_in_first_column(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), _populated_report(), _audit())
        assert "| Must-Win Priority | Ship v2.0 deployment template | Target GA by mid-Q4. |" in text

    def test_headings_and_helper_prompts_preserved(self, template_path: Path) -> None:
        parsed = parse_template(template_path)
        text = render_report(parsed, _populated_report(), _audit())
        for section in parsed.sections:
            if section.number == 6:
                continue  # section 6 heading is renamed when populated (tested separately)
            assert section.heading in text
            if section.helper_prompt is not None:
                assert section.helper_prompt in text

    def test_section1_profile_untouched(self, template_path: Path) -> None:
        text = render_report(parse_template(template_path), _populated_report(), _audit())
        assert "Vincent Caldeira, CTO APAC" in text
        assert "@caldeirav" in text

    def test_user_prefilled_rows_survive(self, template_path: Path, template_text: str) -> None:
        marker = "| *e.g., upstream-project / feature-x*"
        index = template_text.index(marker)
        line_end = template_text.index("\n", index)
        user_row = "| My manual contribution | Done before running the tool | https://github.com/org/repo |"
        filled = template_text[:line_end] + "\n" + user_row + template_text[line_end:]

        text = render_report(parse_template_text(filled), _populated_report(), _audit())

        section2 = _section_block(text, 2)
        assert "My manual contribution" in section2
        assert "upstream-project/fleet-operator" in section2
        assert "Copy/paste row to add item" not in section2


class TestAuditFooter:
    def test_footer_lists_sources_counts_and_warnings(self) -> None:
        footer = render_audit_footer(_audit())
        assert "<details>" in footer
        assert "</details>" in footer
        assert "graphql_api + search_api" in footer
        assert "local_documents" in footer
        assert "LINKEDIN_ACCESS_TOKEN is not set" in footer
        assert "openai:gpt-4o" in footer
        assert "19 gathered / 18 after deduplication / 17 used for synthesis" in footer
        assert "chunked pre-summarization: no" in footer

    def test_footer_lists_empty_sections_and_violations(self) -> None:
        footer = render_audit_footer(_audit())
        assert "Section 3" in footer
        assert "Section 9" in footer
        assert "example.com/fake" in footer

    def test_footer_without_optional_data(self) -> None:
        footer = render_audit_footer(AuditReport(sources=[], model=None))
        assert "n/a" in footer
        assert "Grounding guardrail" not in footer
        assert "Sections left with placeholder rows" not in footer
