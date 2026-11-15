"""Unit tests for quarterly_pulse.parser."""

from __future__ import annotations

from pathlib import Path

from quarterly_pulse.models import ParsedTemplate
from quarterly_pulse.parser import (
    parse_template,
    parse_template_text,
    section_rename_placeholder,
    split_table_row,
    unescape_markdown,
)

MINIMAL_TEMPLATE = """# Activity Log

## 1. Contributor Profile

| Field | Your Input |
| :---- | :---- |
| **Name & Role** | Jane Doe, Staff Engineer |
| **Github Handle** | @jdoe |

---

## 2. Things

*Helper prompt here.*

| A | B | C |
| :---- | :---- | :---- |
| *e.g., x* | y | z |
| *Copy/paste row to add item* |  |  |
"""


class TestHelpers:
    def test_split_table_row_basic(self) -> None:
        assert split_table_row("| a | b | c |") == ["a", "b", "c"]

    def test_split_table_row_keeps_escaped_pipes_in_cells(self) -> None:
        assert split_table_row("| a \\| b | c |") == ["a \\| b", "c"]

    def test_split_table_row_without_trailing_pipe(self) -> None:
        assert split_table_row("| a | b") == ["a", "b"]

    def test_unescape_markdown(self) -> None:
        assert unescape_markdown("https\\://www\\.example\\.com/x") == "https://www.example.com/x"

    def test_unescape_markdown_leaves_plain_text_alone(self) -> None:
        assert unescape_markdown("plain text 100%") == "plain text 100%"


class TestProfileExtraction:
    def test_real_template_profile(self, template_path: Path) -> None:
        profile = parse_template(template_path).profile
        assert profile.name_role == "Vincent Caldeira, CTO APAC"
        assert profile.team_geo == "Field CTO Team APAC"
        assert profile.quarter == "Q3 2026"
        assert profile.github_handle == "caldeirav"
        assert profile.linkedin_url == "https://www.linkedin.com/in/caldeirav/"
        assert profile.okrs is None  # placeholder *e.g., ...* value is treated as unset

    def test_okr_real_value_is_kept(self, template_text: str) -> None:
        filled = template_text.replace(
            "*e.g., Enterprise AI adoption, upstream Kubernetes contributions*",
            "Enterprise AI adoption; upstream Kubernetes contributions",
        )
        parsed = parse_template_text(filled)
        assert parsed.profile.okrs == "Enterprise AI adoption; upstream Kubernetes contributions"

    def test_missing_fields_default_to_none(self) -> None:
        text = (
            "## 1. Profile\n\n"
            "| Field | Your Input |\n| :---- | :---- |\n| **Team / GEO** | Field CTO |\n"
        )
        parsed = parse_template_text(text)
        assert parsed.profile.github_handle is None
        assert parsed.profile.linkedin_url is None
        assert parsed.profile.team_geo == "Field CTO"


class TestSectionCapture:
    def test_all_content_sections_captured(self, template_path: Path) -> None:
        parsed = parse_template(template_path)
        assert [section.number for section in parsed.sections] == [2, 3, 4, 5, 6, 7, 8, 9]

    def test_section2_columns(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(2)
        assert section is not None
        assert section.columns == [
            "Project / Repo / Contribution",
            "What You Did & Why It Matters",
            "Links",
        ]

    def test_section8_columns_drop_annotations(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(8)
        assert section is not None
        assert section.columns == [
            "Type",
            "Initiative / Activity",
            "Target Outcome, Timing, or Capacity Freed",
        ]

    def test_helper_prompts_preserved_verbatim(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(2)
        assert section is not None
        assert section.helper_prompt is not None
        assert section.helper_prompt.startswith("*Include code commits, merged PRs")
        assert section.helper_prompt.endswith("open-source community leadership.*")

    def test_section6_heading_rename_placeholder_detected(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(6)
        assert section is not None
        placeholder = section_rename_placeholder(section.heading)
        assert placeholder is not None
        assert "Rename or Copy/Paste" in placeholder

    def test_section2_heading_has_no_rename_placeholder(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(2)
        assert section is not None
        assert section_rename_placeholder(section.heading) is None

    def test_body_rows_kept_verbatim(self, template_path: Path) -> None:
        parsed = parse_template(template_path)
        section = parsed.get_section(2)
        assert section is not None
        assert len(section.body_rows) == 3  # one *e.g.,* row plus two copy/paste rows
        assert any("Copy/paste row to add item" in row for row in section.body_rows)
        assert any(row.startswith("| *e.g.,") for row in section.body_rows)

    def test_section8_has_four_example_rows(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(8)
        assert section is not None
        assert len(section.body_rows) == 4

    def test_section_spans_heading_to_last_table_row(self, template_path: Path) -> None:
        parsed: ParsedTemplate = parse_template(template_path)
        section = parsed.get_section(9)
        assert section is not None
        assert parsed.lines[section.start_line].startswith("## 9")
        assert parsed.lines[section.end_line - 1].lstrip().startswith("|")
        # The line just past the section must not be another table row.
        assert section.end_line >= len(parsed.lines) or not parsed.lines[section.end_line].lstrip().startswith("|")

    def test_separator_row_captured(self, template_path: Path) -> None:
        section = parse_template(template_path).get_section(3)
        assert section is not None
        assert section.separator_row is not None
        assert set(section.separator_row.replace(" ", "")) <= {"|", ":", "-"}


class TestSyntheticTemplates:
    def test_minimal_unescaped_template(self) -> None:
        parsed = parse_template_text(MINIMAL_TEMPLATE)
        assert parsed.profile.name_role == "Jane Doe, Staff Engineer"
        assert parsed.profile.github_handle == "jdoe"
        section = parsed.get_section(2)
        assert section is not None
        assert section.columns == ["A", "B", "C"]
        assert section.helper_prompt == "*Helper prompt here.*"
        assert len(section.body_rows) == 2

    def test_template_without_content_sections(self) -> None:
        parsed = parse_template_text("# Just a title\n\nSome prose.\n")
        assert parsed.sections == []
        assert parsed.profile.name_role is None
