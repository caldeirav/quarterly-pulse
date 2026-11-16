"""Unit tests for the LinkedIn connector fallback chain (offline)."""

from __future__ import annotations

import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import respx

from quarterly_pulse.connectors import linkedin as linkedin_module
from quarterly_pulse.connectors.linkedin import (
    PORTABILITY_URL,
    SCRAPECREATORS_POSTS_URL,
    EXA_SEARCH_URL,
    ShareRecord,
    _parse_linkedin_datetime,
    _records_to_items,
    fetch_linkedin_activity,
    find_linkedin_export,
    linkedin_exclusions,
)

PROFILE_URL = "https://www.linkedin.com/in/caldeirav/"
CONTRIBUTOR_NAME = "Vincent Caldeira"
START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 9, 30)


class TestPortabilityApiStrategy:
    @respx.mock
    async def test_strategy_a_filters_by_start_date(
        self, linkedin_portability_response: dict, make_settings, tmp_path: Path
    ) -> None:
        elements = linkedin_portability_response["elements"]
        routes = {}
        for snapshot_type in ("SHARE_HISTORY", "ARTICLES", "COMMENTS"):
            routes[snapshot_type] = respx.get(
                PORTABILITY_URL, params={"snapshotType": snapshot_type}
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "elements": [
                            element
                            for element in elements
                            if element["snapshotType"] == snapshot_type
                        ]
                    },
                )
            )

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(linkedin_access_token="li-token"),
        )

        assert result.strategy == "member_data_portability_api"
        assert len(result.items) == 2  # the July comment is filtered out
        post = next(item for item in result.items if "RAG reference architecture" in item.title)
        assert post.date == date(2026, 8, 12)
        assert post.url == "https://www.linkedin.com/posts/caldeirav_rag-activity-7234"
        article = next(item for item in result.items if "Field notes" in item.title)
        assert article.date == date(2026, 9, 2)

        headers = routes["SHARE_HISTORY"].calls.last.request.headers
        assert headers["authorization"] == "Bearer li-token"
        assert headers["linkedin-version"] == "202411"

    @respx.mock
    async def test_strategy_a_failure_falls_back_to_export(
        self, make_settings, tmp_path: Path, export_dir: Path
    ) -> None:
        respx.get(PORTABILITY_URL).mock(
            return_value=httpx.Response(401, json={"message": "unauthorized"})
        )

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(linkedin_access_token="expired"),
        )

        assert result.strategy == "local_export_archive"
        assert any("Portability API failed" in warning for warning in result.warnings)
        assert len(result.items) == 3


class TestExportArchiveStrategy:
    async def test_directory_export_uses_shared_mapper(
        self, make_settings, tmp_path: Path, export_dir: Path
    ) -> None:
        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE, tmp_path, make_settings()
        )

        assert result.strategy == "local_export_archive"
        titles = [item.title for item in result.items]
        # 1 in-window share + 1 article + 1 in-window position change.
        assert len(result.items) == 3
        assert any("RAG reference architecture" in title for title in titles)
        assert any("Field notes" in title for title in titles)
        assert any("Advisory Board Member" in title for title in titles)
        # Pre-window share and 2022 position are filtered out.
        assert not any("before the reporting window" in title for title in titles)
        assert not any("CTO APAC" in title for title in titles)

    async def test_zip_export(
        self, make_settings, tmp_path: Path, export_zip: Path
    ) -> None:
        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE, tmp_path, make_settings()
        )
        assert result.strategy == "local_export_archive"
        assert len(result.items) == 3


class TestThirdPartyStrategies:
    async def test_apify_actor(
        self, make_settings, monkeypatch, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        def fake_actor(token: str, actor_id: str, run_input: dict) -> list[dict]:
            captured["token"] = token
            captured["actor"] = actor_id
            captured["input"] = run_input
            return [
                {
                    "urn": "7123456789012345678",
                    "posted_at": {"date": "2026-08-30 10:00:20", "timestamp": 1756544420000},
                    "text": "Shared our Q3 field enablement recap.",
                    "url": "https://www.linkedin.com/posts/caldeirav_recap-1",
                    "stats": {"total_reactions": 42, "comments": 7},
                }
            ]

        monkeypatch.setattr(linkedin_module, "_run_apify_actor", fake_actor)

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(apify_api_token="apify-token"),
        )

        assert result.strategy == "apify_actor"
        assert captured["token"] == "apify-token"
        assert captured["actor"] == "apimaestro/linkedin-profile-posts"
        assert captured["input"] == {"username": "caldeirav", "page_number": 1, "limit": 100}
        assert len(result.items) == 1
        item = result.items[0]
        assert item.date == date(2026, 8, 30)  # nested posted_at.date parsed
        assert item.url == "https://www.linkedin.com/posts/caldeirav_recap-1"
        assert item.impact == ""  # no engagement stats in report content
        assert "Q3 field enablement recap" in item.summary

    async def test_apify_wrapped_dataset_response(
        self, make_settings, monkeypatch, tmp_path: Path
    ) -> None:
        def fake_actor(token: str, actor_id: str, run_input: dict) -> list[dict]:
            return [
                {
                    "success": True,
                    "data": {
                        "posts": [
                            {
                                "urn": "1",
                                "posted_at": {"date": "2026-09-02 08:00:00"},
                                "text": "Wrapped response post.",
                                "url": "https://www.linkedin.com/posts/caldeirav_wrapped-1",
                            }
                        ]
                    },
                }
            ]

        monkeypatch.setattr(linkedin_module, "_run_apify_actor", fake_actor)

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(apify_api_token="apify-token"),
        )

        assert result.strategy == "apify_actor"
        assert len(result.items) == 1
        assert result.items[0].date == date(2026, 9, 2)

    async def test_apify_input_override(
        self, make_settings, monkeypatch, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        def fake_actor(token: str, actor_id: str, run_input: dict) -> list[dict]:
            captured["actor"] = actor_id
            captured["input"] = run_input
            return []

        monkeypatch.setattr(linkedin_module, "_run_apify_actor", fake_actor)
        settings = make_settings(
            apify_api_token="apify-token",
            apify_linkedin_actor="custom/actor",
            apify_linkedin_input='{"username": "{username}", "total_posts": 50}',
        )

        await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE, tmp_path, settings
        )

        assert captured["actor"] == "custom/actor"
        assert captured["input"] == {"username": "caldeirav", "total_posts": 50}

    async def test_apify_failure_degrades_gracefully(
        self, make_settings, monkeypatch, tmp_path: Path
    ) -> None:
        class ApifyishError(Exception):
            """Mimics apify_client errors (not an OSError/RuntimeError/ValueError)."""

        def failing_actor(token: str, actor_id: str, run_input: dict) -> list[dict]:
            raise ApifyishError("Actor with this name was not found")

        monkeypatch.setattr(linkedin_module, "_run_apify_actor", failing_actor)

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(apify_api_token="apify-token"),
        )

        # The pipeline must not crash; it falls through to graceful degradation.
        assert result.strategy == "unconfigured"
        assert result.items == []
        warnings = " ".join(result.warnings)
        assert "Apify LinkedIn scraper failed" in warnings
        assert "Actor with this name was not found" in warnings

    @respx.mock
    async def test_scrapecreators_endpoint(
        self, make_settings, tmp_path: Path
    ) -> None:
        route = respx.get(SCRAPECREATORS_POSTS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "text": "Webinar recap: AI in regulated industries",
                            "createdAt": "2026-09-20T00:00:00Z",
                            "url": "https://www.linkedin.com/posts/caldeirav_webinar-1",
                        }
                    ]
                },
            )
        )

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(scrapecreators_api_key="sc-key"),
        )

        assert result.strategy == "scrapecreators_api"
        assert len(result.items) == 1
        assert result.items[0].date == date(2026, 9, 20)
        assert route.calls.last.request.headers["authorization"] == "Bearer sc-key"

    @respx.mock
    async def test_exa_search(self, make_settings, tmp_path: Path) -> None:
        route = respx.post(EXA_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "CTO APAC shares RAG reference architecture",
                            "url": "https://www.linkedin.com/pulse/rag-arch",
                            "publishedDate": "2026-08-14",
                            "text": "Article mentioning Vincent Caldeira.",
                        }
                    ]
                },
            )
        )

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(exa_api_key="exa-key"),
        )

        assert result.strategy == "exa_search"
        assert len(result.items) == 1
        assert result.items[0].date == date(2026, 8, 14)
        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert "site:linkedin.com" in body["query"]
        assert body["startPublishedDate"] == "2026-08-01"


class TestGracefulDegradation:
    async def test_unconfigured_warns_and_continues(self, make_settings, tmp_path: Path) -> None:
        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE, tmp_path, make_settings()
        )

        assert result.strategy == "unconfigured"
        assert result.items == []
        warnings = " ".join(result.warnings)
        assert "LINKEDIN_ACCESS_TOKEN" in warnings
        assert "APIFY_API_TOKEN" in warnings
        assert "SCRAPECREATORS_API_KEY" in warnings
        assert "EXA_API_KEY" in warnings


class TestFindLinkedInExport:
    def test_finds_named_export_dir(self, tmp_path: Path) -> None:
        export = tmp_path / "LinkedIn Data Export"
        export.mkdir()
        (export / "Shares.csv").write_text("Date\n")
        assert find_linkedin_export(tmp_path) == export

    def test_finds_dir_with_marker_files(self, tmp_path: Path) -> None:
        export = tmp_path / "export"
        export.mkdir()
        (export / "Positions.csv").write_text("Company Name\n")
        assert find_linkedin_export(tmp_path) == export

    def test_finds_linkedin_zip(self, tmp_path: Path) -> None:
        with zipfile.ZipFile(tmp_path / "linkedin-export.zip", "w") as archive:
            archive.writestr("Shares.csv", "Date\n")
        assert find_linkedin_export(tmp_path) == tmp_path / "linkedin-export.zip"

    def test_finds_loose_markers_in_docs_dir(self, tmp_path: Path) -> None:
        (tmp_path / "Shares.csv").write_text("Date\n")
        assert find_linkedin_export(tmp_path) == tmp_path

    def test_ignores_unrelated_zip(self, tmp_path: Path) -> None:
        with zipfile.ZipFile(tmp_path / "random-archive.zip", "w") as archive:
            archive.writestr("readme.txt", "not a linkedin export\n")
        assert find_linkedin_export(tmp_path) is None

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("hello\n")
        assert find_linkedin_export(tmp_path) is None


class TestExclusions:
    def test_directory_export_excludes_whole_dir(self, tmp_path: Path) -> None:
        export = tmp_path / "LinkedIn Data Export"
        export.mkdir()
        assert linkedin_exclusions(export, tmp_path) == {export}

    def test_loose_markers_exclude_marker_files_and_articles(self, tmp_path: Path) -> None:
        for marker in ("Shares.csv", "Positions.csv"):
            (tmp_path / marker).write_text("x\n")
        articles = tmp_path / "Articles"
        articles.mkdir()
        (articles / "a.html").write_text("<html></html>")

        exclusions = linkedin_exclusions(tmp_path, tmp_path)

        assert (tmp_path / "Shares.csv") in exclusions
        assert (tmp_path / "Positions.csv") in exclusions
        assert articles in exclusions

    def test_no_archive_means_no_exclusions(self, tmp_path: Path) -> None:
        assert linkedin_exclusions(None, tmp_path) == set()


class TestDatetimeParsing:
    def test_iso_with_z_suffix(self) -> None:
        assert _parse_linkedin_datetime("2026-08-12T09:00:00Z") == date(2026, 8, 12)

    def test_csv_style_datetime(self) -> None:
        assert _parse_linkedin_datetime("2026-08-12 09:00") == date(2026, 8, 12)

    def test_epoch_milliseconds(self) -> None:
        epoch_ms = int(datetime(2026, 8, 12, 9, tzinfo=timezone.utc).timestamp() * 1000)
        assert _parse_linkedin_datetime(epoch_ms) == date(2026, 8, 12)

    def test_none_and_garbage(self) -> None:
        assert _parse_linkedin_datetime(None) is None
        assert _parse_linkedin_datetime("not a date") is None


class TestUrlCleaning:
    def test_tracking_params_stripped_from_share_urls(self) -> None:
        records = [
            ShareRecord(
                kind="post",
                occurred_at="2026-08-12 09:00",
                text="hello",
                url=(
                    "https://www.linkedin.com/posts/caldeirav_x-activity-1"
                    "?utm_source=social_share_send&utm_medium=member_desktop_web&rcm=ACoAAA"
                ),
            )
        ]
        items = _records_to_items(records, date(2026, 8, 1), date(2026, 9, 30))
        assert items[0].url == "https://www.linkedin.com/posts/caldeirav_x-activity-1"

    def test_plain_urls_untouched(self) -> None:
        records = [ShareRecord(kind="post", occurred_at="2026-08-12 09:00", text="x",
                               url="https://www.linkedin.com/posts/caldeirav_y-activity-2")]
        items = _records_to_items(records, date(2026, 8, 1), date(2026, 9, 30))
        assert items[0].url == "https://www.linkedin.com/posts/caldeirav_y-activity-2"


class TestItemShape:
    def test_title_is_the_posts_own_headline_without_prefix(self) -> None:
        records = [
            ShareRecord(
                kind="post",
                occurred_at="2026-08-30 10:00",
                text="Agent Substrate Day talk recap and deck",
                url="https://www.linkedin.com/posts/caldeirav_recap-1",
            )
        ]
        items = _records_to_items(records, date(2026, 8, 1), date(2026, 9, 30))
        assert items[0].title == "Agent Substrate Day talk recap and deck"
        assert not items[0].title.startswith("LinkedIn")
        assert "linkedin post" in items[0].raw_context  # kind kept for traceability

    def test_title_is_first_complete_sentence_not_a_hard_clip(self) -> None:
        text = (
            "Presented at Agent Substrate Day on instant circuit breakers achieving sub-15ms "
            "verification. The deck and benchmark results are now available for the team. "
            "Thanks to everyone who came to the session and asked such sharp questions."
        )
        records = [ShareRecord(kind="post", occurred_at="2026-08-30 10:00", text=text)]
        items = _records_to_items(records, date(2026, 8, 1), date(2026, 9, 30))
        assert items[0].title == (
            "Presented at Agent Substrate Day on instant circuit breakers achieving sub-15ms verification."
        )
        assert not items[0].title.endswith("…")  # never a mid-word character clip

    def test_title_sentence_capped_gracefully_when_very_long(self) -> None:
        text = "This single sentence goes on and on and on " * 10  # no punctuation at all
        records = [ShareRecord(kind="post", occurred_at="2026-08-30 10:00", text=text)]
        items = _records_to_items(records, date(2026, 8, 1), date(2026, 9, 30))
        assert len(items[0].title) <= 140
        assert items[0].title.endswith("…")  # capped on a word boundary

    def test_summary_keeps_enough_text_for_multi_sentence_descriptions(self) -> None:
        long_text = " ".join(f"Sentence number {i} about the event." for i in range(60))
        assert len(long_text) > 900
        records = [ShareRecord(kind="post", occurred_at="2026-08-30 10:00", text=long_text)]
        items = _records_to_items(records, date(2026, 8, 1), date(2026, 9, 30))
        assert 850 <= len(items[0].summary) <= 910  # clipped near the 900-char budget
