"""QBR deck renderer: fills the authoritative PPTX template in place.

Follows the fill contract in ``skills/qbr-architect/SKILL.md``:

* Works on a copy of the complete template; replaces the *text of existing
  runs* so bold titles, regular details, and the lowlights' red ``Mitigation:``
  runs keep their fonts, sizes, and colors. No shapes, paragraphs, or slides
  are added or removed.
* Slots are located geometrically (bracketed bold boxes at ``top >= 2in``,
  grouped into columns by left position, ordered top-to-bottom) — never by
  shape ID, which would break if the template is re-exported.
* Fixed labels keep their wording except review/preview quarter tokens
  (``Q3``/``Q4``), which are replaced consistently with placeholder-safe
  ordering so Q4→Q1 rollover cannot corrupt the text.
* Speaker notes carry source locators, objective/KR mappings, stop linkages,
  and contributor attribution; the companion Markdown record mirrors them
  for environments without notes.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

from quarterly_pulse.qbr import QbrConfig, QbrSynthesisResult, ReportRow, SlotItem

#: Slot boxes live at or below this vertical offset; header/metadata fields above.
_SLOT_TOP_MIN = Inches(2.0)
#: Column grouping tolerance: slot columns are > 3 inches apart.
_COLUMN_TOLERANCE = Inches(1.0)

#: slot field name, expected items per column, columns left-to-right.
_SLIDE_SLOTS: dict[int, list[tuple[str, int]]] = {
    0: [("highlights", 3), ("lowlights", 3)],
    1: [("pillar_programs", 4), ("pillar_customers", 4), ("pillar_ecosystem", 4)],
    2: [("commitments", 4), ("opportunities", 4), ("stops", 4)],
    3: [("asks", 2), ("interlocks", 2), ("radar", 3)],
}

_METADATA_FIELDS: dict[int, list[tuple[str, str]]] = {
    0: [
        ("[ORG / TEAM NAME", "org_title"),
        ("[LEADER NAME", "identity"),
        ("Statement of impact:", "statement"),
    ],
    1: [("[EXECUTION AGAINST KEY OBJECTIVES", "execution_metadata")],
    2: [("[EXECUTION GUARDRAILS", "guardrails_metadata")],
    3: [
        ("[ACTION & ALIGNMENT REGISTER", "action_metadata"),
        ("Target Decision Date", "decision_date"),
        ("Target Agreement Date", "agreement_date"),
    ],
}


@dataclass
class QbrDeckOutputs:
    """Paths written by a successful render."""

    deck: Path
    companion: Path
    warnings: list[str]


def render_qbr_deck(
    template_path: Path,
    result: QbrSynthesisResult,
    config: QbrConfig,
    manifest: dict[str, ReportRow],
    output_path: Path,
) -> QbrDeckOutputs:
    """Copy the template, fill every slot in place, write notes and save."""
    content = result.content
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, output_path)
    prs = Presentation(str(output_path))
    if len(prs.slides) != 4:
        raise ValueError(f"template must have exactly 4 slides, found {len(prs.slides)}")

    _apply_quarter_tokens(prs, config)

    for slide_index, slide in enumerate(prs.slides):
        _fill_metadata_fields(slide, slide_index, content, config)
        _fill_slots(slide, slide_index, content)
        _write_notes(slide, slide_index, content, manifest)

    prs.save(str(output_path))
    warnings = _validate_deck(output_path) + list(result.grounding_warnings)
    companion = render_qbr_companion(result, config, manifest, _companion_path(output_path))
    return QbrDeckOutputs(deck=output_path, companion=companion, warnings=warnings)


def default_output_path(config: QbrConfig, cwd: Path) -> Path:
    """``<org-slug>_<review-quarter>_QBR.pptx`` named from confirmed metadata."""
    org = _slug(config.org or "team")
    quarter = _slug_preserving_case(config.review_quarter or "quarter")
    return cwd / f"{org}_{quarter}_QBR.pptx"


def _companion_path(deck_path: Path) -> Path:
    return deck_path.with_name(deck_path.stem + "_sources.md")


# ---------------------------------------------------------------------------
# Slot discovery and filling
# ---------------------------------------------------------------------------


def _content_shapes(slide) -> list:
    """Every text box in the content zone (top >= 2in), ordered left-then-top.

    Includes the slide 4 date-label boxes; use :func:`_slot_shapes` for the
    bracketed slot boxes of an unfilled template.
    """
    shapes = [
        shape
        for shape in slide.shapes
        if shape.has_text_frame and shape.top is not None and shape.top >= _SLOT_TOP_MIN
    ]
    return sorted(shapes, key=lambda shape: (shape.left, shape.top))


def _slot_shapes(slide) -> list:
    """Bracketed bold boxes in the content zone of an unfilled template."""
    return [shape for shape in _content_shapes(slide) if _first_run_text(shape).startswith("[")]


def _first_run_text(shape) -> str:
    paragraphs = shape.text_frame.paragraphs
    if not paragraphs or not paragraphs[0].runs:
        return ""
    return paragraphs[0].runs[0].text or ""


def _fill_slots(slide, slide_index: int, content) -> None:
    """Assign slot shapes to schema lists column-by-column, top-to-bottom."""
    expected = _SLIDE_SLOTS[slide_index]
    columns = _group_columns(_slot_shapes(slide))
    if len(columns) != len(expected):
        raise ValueError(
            f"slide {slide_index + 1}: found {len(columns)} slot columns, "
            f"expected {len(expected)} — template layout changed?"
        )
    for (field, capacity), column in zip(expected, columns):
        if len(column) != capacity:
            raise ValueError(
                f"slide {slide_index + 1} column {field}: found {len(column)} slots, "
                f"expected {capacity} — template layout changed?"
            )
        items = getattr(content, field)[:capacity]
        for shape, item in zip(column, items + [None] * capacity):
            _fill_slot_shape(shape, item)


def _fill_slot_shape(shape, item: SlotItem | None) -> None:
    """Replace run text in place; blank the slot when no item is provided."""
    runs = shape.text_frame.paragraphs[0].runs
    if not runs:
        return
    if item is None:
        for run in runs:
            run.text = ""
        return
    if len(runs) == 1:
        runs[0].text = f"{item.title} — {item.detail}".strip()
        return
    runs[0].text = item.title
    runs[1].text = f" {item.detail}"
    for run in runs[2:]:
        run.text = f" Mitigation: {item.mitigation}" if item.mitigation else ""


def _group_columns(shapes: list) -> list[list]:
    """Group slot shapes into columns by left position (tolerance 1 inch)."""
    columns: list[list] = []
    for shape in shapes:
        if columns and abs(shape.left - columns[-1][0].left) <= _COLUMN_TOLERANCE:
            columns[-1].append(shape)
        else:
            columns.append([shape])
    return columns


# ---------------------------------------------------------------------------
# Metadata fields, statement, and quarter tokens
# ---------------------------------------------------------------------------


def _fill_metadata_fields(slide, slide_index: int, content, config: QbrConfig) -> None:
    values = _metadata_values(content, config)
    for prefix, key in _METADATA_FIELDS[slide_index]:
        shape = _find_shape_by_prefix(slide, prefix)
        if shape is None:
            continue
        runs = shape.text_frame.paragraphs[0].runs
        if not runs:
            continue
        if key in {"decision_date", "agreement_date"} and len(runs) > 1:
            runs[1].text = f" {values[key]}"
        else:
            runs[0].text = values[key]
            for run in runs[1:]:
                run.text = ""


def _metadata_values(content, config: QbrConfig) -> dict[str, str]:
    org = config.org or "Team to confirm"
    return {
        "org_title": f"{org} · QUARTERLY BUSINESS REVIEW".upper(),
        "identity": f"{config.leader or 'Leader to confirm'} · {org}",
        "statement": f"Statement of impact: {content.statement_of_impact}".strip(),
        "execution_metadata": content.execution_metadata,
        "guardrails_metadata": content.guardrails_metadata,
        "action_metadata": content.action_metadata,
        "decision_date": content.decision_date,
        "agreement_date": content.agreement_date,
    }


def _find_shape_by_prefix(slide, prefix: str):
    for shape in slide.shapes:
        if shape.has_text_frame and _first_run_text(shape).startswith(prefix):
            return shape
    return None


def _apply_quarter_tokens(prs: Presentation, config: QbrConfig) -> None:
    """Replace Q3/Q4 tokens in the fixed labels when the period differs.

    Placeholder-safe ordering: substituting Q3->Q4 before Q4->Q1 directly
    would corrupt the text, so both tokens route through sentinels.
    """
    review = _quarter_token(config.review_quarter, "Q3")
    preview = _quarter_token(config.preview_quarter, "Q4")
    if review == "Q3" and preview == "Q4":
        return
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if "Q3" in run.text or "Q4" in run.text:
                        run.text = (
                            run.text.replace("Q3", "\x00R\x00")
                            .replace("Q4", "\x00P\x00")
                            .replace("\x00R\x00", review)
                            .replace("\x00P\x00", preview)
                        )


def _quarter_token(label: str | None, default: str) -> str:
    if not label:
        return default
    token = label.split()[0].upper()
    return token if re.match(r"^Q[1-4]$", token) else default


# ---------------------------------------------------------------------------
# Speaker notes
# ---------------------------------------------------------------------------


def _write_notes(slide, slide_index: int, content, manifest: dict[str, ReportRow]) -> None:
    fields = _SLIDE_SLOTS[slide_index]
    lines: list[str] = []
    for field, _capacity in fields:
        for slot in getattr(content, field):
            if not slot.title:
                continue
            lines.append(f"• {slot.title}")
            if slot.contributor:
                lines.append(f"  Contributor: {slot.contributor}")
            if slot.objective_kr:
                lines.append(f"  Objective/KR: {slot.objective_kr}")
            if slot.stop_link:
                lines.append(f"  Stop linkage: {slot.stop_link}")
            for locator in _locators(slot, manifest):
                lines.append(f"  Source: {locator}")
    if lines:
        slide.notes_slide.notes_text_frame.text = "\n".join(lines)


def _locators(slot: SlotItem, manifest: dict[str, ReportRow]) -> list[str]:
    locators: list[str] = []
    for row_id in slot.source_row_ids:
        row = manifest.get(row_id)
        if row is None:
            continue
        locator = row.links or f"{row.contributor}: {row.title}"
        locators.append(f"[{row.row_id}] {locator}")
    return locators


# ---------------------------------------------------------------------------
# Companion record
# ---------------------------------------------------------------------------


def render_qbr_companion(
    result: QbrSynthesisResult,
    config: QbrConfig,
    manifest: dict[str, ReportRow],
    path: Path,
) -> Path:
    """Write the concise companion source/gap record next to the deck."""
    content = result.content
    lines = [
        f"# QBR companion record — {config.org or 'Team to confirm'}"
        f" · {config.review_quarter or 'quarter to confirm'}",
        "",
        f"Generated: {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}",
        f"Rows received: {result.rows_received} / used for synthesis:"
        f" {result.rows_for_synthesis} (chunked pre-summarization:"
        f" {'yes' if result.chunked else 'no'})",
        "",
        "## Deck metadata",
        "",
        f"- Leader: {config.leader or 'Leader to confirm'}",
        f"- Org / team: {config.org or 'Team to confirm'}",
        f"- Review quarter: {config.review_quarter or 'to confirm'}",
        f"- Preview quarter: {config.preview_quarter or 'to confirm'}",
        "",
    ]
    slide_names = {
        0: "Slide 1 — Executive Readout",
        1: "Slide 2 — Execution Deep-Dive",
        2: "Slide 3 — Looking Ahead",
        3: "Slide 4 — Executive Escalations & Radar",
    }
    for slide_index, fields in _SLIDE_SLOTS.items():
        lines += [f"## {slide_names[slide_index]}", ""]
        for field, _capacity in fields:
            for slot in getattr(content, field):
                if not slot.title:
                    continue
                lines.append(f"- **{slot.title}** — {slot.detail}")
                if slot.mitigation:
                    lines.append(f"  - Mitigation: {slot.mitigation}")
                if slot.contributor:
                    lines.append(f"  - Contributor: {slot.contributor}")
                if slot.objective_kr:
                    lines.append(f"  - Objective/KR: {slot.objective_kr}")
                if slot.stop_link:
                    lines.append(f"  - Stop linkage: {slot.stop_link}")
                for locator in _locators(slot, manifest):
                    lines.append(f"  - Source: {locator}")
        lines.append("")

    lines += ["## Unresolved gaps", ""]
    if content.gaps:
        for gap in content.gaps:
            lines.append(f"- Slide {gap.slide} · {gap.slot}: {gap.description}")
    else:
        lines.append("- None recorded.")
    lines.append("")

    if result.grounding_warnings:
        lines += ["## Grounding warnings", ""]
        lines.extend(f"- {warning}" for warning in result.grounding_warnings)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_deck(path: Path) -> list[str]:
    """Post-save checks: slide count and no leftover bracket prompts."""
    prs = Presentation(str(path))
    warnings: list[str] = []
    if len(prs.slides) != 4:
        warnings.append(f"deck has {len(prs.slides)} slides, expected 4")
    for slide_index, slide in enumerate(prs.slides):
        for shape in _content_shapes(slide):
            text = _first_run_text(shape)
            if text.startswith("["):
                warnings.append(
                    f"slide {slide_index + 1}: unfilled bracket prompt remains: {text[:60]!r}"
                )
    return warnings


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "team"


def _slug_preserving_case(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return slug or "quarter"
