"""Unit tests for the LangChain synthesizer (offline, scripted structured outputs)."""

from __future__ import annotations

from datetime import date

import pytest

from quarterly_pulse.models import ActivityItem, TemplateProfile
from quarterly_pulse.synthesizer import (
    DistilledActivities,
    DistilledActivity,
    EngagementRow,
    QuarterReport,
    UpstreamContributionRow,
    CustomerFieldWorkRow,
    LookingAheadRow,
    synthesize_report,
    deduplicate_items,
)

from tests.fakes import StructuredFakeChatModel

PROFILE = TemplateProfile(
    name_role="Vincent Caldeira, CTO APAC",
    team_geo="Field CTO Team APAC",
    quarter="Q3 2026",
    github_handle="caldeirav",
    linkedin_url="https://www.linkedin.com/in/caldeirav/",
    okrs="Enterprise AI adoption",
)


def _sample_items() -> list[ActivityItem]:
    return [
        ActivityItem(
            source="github",
            date=date(2026, 8, 5),
            title="PR: Add GPU scheduling support to fleet operator (upstream-project/fleet-operator)",
            summary="Implements topology-aware GPU scheduling.",
            impact="merged",
            url="https://github.com/upstream-project/fleet-operator/pull/42",
        ),
        ActivityItem(
            source="linkedin",
            date=date(2026, 8, 12),
            title="LinkedIn post: multi-cluster RAG reference architecture",
            summary="Shared the reference architecture; 6 field teams using it.",
            url="https://www.linkedin.com/posts/caldeirav_rag-activity-7234",
        ),
        ActivityItem(
            source="docs",
            date=date(2026, 8, 20),
            title="Acme Corp POC readout",
            summary="3-week architecture sprint with Acme Corp; unblocked renewal.",
            url="https://docs.google.com/presentation/d/abc123/edit",
            raw_context="document: customer-poc-readout.md; urls: https://docs.google.com/presentation/d/abc123/edit",
        ),
    ]


def _scripted_report() -> QuarterReport:
    return QuarterReport(
        section_2_upstream=[
            UpstreamContributionRow(
                project_repo_contribution="upstream-project/fleet-operator",
                what_you_did_and_why="Merged PR adding topology-aware GPU scheduling; adopted in v1.8.",
                links="https://github.com/upstream-project/fleet-operator/pull/42",
            )
        ],
        section_4_customers=[
            CustomerFieldWorkRow(
                customer_segment_opportunity="Acme Corp (Financial Services)",
                activity_and_outcome="3-week architecture sprint; technical win unblocking renewal.",
                links="customer-poc-readout.md and https://docs.google.com/presentation/d/abc123/edit",
            )
        ],
        section_8_looking_ahead=[],
        section_9_help_needed=[],
    )


class TestDeduplicateItems:
    def test_url_duplicates_collapse(self) -> None:
        items = _sample_items()
        items.append(items[0].model_copy())
        assert len(deduplicate_items(items)) == 3

    def test_same_title_and_date_collapse(self) -> None:
        base = ActivityItem(source="github", title="Commits: org/repo (5 commits)", date=None)
        duplicate = ActivityItem(source="github", title="Commits: org/REPO (5 commits)!!", date=None)
        assert len(deduplicate_items([base, duplicate])) == 1

    def test_cross_source_duplicates_survive(self) -> None:
        # The LLM merges cross-source duplicates semantically; deterministic
        # dedup must not drop them.
        github = ActivityItem(source="github", title="RAG reference arch", url="https://github.com/x/y")
        docs = ActivityItem(source="docs", title="RAG reference arch", url="https://github.com/x/y")
        assert len(deduplicate_items([github, docs])) == 2

    def test_same_title_posts_with_different_urls_survive(self) -> None:
        # Event-series posts often share an opening line; distinct URLs must
        # never collapse just because the title clip and date match.
        posts = [
            ActivityItem(
                source="linkedin",
                title="Summit Connect — a great event with customers and partners",
                date=date(2026, 8, 20),
                url=f"https://www.linkedin.com/posts/caldeirav_summit-connect-activity-{index}",
            )
            for index in (1, 2)
        ]
        assert len(deduplicate_items(posts)) == 2


class TestSynthesizeReport:
    async def test_maps_items_via_lcel_chain(self, fake_model: StructuredFakeChatModel) -> None:
        fake_model.scripted = {"QuarterReport": [_scripted_report()]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        assert result.items_received == 3
        assert result.items_after_dedup == 3
        assert result.chunked is False
        assert result.grounding_violations == []
        assert result.report.section_2_upstream[0].project_repo_contribution == "upstream-project/fleet-operator"
        assert result.report.section_4_customers[0].customer_segment_opportunity == "Acme Corp (Financial Services)"

    async def test_prompt_contains_grounding_rules_profile_and_items(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        fake_model.scripted = {"QuarterReport": [_scripted_report()]}

        await synthesize_report(PROFILE, _sample_items(), fake_model)

        assert len(fake_model.inputs_seen) == 1
        schema_name, prompt_value = fake_model.inputs_seen[0]
        assert schema_name == "QuarterReport"
        messages = list(prompt_value.messages) if hasattr(prompt_value, "messages") else list(prompt_value)
        system = messages[0].content
        human = messages[1].content
        assert "NEVER fabricate" in system
        assert "ONE row per GitHub repository" in system
        assert "EVERY LinkedIn post" in system
        assert "EVERY gathered item must appear somewhere" in system
        assert "catch-all" in system
        assert 'never prefix it with "LinkedIn"' in system
        assert "coincides with presenting" in system
        assert "Summit Connect" in system
        assert "AI Center of Excellence" in system
        assert "character-for-character" in system
        assert "PROPOSE concrete recommendations" in system  # sections 8/9 suggestions
        assert "Vincent Caldeira, CTO APAC" in human
        assert "caldeirav" in human
        assert "upstream-project/fleet-operator/pull/42" in human
        assert "Acme Corp" in human

    async def test_empty_items_skip_llm(self, fake_model: StructuredFakeChatModel) -> None:
        result = await synthesize_report(PROFILE, [], fake_model)
        assert fake_model.inputs_seen == []
        assert result.report == QuarterReport()
        assert result.items_for_synthesis == 0

    async def test_fabricated_urls_stripped_and_recorded(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        report = _scripted_report()
        report.section_2_upstream[0].links = (
            "https://github.com/upstream-project/fleet-operator/pull/42 and "
            "https://github.com/nonexistent/fake-repo plus [deck](https://example.com/fake)"
        )
        fake_model.scripted = {"QuarterReport": [report]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        links = result.report.section_2_upstream[0].links
        assert "https://github.com/upstream-project/fleet-operator/pull/42" in links
        assert "fake-repo" not in links
        assert "example.com" not in links
        assert "deck" in links  # link text survives, URL removed
        assert len(result.grounding_violations) == 2

    async def test_document_paths_are_grounded(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        fake_model.scripted = {"QuarterReport": [_scripted_report()]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        # customer-poc-readout.md comes from the docs item's raw_context and must survive.
        assert "customer-poc-readout.md" in result.report.section_4_customers[0].links
        assert result.grounding_violations == []

    async def test_empty_rows_dropped(self, fake_model: StructuredFakeChatModel) -> None:
        report = _scripted_report()
        report.section_5_engagements = [
            EngagementRow(engagement_event_partner="", activity_audience_outcome="", links=""),
            EngagementRow(
                engagement_event_partner="KubeCon talk",
                activity_audience_outcome="Delivered to 300+ attendees.",
                links="",
            ),
        ]
        fake_model.scripted = {"QuarterReport": [report]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        assert len(result.report.section_5_engagements) == 1

    async def test_section8_rows_pass_through(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        report = _scripted_report()
        report.section_8_looking_ahead = [
            LookingAheadRow(
                type="Must-Win Priority",
                initiative_activity="Ship v2.0 deployment template",
                target_outcome_timing_or_capacity="Target GA by mid-Q4.",
            )
        ]
        fake_model.scripted = {"QuarterReport": [report]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        assert result.report.section_8_looking_ahead[0].type == "Must-Win Priority"


class TestCompletenessBackstop:
    async def test_unrepresented_items_backfilled_to_section6(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        # The model returned an empty report; every item must still land somewhere.
        fake_model.scripted = {"QuarterReport": [QuarterReport()]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        assert result.backfilled_count == 3
        assert len(result.report.section_6_custom) == 3
        assert result.report.section_6_title == "Additional Gathered Activity"
        titles = {row.initiative_activity_name for row in result.report.section_6_custom}
        assert "PR: Add GPU scheduling support to fleet operator (upstream-project/fleet-operator)" in titles
        assert all("auto-added" in row.what_you_did_outcome for row in result.report.section_6_custom)
        # Injected links are grounded by construction.
        assert result.grounding_violations == []

    async def test_represented_items_not_backfilled(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        # The scripted report covers the GitHub PR (by URL) and the docs item
        # (by path); only the LinkedIn post is absent.
        fake_model.scripted = {"QuarterReport": [_scripted_report()]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        assert result.backfilled_count == 1
        row = result.report.section_6_custom[0]
        assert row.initiative_activity_name.startswith("LinkedIn post: multi-cluster RAG")
        assert row.links == "https://www.linkedin.com/posts/caldeirav_rag-activity-7234"

    async def test_truncated_url_citations_still_count_as_represented(
        self, fake_model: StructuredFakeChatModel
    ) -> None:
        report = _scripted_report()
        report.section_2_upstream[0].links = (
            "https://github.com/upstream-project/fleet-operator/pull/4"  # truncated by the model
        )
        fake_model.scripted = {"QuarterReport": [report]}

        result = await synthesize_report(PROFILE, _sample_items(), fake_model)

        # The truncated link survives grounding ...
        assert "pull/4" in result.report.section_2_upstream[0].links
        assert result.grounding_violations == []
        # ... and still counts as representing the GitHub item (no duplicate row).
        github_rows = [
            row
            for row in result.report.section_6_custom
            if "fleet-operator" in row.initiative_activity_name
        ]
        assert github_rows == []


class TestChunkedSynthesis:
    async def test_large_context_triggers_abatch_summarization(
        self,
        fake_model: StructuredFakeChatModel,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import quarterly_pulse.synthesizer as synthesizer

        monkeypatch.setattr(synthesizer, "_CONTEXT_CHAR_BUDGET", 400)
        monkeypatch.setattr(synthesizer, "_CHUNK_CHAR_BUDGET", 200)

        distilled = DistilledActivities(
            activities=[
                DistilledActivity(
                    source="github",
                    date="2026-08-05",
                    title="PR: Add GPU scheduling support to fleet operator (upstream-project/fleet-operator)",
                    summary="Merged; adopted in v1.8.",
                    url="https://github.com/upstream-project/fleet-operator/pull/42",
                )
            ]
        )
        items = _sample_items() * 4  # duplicates collapse; 3 unique records remain

        records = [synthesizer._compact_item(item) for item in deduplicate_items(items)]
        chunks = synthesizer._chunk_records(records, synthesizer._CHUNK_CHAR_BUDGET)
        assert len(chunks) >= 2  # the shrunken budget must force multiple batches

        fake_model.scripted = {
            "DistilledActivities": [distilled] + [DistilledActivities() for _ in chunks[1:]],
            "QuarterReport": [_scripted_report()],
        }

        result = await synthesize_report(PROFILE, items, fake_model)

        assert result.chunked is True
        assert result.items_for_synthesis == 1  # only the distilled record remains
        # The summarize chain ran once per chunk via abatch, then the final chain once.
        schema_names = [name for name, _ in fake_model.inputs_seen]
        assert schema_names.count("DistilledActivities") == len(chunks)
        assert schema_names.count("QuarterReport") == 1
        # The final prompt is built from distilled records, not raw duplicates.
        _, final_prompt = fake_model.inputs_seen[-1]
        messages = list(final_prompt.messages) if hasattr(final_prompt, "messages") else list(final_prompt)
        assert "Merged; adopted in v1.8." in messages[1].content
