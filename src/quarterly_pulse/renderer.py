"""Markdown renderer: populates template tables with generated report rows.

Guarantees:

* Sections 2-9 keep their exact headings, helper prompts, and column schemas;
  only table body rows are replaced.
* Populated sections replace all italicized example rows (``*e.g., ...*``) and
  ``*Copy/paste row to add item*`` placeholders with generated rows (any
  user-prefilled rows are preserved).
* Empty sections drop the example rows but keep the copy/paste placeholder
  rows so the report can still be completed by hand.
* Every cell is pipe-escaped so tables render cleanly.
* A collapsible ``<details>`` audit footer lists sources queried, item counts
  per source, the model used, and any fallback warnings.
"""

from __future__ import annotations

from quarterly_pulse.models import AuditReport, ParsedTemplate, SectionSpec
from quarterly_pulse.parser import RENAME_PLACEHOLDER_RE
from quarterly_pulse.synthesizer import QuarterReport

#: QuarterReport field holding each section's rows.
SECTION_FIELDS: dict[int, str] = {
    2: "section_2_upstream",
    3: "section_3_assets",
    4: "section_4_customers",
    5: "section_5_engagements",
    6: "section_6_custom",
    7: "section_7_challenges",
    8: "section_8_looking_ahead",
    9: "section_9_help_needed",
}


def render_report(parsed: ParsedTemplate, report: QuarterReport, audit: AuditReport) -> str:
    """Render the populated report, preserving the template structure verbatim."""
    lines = list(parsed.lines)
    # Replace sections bottom-up so earlier line spans stay valid.
    for section in sorted(parsed.sections, key=lambda spec: spec.start_line, reverse=True):
        rows = _rows_for_section(report, section.number)
        body = _render_section_body(section, rows)
        replacement = [_section_heading(section, report), *section.preamble, section.header_row]
        if section.separator_row is not None:
            replacement.append(section.separator_row)
        replacement.extend(body)
        lines[section.start_line : section.end_line] = replacement
    return "\n".join(lines) + render_audit_footer(audit)


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------


def escape_cell(value: str) -> str:
    """Collapse whitespace/newlines and escape pipes so a table cell renders cleanly."""
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|")


def _rows_for_section(report: QuarterReport, number: int) -> list[list[str]]:
    """Extract a section's rows as lists of cell strings, in column order."""
    field = SECTION_FIELDS.get(number)
    if field is None:
        return []
    rows: list[list[str]] = []
    for row in getattr(report, field):
        cells = [str(getattr(row, name) or "") for name in type(row).model_fields]
        cells += [""] * (3 - len(cells))
        rows.append(cells[:3])
    return rows


def _render_section_body(section: SectionSpec, rows: list[list[str]]) -> list[str]:
    """Combine generated rows with the section's template body rows."""
    user_rows = [row for row in section.body_rows if not _is_placeholder_row(row)]
    generated = [_render_row(cells) for cells in rows]

    if generated:
        return generated + user_rows

    # Empty section: keep user rows and copy/paste placeholders, drop examples.
    retained = user_rows + [row for row in section.body_rows if _is_copy_paste_row(row)]
    return retained or [_render_row(["", "", ""])]


def _render_row(cells: list[str]) -> str:
    """Render one Markdown table row with escaped cells."""
    return "| " + " | ".join(escape_cell(cell) for cell in cells) + " |"


def _is_placeholder_row(row: str) -> bool:
    """True for template example rows and copy/paste placeholder rows."""
    return "*e.g.," in row or "Copy/paste row to add" in row or "*Enter activity name*" in row


def _is_copy_paste_row(row: str) -> bool:
    """True for the reusable ``*Copy/paste row to add ...*`` placeholder rows."""
    return "Copy/paste row to add" in row


def _section_heading(section: SectionSpec, report: QuarterReport) -> str:
    """Return the section heading, renaming Section 6 when it has content."""
    if section.number == 6 and report.section_6_custom and report.section_6_title:
        return _rename_custom_heading(section.heading, report.section_6_title)
    return section.heading


def _rename_custom_heading(heading: str, new_title: str) -> str:
    """Replace the Section 6 rename placeholder with the derived title."""
    if not RENAME_PLACEHOLDER_RE.search(heading):
        return heading
    base = RENAME_PLACEHOLDER_RE.sub("", heading).rstrip().rstrip(":")
    clean_title = " ".join(new_title.replace("|", " ").split())
    return f"{base}: {clean_title}"


# ---------------------------------------------------------------------------
# Audit footer
# ---------------------------------------------------------------------------


def render_audit_footer(audit: AuditReport) -> str:
    """Render the collapsible audit footer appended to the report."""
    lines = [
        "",
        "---",
        "",
        "<details>",
        "<summary><strong>quarterly-pulse audit footer</strong> — sources queried, item counts, fallback warnings</summary>",
        "",
        f"- Generated at: {audit.generated_at.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- Model: {audit.model or 'n/a'}",
        f"- Activity items: {audit.total_items} gathered / {audit.items_after_dedup} after deduplication / "
        f"{audit.items_for_synthesis} used for synthesis (chunked pre-summarization: "
        f"{'yes' if audit.chunked else 'no'})",
        "",
        "| Source | Strategy used | Items | Warnings |",
        "| :---- | :---- | ----: | :---- |",
    ]
    for source in audit.sources:
        warnings = " / ".join(source.warnings) if source.warnings else "none"
        lines.append(
            f"| {escape_cell(source.source)} | {escape_cell(source.strategy)} "
            f"| {source.item_count} | {escape_cell(warnings)} |"
        )

    if audit.section_row_counts:
        joined = ", ".join(
            f"Section {number}: {count}"
            for number, count in sorted(audit.section_row_counts.items())
        )
        lines.append("")
        lines.append(f"- Report rows per section: {joined}")

    if audit.cache_gathered_at is not None:
        lines.append("")
        lines.append(
            f"- Sources loaded from cache gathered at "
            f"{audit.cache_gathered_at.strftime('%Y-%m-%d %H:%M %Z')} (connectors skipped)."
        )

    if audit.backfilled_count:
        lines.append("")
        lines.append(
            f"- Completeness backstop: {audit.backfilled_count} item(s) the model left out were "
            "auto-added to Section 6 (marked 'auto-added'); re-place or edit them manually."
        )

    if audit.unplaced_items:
        lines.append("")
        lines.append(
            f"- Not placed as separate rows ({len(audit.unplaced_items)} item(s) judged already "
            "covered by existing rows — verify):"
        )
        lines.extend(f"  - {escape_cell(title)}" for title in audit.unplaced_items)

    if audit.empty_sections:
        joined = ", ".join(f"Section {number}" for number in audit.empty_sections)
        lines.append("")
        lines.append(f"- Sections left with placeholder rows (no grounded data): {joined}")

    if audit.grounding_violations:
        lines.append("")
        lines.append("- Grounding guardrail: removed URLs not present in retrieved sources:")
        lines.extend(f"  - {escape_cell(violation)}" for violation in audit.grounding_violations)

    lines.extend(["", "</details>", ""])
    return "\n".join(lines)
