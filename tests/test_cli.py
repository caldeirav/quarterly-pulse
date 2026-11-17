"""End-to-end offline CLI tests via typer's CliRunner."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from quarterly_pulse import cli as cli_module
from quarterly_pulse.cli import app
from quarterly_pulse.models import ActivityItem, ConnectorResult, TemplateProfile
from quarterly_pulse.synthesizer import CustomerFieldWorkRow, QuarterReport, UpstreamContributionRow

from tests.fakes import StructuredFakeChatModel

runner = CliRunner()

PROFILE_URL = "https://www.linkedin.com/in/caldeirav/"


def _e2e_report() -> QuarterReport:
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
                activity_and_outcome="3-week architecture sprint; technical win.",
                links="customer-poc-readout.md",
            )
        ],
    )


def _install_fakes(monkeypatch, docs_dir: Path, report: QuarterReport) -> StructuredFakeChatModel:
    """Replace the LLM and the network-bound connectors with deterministic fakes."""
    fake_model = StructuredFakeChatModel(responses=["unused"])
    fake_model.scripted = {"QuarterReport": [report]}

    async def fake_github(handle: str, start_date: date, end_date: date, settings) -> ConnectorResult:
        assert handle == "caldeirav"
        assert start_date == date(2026, 8, 1)
        assert end_date == date(2026, 9, 30)
        return ConnectorResult(
            source="github",
            strategy="graphql_api + search_api",
            items=[
                ActivityItem(
                    source="github",
                    date=date(2026, 8, 5),
                    title="PR: Add GPU scheduling support (upstream-project/fleet-operator)",
                    summary="Merged; adopted in v1.8.",
                    impact="merged",
                    url="https://github.com/upstream-project/fleet-operator/pull/42",
                )
            ],
        )

    async def fake_linkedin(
        profile_url: str | None,
        contributor_name: str | None,
        start_date: date,
        end_date: date,
        docs_dir: Path,
        settings,
    ) -> ConnectorResult:
        assert profile_url == PROFILE_URL
        assert contributor_name == "Vincent Caldeira"
        return ConnectorResult(
            source="linkedin",
            strategy="unconfigured",
            items=[],
            warnings=["LinkedIn data unavailable: set LINKEDIN_ACCESS_TOKEN or add an export archive."],
        )

    monkeypatch.setattr(cli_module, "init_chat_model", lambda model_id: fake_model)
    monkeypatch.setattr(cli_module, "fetch_github_activity", fake_github)
    monkeypatch.setattr(cli_module, "fetch_linkedin_activity", fake_linkedin)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    return fake_model


class TestEndToEndOffline:
    def test_full_run_writes_populated_report(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # keep the developer's real .env out of Settings()
        _install_fakes(monkeypatch, docs_dir, _e2e_report())
        output = tmp_path / "report.md"

        result = runner.invoke(
            app,
            [
                "populate",
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
                "--output", str(output),
            ],
        )

        assert result.exit_code == 0, result.output
        text = output.read_text(encoding="utf-8")

        # Generated, grounded rows from all three sources.
        assert "upstream-project/fleet-operator" in text
        assert "customer-poc-readout.md" in text
        assert "Acme Corp (Financial Services)" in text

        # Template structure preserved; placeholders replaced in populated sections.
        assert "## 2\\. Open Source & Upstream Contributions" in text
        assert "Vincent Caldeira, CTO APAC" in text
        tail = text[text.index("## 2") :]
        assert "*e.g.," not in tail

        # Audit footer with per-source strategies (docs ran for real).
        assert "<details>" in text
        assert "graphql_api + search_api" in text
        assert "local_documents" in text
        assert "unconfigured" in text
        assert "LinkedIn data unavailable" in text
        assert "openai:gpt-4o" in text
        assert "Report rows per section" in text

    def test_default_output_written_to_cwd(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        _install_fakes(monkeypatch, docs_dir, _e2e_report())
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "populate",
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
            ],
        )

        assert result.exit_code == 0, result.output
        assert (tmp_path / "quarterly-template_populated.md").is_file()

    def test_handle_overrides_passed_to_connectors(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # keep the developer's real .env out of Settings()
        captured: dict[str, str | None] = {}

        async def fake_github(handle: str, start_date: date, end_date: date, settings) -> ConnectorResult:
            captured["github"] = handle
            return ConnectorResult(source="github", strategy="graphql_api + search_api", items=[])

        async def fake_linkedin(
            profile_url: str | None,
            contributor_name: str | None,
            start_date: date,
            end_date: date,
            docs_dir: Path,
            settings,
        ) -> ConnectorResult:
            captured["linkedin"] = profile_url
            return ConnectorResult(source="linkedin", strategy="unconfigured", items=[], warnings=[])

        fake_model = StructuredFakeChatModel(responses=["unused"])
        fake_model.scripted = {"QuarterReport": [QuarterReport()]}
        monkeypatch.setattr(cli_module, "init_chat_model", lambda model_id: fake_model)
        monkeypatch.setattr(cli_module, "fetch_github_activity", fake_github)
        monkeypatch.setattr(cli_module, "fetch_linkedin_activity", fake_linkedin)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.delenv("LLM_MODEL", raising=False)

        result = runner.invoke(
            app,
            [
                "populate",
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--github", "@octocat",
                "--linkedin", "https://www.linkedin.com/in/octocat/",
                "--output", str(tmp_path / "out.md"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["github"] == "octocat"
        assert captured["linkedin"] == "https://www.linkedin.com/in/octocat/"


class TestFailLoudly:
    def test_missing_api_key_exits_nonzero(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        # Isolate from the developer's real .env (Settings reads it from the CWD).
        monkeypatch.chdir(tmp_path)
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_MODEL"):
            monkeypatch.delenv(var, raising=False)

        result = runner.invoke(
            app,
            [
                "populate",
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--output", str(tmp_path / "out.md"),
            ],
        )

        assert result.exit_code == 1
        assert "OPENAI_API_KEY" in result.output
        assert not (tmp_path / "out.md").exists()

    def test_end_date_before_start_date_rejected(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # keep the developer's real .env out of Settings()
        _install_fakes(monkeypatch, docs_dir, _e2e_report())

        result = runner.invoke(
            app,
            [
                "populate",
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-09-30",
                "--end-date", "2026-08-01",
                "--output", str(tmp_path / "out.md"),
            ],
        )

        assert result.exit_code == 2
        assert "before --start-date" in result.output


class TestConnectorSafetyNet:
    async def test_crashing_connector_does_not_kill_pipeline(
        self, tmp_path: Path, monkeypatch, make_settings
    ) -> None:
        async def exploding(docs_dir, start_date, end_date, exclude=None):
            raise RuntimeError("unexpected docs connector bug")

        monkeypatch.setattr(cli_module, "fetch_docs_activity", exploding)

        profile = cli_module._apply_overrides(
            TemplateProfile(name_role="Vincent Caldeira, CTO APAC"), github=None, linkedin=None
        )
        results = await cli_module._gather_activity(
            profile, tmp_path, date(2026, 8, 1), date(2026, 9, 30), make_settings()
        )

        assert len(results) == 3
        github_result, linkedin_result, docs_result = results
        assert github_result.strategy == "skipped"  # no handle in profile
        assert linkedin_result.strategy == "unconfigured"  # no tokens, no export
        assert docs_result.strategy == "failed"
        assert any("crashed" in warning for warning in docs_result.warnings)
        assert any("unexpected docs connector bug" in warning for warning in docs_result.warnings)


class TestCaching:
    def _invoke(self, runner_args: list[str], monkeypatch, report: QuarterReport):
        fake_model = StructuredFakeChatModel(responses=["x"])
        fake_model.scripted = {"QuarterReport": [report]}
        monkeypatch.setattr(cli_module, "init_chat_model", lambda model_id: fake_model)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.delenv("LLM_MODEL", raising=False)
        return runner.invoke(app, ["populate", *runner_args])

    def test_cache_roundtrip_skips_connectors(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        _install_fakes(monkeypatch, docs_dir, _e2e_report())
        monkeypatch.chdir(tmp_path)  # keep the developer's real .env out of Settings()
        cache = tmp_path / "cache.json"

        result1 = self._invoke(
            [
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
                "--output", str(tmp_path / "first.md"),
                "--cache", str(cache),
            ],
            monkeypatch,
            _e2e_report(),
        )
        assert result1.exit_code == 0, result1.output
        assert cache.is_file()
        assert "wrote" in result1.output  # [cache] wrote ... on stderr (mixed in)

        # Second run: any connector invocation is fatal — proves they are skipped.
        async def must_not_run(*args, **kwargs):
            raise AssertionError("connectors must not run when a cache is used")

        monkeypatch.setattr(cli_module, "fetch_github_activity", must_not_run)
        monkeypatch.setattr(cli_module, "fetch_linkedin_activity", must_not_run)
        monkeypatch.setattr(cli_module, "fetch_docs_activity", must_not_run)

        result2 = self._invoke(
            [
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
                "--output", str(tmp_path / "second.md"),
                "--cache", str(cache),
            ],
            monkeypatch,
            _e2e_report(),
        )
        assert result2.exit_code == 0, result2.output
        assert "loaded" in result2.output  # [cache] loaded N items ...
        text2 = (tmp_path / "second.md").read_text(encoding="utf-8")
        assert "Sources loaded from cache" in text2  # audit footer reflects the cache

    def test_cache_window_mismatch_warns(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        _install_fakes(monkeypatch, docs_dir, _e2e_report())
        monkeypatch.chdir(tmp_path)
        cache = tmp_path / "cache.json"

        first = self._invoke(
            [
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
                "--output", str(tmp_path / "a.md"),
                "--cache", str(cache),
            ],
            monkeypatch,
            _e2e_report(),
        )
        assert first.exit_code == 0, first.output

        second = self._invoke(
            [
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-07-01",  # different window than the cache
                "--end-date", "2026-09-30",
                "--output", str(tmp_path / "b.md"),
                "--cache", str(cache),
            ],
            monkeypatch,
            _e2e_report(),
        )
        assert second.exit_code == 0, second.output
        assert "cache was gathered for 2026-08-01..2026-09-30" in second.output

    def test_refresh_regathers_and_overwrites(
        self, tmp_path: Path, docs_dir: Path, template_path: Path, monkeypatch
    ) -> None:
        _install_fakes(monkeypatch, docs_dir, _e2e_report())
        monkeypatch.chdir(tmp_path)
        cache = tmp_path / "cache.json"

        first = self._invoke(
            [
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
                "--output", str(tmp_path / "a.md"),
                "--cache", str(cache),
            ],
            monkeypatch,
            _e2e_report(),
        )
        assert first.exit_code == 0, first.output

        calls = {"github": 0}

        async def counting_github(handle, start_date, end_date, settings):
            calls["github"] += 1
            return ConnectorResult(source="github", strategy="graphql_api + search_api", items=[])

        monkeypatch.setattr(cli_module, "fetch_github_activity", counting_github)

        second = self._invoke(
            [
                "--template", str(template_path),
                "--docs-dir", str(docs_dir),
                "--start-date", "2026-08-01",
                "--end-date", "2026-09-30",
                "--output", str(tmp_path / "b.md"),
                "--cache", str(cache),
                "--refresh",
            ],
            monkeypatch,
            _e2e_report(),
        )
        assert second.exit_code == 0, second.output
        assert calls["github"] == 1  # connectors re-ran despite the cache
        assert "wrote" in second.output  # cache overwritten


class TestQbrCommand:
    def _invoke_qbr(self, args: list[str], monkeypatch, content) -> object:
        fake_model = StructuredFakeChatModel(responses=["unused"])
        fake_model.scripted = {"QbrContent": [content]}
        monkeypatch.setattr(cli_module, "init_chat_model", lambda model_id: fake_model)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.delenv("LLM_MODEL", raising=False)
        return runner.invoke(app, ["qbr", *args])

    def test_qbr_writes_deck_and_companion(
        self,
        tmp_path: Path,
        qbr_reports_dir: Path,
        qbr_template_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)  # keep the developer's real .env out of Settings()
        from tests.test_qbr import _content

        deck = tmp_path / "team-deck.pptx"
        result = self._invoke_qbr(
            [
                "--reports-dir", str(qbr_reports_dir),
                "--pptx", str(qbr_template_path),
                "--output", str(deck),
                "--no-interview",
            ],
            monkeypatch,
            _content(),
        )

        assert result.exit_code == 0, result.output
        assert deck.is_file()
        companion = tmp_path / "team-deck_sources.md"
        assert companion.is_file()
        assert "source rows" in result.output
        # The config file's leader/org reached the deck (via synthesized content check).
        deck_text = str(result.output)
        assert "2 report(s)" in deck_text

    def test_qbr_interview_answers_flow_into_synthesis(
        self,
        tmp_path: Path,
        qbr_reports_dir: Path,
        qbr_template_path: Path,
        monkeypatch,
    ) -> None:
        """Interactive path: plan -> prompt -> answer -> synthesize on one loop.

        Regression guard for the 'Event loop is closed' failure: interview
        planning and synthesis must share a single event loop because the
        provider SDK's async client binds to the first loop it runs on."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli_module, "_stdin_is_interactive", lambda: True)
        from quarterly_pulse.qbr import InterviewPlan, InterviewQuestion
        from tests.test_qbr import _content

        fake_model = StructuredFakeChatModel(responses=["unused"])
        plan = InterviewPlan(
            context_summary="Two contributors, Q3 2026.",
            questions=[
                InterviewQuestion(topic="must-win owner", question="Who owns the Q4 must-win?"),
                InterviewQuestion(topic="stop-list", question="What do we stop in Q4?"),
            ],
        )
        fake_model.scripted = {"InterviewPlan": [plan], "QbrContent": [_content()]}
        monkeypatch.setattr(cli_module, "init_chat_model", lambda model_id: fake_model)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.delenv("LLM_MODEL", raising=False)

        deck = tmp_path / "interviewed.pptx"
        result = runner.invoke(
            app,
            [
                "qbr",
                "--reports-dir", str(qbr_reports_dir),
                "--pptx", str(qbr_template_path),
                "--output", str(deck),
            ],
            input="Priya owns it\nstop the unsponsored POCs\n",
        )

        assert result.exit_code == 0, result.output
        assert deck.is_file()
        assert "Question 1 of up to 2" in result.output
        # The answers reached the synthesis prompt.
        _, synthesis_prompt = fake_model.inputs_seen[-1]
        human = list(synthesis_prompt.messages)[1].content
        assert "Priya owns it" in human
        assert "stop the unsponsored POCs" in human

    def test_qbr_requires_reports(self, tmp_path: Path, qbr_template_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        result = self._invoke_qbr(
            [
                "--reports-dir", str(empty),
                "--pptx", str(qbr_template_path),
                "--no-interview",
            ],
            monkeypatch,
            None,
        )
        assert result.exit_code == 2
        assert "No Markdown reports" in result.output

    def test_qbr_requires_template(self, tmp_path: Path, qbr_reports_dir: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = self._invoke_qbr(
            [
                "--reports-dir", str(qbr_reports_dir),
                "--pptx", str(tmp_path / "missing.pptx"),
                "--no-interview",
            ],
            monkeypatch,
            None,
        )
        assert result.exit_code == 2
        assert "QBR template not found" in result.output

    def test_qbr_missing_review_quarter_exits(
        self, tmp_path: Path, qbr_template_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # Reports without Section 1 quarters and no config: nothing to derive from.
        bare = tmp_path / "bare"
        bare.mkdir()
        (bare / "one.md").write_text(
            "# Log\n\n## 2\\. Open Source & Upstream Contributions\n\n"
            "| A | B | C |\n| :---- | :---- | :---- |\n"
            "| repo | merged a PR | link |\n",
            encoding="utf-8",
        )
        result = self._invoke_qbr(
            [
                "--reports-dir", str(bare),
                "--pptx", str(qbr_template_path),
                "--no-interview",
            ],
            monkeypatch,
            None,
        )
        assert result.exit_code == 2
        assert "review quarter" in result.output
