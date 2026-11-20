"""Unit tests for the QBR Architect core (offline, scripted structured outputs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterly_pulse.models import TemplateProfile
from quarterly_pulse.qbr import (
    INTERVIEW_BUDGET,
    DistilledRow,
    DistilledRows,
    GapRecord,
    InterviewPlan,
    InterviewQuestion,
    QbrConfig,
    QbrContent,
    ReportRow,
    SlotItem,
    load_team_reports,
    next_quarter,
    plan_interview,
    resolve_deck_config,
    source_manifest,
    synthesize_qbr,
)
from tests.fakes import StructuredFakeChatModel

QBR_FIXTURES = Path(__file__).parent / "fixtures" / "qbr"


def _reports() -> list:
    return load_team_reports(QBR_FIXTURES)


def _config() -> QbrConfig:
    return QbrConfig.load(QBR_FIXTURES)


def _row(row_id: str, section: int = 5, contributor: str = "Alice Tan") -> ReportRow:
    return ReportRow(
        row_id=row_id,
        contributor=contributor,
        section=section,
        section_title="Engagements",
        title="KubeCon Japan talk",
        detail="Co-presented to 300+ attendees.",
        links="https://example.com/talk",
    )


def _content(**overrides) -> QbrContent:
    values: dict = {
        "org_team": "Field CTO Team APAC",
        "leader_identity": "Priya Nair · Field CTO Team APAC",
        "highlights": [
            SlotItem(
                title="Acme Corp renewal unblocked",
                detail="3-week architecture sprint closed a technical win on the $450K renewal.",
                source_row_ids=["alice-populated-s4-00"],
                contributor="Alice Tan",
            )
        ],
        "lowlights": [
            SlotItem(
                title="DSTA pact signing slipped",
                detail="Procurement friction pushed the signature three weeks.",
                mitigation="Escalated to legal; target signature Nov 07.",
                source_row_ids=["bob-populated-s7-00"],
            )
        ],
        "statement_of_impact": "Two technical wins and one slipped signature.",
        "execution_metadata": "Aligned to the enterprise AI adoption KR.",
        "pillar_programs": [],
        "pillar_customers": [],
        "pillar_ecosystem": [],
        "guardrails_metadata": "Stop-list funded must-wins.",
        "commitments": [],
        "opportunities": [],
        "stops": [],
        "action_metadata": "Asks carry owners and dates.",
        "asks": [],
        "decision_date": "Oct 24",
        "interlocks": [],
        "agreement_date": "Date to confirm",
        "radar": [],
        "gaps": [GapRecord(slide=3, slot="commitments", description="No stop linkage supplied.")],
    }
    values.update(overrides)
    return QbrContent(**values)


class TestQbrConfig:
    def test_loads_yaml_beside_reports(self) -> None:
        config = _config()
        assert config.leader == "Priya Nair"
        assert config.org == "Field CTO Team APAC"
        assert config.review_quarter == "Q3 2026"
        assert config.preview_quarter == "Q4 2026"

    def test_missing_file_yields_defaults(self, tmp_path: Path) -> None:
        assert QbrConfig.load(tmp_path).leader is None

    def test_merged_applies_nonempty_overrides(self) -> None:
        merged = _config().merged(leader="Override Lead", org=None)
        assert merged.leader == "Override Lead"
        assert merged.org == "Field CTO Team APAC"


class TestQuarters:
    def test_next_quarter_rollover(self) -> None:
        assert next_quarter("Q3 2026") == "Q4 2026"
        assert next_quarter("Q4 2026") == "Q1 2027"
        assert next_quarter("q1 2026") == "Q2 2026"

    def test_invalid_label_rejected(self) -> None:
        with pytest.raises(ValueError):
            next_quarter("third quarter")


class TestResolveDeckConfig:
    def test_config_wins_and_preview_rolls_over(self) -> None:
        config = resolve_deck_config(
            _reports(), QbrConfig(review_quarter="Q4 2026")
        )
        assert config.review_quarter == "Q4 2026"
        assert config.preview_quarter == "Q1 2027"

    def test_review_quarter_from_report_consensus(self) -> None:
        config = resolve_deck_config(_reports(), QbrConfig())
        assert config.review_quarter == "Q3 2026"  # both fixture reports say Q3 2026
        assert config.preview_quarter == "Q4 2026"

    def test_unknown_review_quarter_is_an_error(self, tmp_path: Path) -> None:
        reports = _reports()
        for report in reports:
            report.profile = TemplateProfile(name_role=report.profile.name_role)
        with pytest.raises(ValueError, match="review quarter"):
            resolve_deck_config(reports, QbrConfig())

    def test_org_from_team_geo_consensus(self) -> None:
        config = resolve_deck_config(_reports(), QbrConfig(review_quarter="Q3 2026"))
        assert config.org == "Field CTO Team APAC"


class TestLoadTeamReports:
    def test_loads_every_markdown_report(self) -> None:
        reports = _reports()
        assert [report.path.name for report in reports] == [
            "alice-populated.md",
            "bob-populated.md",
            "carol-populated.md",
        ]
        contributors = {report.profile.name_role for report in reports}
        assert "Carol Diaz - Principal Architect" in contributors  # bold-heading variant, unescaped

    def test_contributor_names_cleaned_for_attribution(self) -> None:
        """Emphasis and role suffixes never leak into attribution."""
        reports = _reports()
        carol = next(report for report in reports if report.path.stem == "carol-populated")
        assert {row.contributor for row in carol.rows} == {"Carol Diaz"}

    def test_bold_heading_variant_report_is_aggregated(self) -> None:
        """``## **2\\. ...**`` headings, plain field labels, and table-less
        sections parse like the canonical format instead of being dropped."""
        reports = _reports()
        carol = next(report for report in reports if report.path.stem == "carol-populated")
        row_sections = {row.section for row in carol.rows}
        assert {2, 3, 4, 6, 7, 8, 9} <= row_sections
        # The table-less section 5 contributes no rows but does not crash.
        assert 5 not in row_sections

    def test_rows_parsed_with_ids_and_placeholder_rows_skipped(self) -> None:
        reports = _reports()
        alice = reports[0]
        ids = [row.row_id for row in alice.rows]
        assert "alice-populated-s2-00" in ids  # real row
        assert not any("copy" in row.title.lower() for row in alice.rows)
        assert not any(row.title.startswith("*") for row in alice.rows)
        kubecon = next(row for row in alice.rows if row.row_id.endswith("s5-00"))
        assert kubecon.title == "KubeCon Japan talk on secure agentic workflows"
        assert kubecon.detail.startswith("Co-presented")
        assert kubecon.links.endswith("activity-7234")
        assert kubecon.contributor == "Alice Tan"

    def test_manifest_covers_all_rows(self) -> None:
        manifest = source_manifest(_reports())
        assert len(manifest) == sum(len(report.rows) for report in _reports())
        assert manifest["bob-populated-s8-00"].detail.startswith("Unsponsored")
        assert manifest["bob-populated-s8-00"].columns[0].startswith("Type")
        # Markdown links in cells collapse to bare URLs; escapes are cleared.
        carol_repo = manifest["carol-populated-s2-00"]
        assert carol_repo.links == "https://github.com/variant-labs/parser-tolerances"
        assert "\\:" not in carol_repo.links and "](" not in carol_repo.links


class TestPlanInterview:
    async def test_plan_is_capped_and_prompted_with_records(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        plan = InterviewPlan(
            context_summary="Two contributors, Q3 2026, strong customer work.",
            questions=[
                InterviewQuestion(topic=f"topic {i}", question=f"Question {i}?")
                for i in range(INTERVIEW_BUDGET + 5)
            ],
        )
        fake_model.scripted = {"InterviewPlan": [plan]}

        result = await plan_interview(_config(), _reports(), fake_model)

        assert len(result.questions) == INTERVIEW_BUDGET
        assert result.context_summary.startswith("Two contributors")
        # The prompt carried the aggregated records and configuration.
        _, prompt = fake_model.inputs_seen[0]
        human = list(prompt.messages)[1].content
        assert "alice-populated-s2-00" in human
        assert "Priya Nair" in human

    async def test_zero_questions_when_context_suffices(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        fake_model.scripted = {
            "InterviewPlan": [InterviewPlan(context_summary="Sufficient.", questions=[])]
        }
        result = await plan_interview(_config(), _reports(), fake_model)
        assert result.questions == []


class TestSynthesizeQbr:
    async def test_synthesis_receives_rows_config_and_answers(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        fake_model.scripted = {"QbrContent": [_content()]}
        answers = [{"question": "Who owns the stop list?", "answer": "Priya"}]

        result = await synthesize_qbr(_config(), _reports(), answers, fake_model)

        assert result.content.statement_of_impact.startswith("Two technical wins")
        assert result.rows_received == sum(len(r.rows) for r in _reports())
        assert result.chunked is False
        assert result.grounding_warnings == []
        _, prompt = fake_model.inputs_seen[0]
        human = list(prompt.messages)[1].content
        assert "Who owns the stop list?" in human
        assert "Priya Nair" in human

    async def test_unknown_row_citations_flagged_not_fatal(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        content = _content(
            highlights=[
                SlotItem(
                    title="Fabricated grounding",
                    detail="Cites a row that does not exist.",
                    source_row_ids=["alice-populated-s9-99"],
                )
            ]
        )
        fake_model.scripted = {"QbrContent": [content]}

        result = await synthesize_qbr(_config(), _reports(), [], fake_model)

        assert len(result.grounding_warnings) == 1
        assert "alice-populated-s9-99" in result.grounding_warnings[0]

    async def test_large_context_triggers_chunked_distillation(
        self,
        fake_model: StructuredFakeChatModel,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import quarterly_pulse.qbr as qbr

        monkeypatch.setattr(qbr, "_CONTEXT_CHAR_BUDGET", 500)
        monkeypatch.setattr(qbr, "_CHUNK_CHAR_BUDGET", 300)

        distilled = DistilledRows(
            rows=[
                DistilledRow(
                    row_id="alice-populated-s2-00",
                    contributor="Alice Tan",
                    section=2,
                    title="fleet-operator",
                    summary="Merged GPU scheduling PRs.",
                )
            ]
        )
        fake_model.scripted = {
            "DistilledRows": [distilled] + [DistilledRows() for _ in range(30)],
            "QbrContent": [_content()],
        }

        result = await synthesize_qbr(_config(), _reports(), [], fake_model)

        assert result.chunked is True
        assert result.rows_for_synthesis == 1
        # row_id survives distillation so citations stay resolvable.
        _, final_prompt = fake_model.inputs_seen[-1]
        assert "alice-populated-s2-00" in list(final_prompt.messages)[1].content
