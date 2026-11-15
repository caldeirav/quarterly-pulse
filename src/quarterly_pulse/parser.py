"""Markdown template parser.

Extracts Section 1 (*Contributor Profile & Quarter Context*) into a
:class:`~quarterly_pulse.models.TemplateProfile` and captures Sections 2-9 as
:class:`~quarterly_pulse.models.SectionSpec` objects that preserve the exact
section headings, helper prompts, and Markdown table column schemas so the
renderer can repopulate them without altering the template structure.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

from quarterly_pulse.models import ParsedTemplate, SectionSpec, TemplateProfile

#: Matches ``## N\. Title`` headings, tolerating the escaped dot (``## 2\.``).
SECTION_HEADING_RE = re.compile(r"^##\s+(\d)\\?\.\s+(.+?)\s*$")
#: Matches a table separator row such as ``| :---- | :---- |`` (after space removal).
SEPARATOR_ROW_RE = re.compile(r"^(\|:?-+:?)+\|$")
#: Matches a fully italic line (helper prompts).
FULL_ITALIC_RE = re.compile(r"^\*[^*|]+\*$")
#: Matches placeholder example values such as ``*e.g., Enterprise AI adoption...*``.
PLACEHOLDER_VALUE_RE = re.compile(r"^\*e\.g\.,", re.IGNORECASE)
#: Matches the rename placeholder in Section 6's heading (``*\<Rename or ...>*``).
RENAME_PLACEHOLDER_RE = re.compile(r"\*\\?<[^>|]+?\\?>\*")
#: Matches a bold label cell such as ``**Github Handle**``.
BOLD_LABEL_RE = re.compile(r"^\*\*(.+?)\*\*")
#: Matches the italic annotation suffix on column headers (``*(Deliverable, milestone)*``).
COLUMN_ANNOTATION_RE = re.compile(r"\s*\*\([^)]*\)\*\s*$")

_ESCAPED_PUNCT_RE = re.compile(r"\\([" + re.escape(string.punctuation) + r"])")

#: Maps normalized Section 1 field labels to :class:`TemplateProfile` attributes.
PROFILE_FIELD_MAP: dict[str, str] = {
    "name role": "name_role",
    "team geo": "team_geo",
    "reporting quarter year": "quarter",
    "github handle": "github_handle",
    "linkedin profile": "linkedin_url",
    "key goals okrs focused on": "okrs",
}


def split_table_row(line: str) -> list[str]:
    """Split a Markdown table row on unescaped ``|`` characters.

    Cells keep any escaped pipes (``\\|``) intact; callers unescape as needed.
    """
    stripped = line.strip()
    parts = re.split(r"(?<!\\)\|", stripped)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [part.strip() for part in parts]


def unescape_markdown(text: str) -> str:
    """Remove backslash escapes before ASCII punctuation (``https\\://`` -> ``https://``)."""
    return _ESCAPED_PUNCT_RE.sub(r"\1", text)


def section_rename_placeholder(heading: str) -> str | None:
    """Return the verbatim rename placeholder span in a section heading, if any."""
    match = RENAME_PLACEHOLDER_RE.search(heading)
    return match.group(0) if match else None


def parse_template(template_path: Path) -> ParsedTemplate:
    """Parse a Markdown template file from disk."""
    return parse_template_text(template_path.read_text(encoding="utf-8"))


def parse_template_text(text: str) -> ParsedTemplate:
    """Parse template text into a contributor profile plus Sections 2-9 specs."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        match = SECTION_HEADING_RE.match(line)
        if match:
            headings.append((int(match.group(1)), idx, line))

    profile = TemplateProfile()
    sections: list[SectionSpec] = []
    for pos, (number, head_idx, raw_heading) in enumerate(headings):
        next_idx = headings[pos + 1][1] if pos + 1 < len(headings) else len(lines)
        if number == 1:
            profile = _parse_profile_section(lines, head_idx, next_idx)
        else:
            sections.append(_parse_content_section(lines, head_idx, next_idx, number, raw_heading))

    return ParsedTemplate(profile=profile, sections=sections, lines=lines, raw_text=text)


def _table_row_indices(lines: list[str], start: int, end: int) -> list[int]:
    """Line indices of the first contiguous run of Markdown table rows in ``lines[start:end]``."""
    indices: list[int] = []
    for idx in range(start, end):
        if lines[idx].lstrip().startswith("|"):
            indices.append(idx)
        elif indices:
            break  # the contiguous table has ended
    return indices


def _parse_profile_section(lines: list[str], head_idx: int, next_idx: int) -> TemplateProfile:
    """Extract the Section 1 field/value table into a :class:`TemplateProfile`."""
    row_indices = _table_row_indices(lines, head_idx + 1, next_idx)
    fields: dict[str, str | None] = {}
    data_rows = row_indices[2:] if len(row_indices) > 2 else []
    for row_idx in data_rows:
        cells = split_table_row(lines[row_idx])
        if len(cells) < 2:
            continue
        label = _normalize_label(_clean_label(cells[0]))
        field = PROFILE_FIELD_MAP.get(label)
        if field is not None:
            fields[field] = _clean_value(cells[1])

    handle = fields.get("github_handle")
    if handle is not None:
        handle = handle.lstrip("@").strip() or None
    fields["github_handle"] = handle
    return TemplateProfile(**fields)


def _parse_content_section(
    lines: list[str], head_idx: int, next_idx: int, number: int, raw_heading: str
) -> SectionSpec:
    """Capture a Sections 2-9 block, preserving its structure verbatim."""
    row_indices = _table_row_indices(lines, head_idx + 1, next_idx)
    if not row_indices:
        return SectionSpec(
            number=number,
            heading=raw_heading,
            title=_heading_title(raw_heading),
            start_line=head_idx,
            end_line=head_idx + 1,
        )

    first, last = row_indices[0], row_indices[-1]
    header_row = lines[first]
    has_separator = len(row_indices) > 1 and bool(
        SEPARATOR_ROW_RE.match(lines[first + 1].replace(" ", ""))
    )
    separator_row = lines[first + 1] if has_separator else None
    body_start = first + (2 if has_separator else 1)
    body_rows = lines[body_start : last + 1]
    preamble = lines[head_idx + 1 : first]
    helper_prompt = next(
        (line for line in preamble if FULL_ITALIC_RE.match(line.strip())), None
    )
    columns = [_clean_column(cell) for cell in split_table_row(header_row)]

    return SectionSpec(
        number=number,
        heading=raw_heading,
        title=_heading_title(raw_heading),
        helper_prompt=helper_prompt,
        preamble=preamble,
        columns=columns,
        header_row=header_row,
        separator_row=separator_row,
        body_rows=body_rows,
        start_line=head_idx,
        end_line=last + 1,
    )


def _heading_title(heading: str) -> str:
    """Return the unescaped title text of a section heading line."""
    match = SECTION_HEADING_RE.match(heading)
    return unescape_markdown(match.group(2)).strip() if match else heading


def _clean_label(cell: str) -> str:
    """Extract the bold label text from a Section 1 field cell."""
    match = BOLD_LABEL_RE.match(cell.strip())
    return match.group(1).strip() if match else cell.strip()


def _normalize_label(label: str) -> str:
    """Normalize a field label for tolerant matching against :data:`PROFILE_FIELD_MAP`."""
    lowered = re.sub(r"[&*/]", " ", label.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _clean_value(cell: str) -> str | None:
    """Clean a Section 1 value cell; placeholder examples and blanks become ``None``."""
    value = unescape_markdown(cell.strip())
    if not value or PLACEHOLDER_VALUE_RE.match(value):
        return None
    return value


def _clean_column(cell: str) -> str:
    """Clean a table column header by unescaping and dropping italic annotations."""
    cleaned = COLUMN_ANNOTATION_RE.sub("", unescape_markdown(cell.strip())).strip()
    return cleaned or cell.strip()
