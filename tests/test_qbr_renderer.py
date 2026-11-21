"""Unit tests for the QBR deck renderer against the real PPTX template."""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

from quarterly_pulse.qbr import (
    QbrConfig,
    QbrSynthesisResult,
    ReportRow,
    SlotItem,
    load_team_reports,
    source_manifest,
)
from quarterly_pulse.qbr_renderer import (
    _content_shapes,
    _slot_shapes,
    default_output_path,
    render_qbr_deck,
)
from tests.test_qbr import QBR_FIXTURES, _content


def _manifest() -> dict[str, ReportRow]:
    return source_manifest(load_team_reports(QBR_FIXTURES))


def _result() -> QbrSynthesisResult:
    return QbrSynthesisResult(content=_content(), rows_received=12, rows_for_synthesis=12)


def _render(
    tmp_path: Path,
    template_path: Path,
    config: QbrConfig,
    result: QbrSynthesisResult | None = None,
) -> tuple[Path, object]:
    deck = tmp_path / "deck.pptx"
    outputs = render_qbr_deck(
        template_path, result or _result(), config, _manifest(), deck
    )
    assert outputs.warnings == []
    assert outputs.companion.is_file()
    return deck, outputs


def _rendered_slots(template_path: Path, deck: Path) -> list[list]:
    """Rendered-deck slot shapes in template slot order, matched by shape ID."""
    template = Presentation(str(template_path))
    rendered = Presentation(str(deck))
    slides: list[list] = []
    for t_slide, r_slide in zip(template.slides, rendered.slides):
        slot_ids = {shape.shape_id for shape in _slot_shapes(t_slide)}
        matched = sorted(
            (shape for shape in r_slide.shapes if shape.shape_id in slot_ids),
            key=lambda shape: (shape.left, shape.top),
        )
        slides.append(matched)
    return slides


class TestSlotDiscovery:
    def test_expected_slot_counts_per_slide(self, qbr_template_path: Path) -> None:
        prs = Presentation(str(qbr_template_path))
        expected = [6, 12, 12, 7]  # 3+3, 4+4+4, 4+4+4, 2+2+3
        for slide, count in zip(prs.slides, expected):
            assert len(_slot_shapes(slide)) == count


class TestRenderQbrDeck:
    def test_fills_slots_runs_and_preserves_styling(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(
            leader="Priya Nair",
            org="Field CTO Team APAC",
            review_quarter="Q3 2026",
            preview_quarter="Q4 2026",
        )
        deck, _ = _render(tmp_path, qbr_template_path, config)
        prs = Presentation(str(deck))
        slide1_slots = _rendered_slots(qbr_template_path, deck)[0]

        # Highlight 1: bold title run replaced, detail run follows.
        highlight = slide1_slots[0]
        runs = highlight.text_frame.paragraphs[0].runs
        assert runs[0].text == "Acme Corp renewal unblocked"
        assert runs[0].font.bold is True
        assert runs[1].text.startswith(" 3-week architecture sprint")
        assert runs[1].font.bold is False

        # Lowlight 1 (second column, first row): red Mitigation run preserved.
        lowlight = slide1_slots[3]
        low_runs = lowlight.text_frame.paragraphs[0].runs
        assert low_runs[0].text == "DSTA pact signing slipped"
        assert low_runs[2].text == " Mitigation: Escalated to legal; target signature Nov 07."
        assert low_runs[2].font.bold is True

        # Metadata fields replaced, fixed labels preserved.
        slide1 = prs.slides[0]
        all_text = "\n".join(
            shape.text_frame.text for shape in slide1.shapes if shape.has_text_frame
        )
        assert "FIELD CTO TEAM APAC · QUARTERLY BUSINESS REVIEW" in all_text
        assert "Priya Nair · Field CTO Team APAC" in all_text
        assert "Statement of impact: Two technical wins" in all_text
        assert "1 of 4 · Executive Readout" in all_text
        assert "Q3 Highlights" in all_text

    def test_empty_slots_blank_and_no_bracket_prompts_remain(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(review_quarter="Q3 2026", preview_quarter="Q4 2026")
        deck, _ = _render(tmp_path, qbr_template_path, config)
        prs = Presentation(str(deck))

        for slide in prs.slides:
            for shape in _content_shapes(slide):
                text = shape.text_frame.text
                assert not text.startswith("["), f"unfilled prompt: {text[:50]!r}"

        # Slide 2 has no items in the fixture content: all slots stay empty.
        slide2_text = "\n".join(
            shape.text_frame.text for shape in prs.slides[1].shapes if shape.has_text_frame
        )
        assert "[Program / Initiative Name 1]" not in slide2_text
        assert "PILLAR 01" in slide2_text  # fixed labels untouched

    def test_quarter_tokens_updated_with_rollover(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(
            leader="Priya Nair",
            org="Field CTO Team APAC",
            review_quarter="Q4 2026",
            preview_quarter="Q1 2027",
        )
        deck, _ = _render(tmp_path, qbr_template_path, config)
        prs = Presentation(str(deck))

        slide1_text = "\n".join(
            shape.text_frame.text for shape in prs.slides[0].shapes if shape.has_text_frame
        )
        slide3_text = "\n".join(
            shape.text_frame.text for shape in prs.slides[2].shapes if shape.has_text_frame
        )
        assert "Q4 in Review: Impact, Highlights & Lowlights" in slide1_text
        assert "Q3 in Review" not in slide1_text
        assert "LOOKING AHEAD · Q1 COMMITMENTS" in slide3_text.upper()
        assert "Q1 Focus Areas (Must-Wins)" in slide3_text

    def test_default_tokens_untouched_for_q3_q4(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(review_quarter="Q3 2026", preview_quarter="Q4 2026")
        deck, _ = _render(tmp_path, qbr_template_path, config)
        prs = Presentation(str(deck))
        slide1_text = "\n".join(
            shape.text_frame.text for shape in prs.slides[0].shapes if shape.has_text_frame
        )
        assert "Q3 in Review: Impact, Highlights & Lowlights" in slide1_text

    def test_speaker_notes_carry_locators_and_mappings(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(review_quarter="Q3 2026", preview_quarter="Q4 2026")
        deck, _ = _render(tmp_path, qbr_template_path, config)
        prs = Presentation(str(deck))
        notes = prs.slides[0].notes_slide.notes_text_frame.text
        assert "Acme Corp renewal unblocked" in notes
        # The source rows' actual content is the narrative, not just locators.
        assert "technical win unblocking the $450K renewal" in notes
        assert "Alice Tan — Acme Corp (Financial Services): Completed 3-week architecture sprint" in notes
        assert "customer-poc-readout.md" in notes
        assert "Contributor: Alice Tan" in notes

    def test_companion_record_lists_gaps_and_sources(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(
            leader="Priya Nair",
            org="Field CTO Team APAC",
            review_quarter="Q3 2026",
            preview_quarter="Q4 2026",
        )
        _, outputs = _render(tmp_path, qbr_template_path, config)
        companion = outputs.companion.read_text(encoding="utf-8")
        assert "QBR companion record — Field CTO Team APAC · Q3 2026" in companion
        assert "Slide 3 — Looking Ahead" in companion
        assert "No stop linkage supplied." in companion
        assert "[alice-populated-s4-00]" in companion
        # Source narratives (the rows' own content) back every cited slot.
        assert "technical win unblocking the $450K renewal" in companion

    def test_date_fields_filled_on_slide_4(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        config = QbrConfig(review_quarter="Q3 2026", preview_quarter="Q4 2026")
        deck, _ = _render(tmp_path, qbr_template_path, config)
        prs = Presentation(str(deck))
        slide4_text = "\n".join(
            shape.text_frame.text for shape in prs.slides[3].shapes if shape.has_text_frame
        )
        assert "Target Decision Date Oct 24" in slide4_text
        assert "Target Agreement Date Date to confirm" in slide4_text

    def test_template_modification_detection(
        self, tmp_path: Path, qbr_template_path: Path
    ) -> None:
        """A template whose slot boxes were removed fails loudly."""
        broken = tmp_path / "broken.pptx"
        prs = Presentation(str(qbr_template_path))
        slide1 = prs.slides[0]
        victim = _slot_shapes(slide1)[0]
        victim._element.getparent().remove(victim._element)
        prs.save(str(broken))

        config = QbrConfig(review_quarter="Q3 2026", preview_quarter="Q4 2026")
        import pytest

        with pytest.raises(ValueError, match="slot columns|slots"):
            render_qbr_deck(
                broken, _result(), config, _manifest(), tmp_path / "out.pptx"
            )


class TestDefaultOutputPath:
    def test_named_from_org_and_quarter(self, tmp_path: Path) -> None:
        config = QbrConfig(org="Field CTO Team APAC", review_quarter="Q3 2026")
        path = default_output_path(config, tmp_path)
        assert path == tmp_path / "out" / "field-cto-team-apac_Q3-2026_QBR.pptx"
