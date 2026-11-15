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
    _parse_linkedin_datetime,
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
        def fake_actor(token: str, actor_id: str, run_input: dict) -> list[dict]:
            assert token == "apify-token"
            assert run_input == {"profileUrls": [PROFILE_URL]}
            return [
                {
                    "text": "Shared our Q3 field enablement recap.",
                    "postedAt": "2026-08-30T10:00:00Z",
                    "url": "https://www.linkedin.com/posts/caldeirav_recap-1",
                }
            ]

        monkeypatch.setattr(linkedin_module, "_run_apify_actor", fake_actor)

        result = await fetch_linkedin_activity(
            PROFILE_URL, CONTRIBUTOR_NAME, START_DATE, END_DATE,
            tmp_path, make_settings(apify_api_token="apify-token"),
        )

        assert result.strategy == "apify_actor"
        assert len(result.items) == 1
        assert result.items[0].date == date(2026, 8, 30)

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
