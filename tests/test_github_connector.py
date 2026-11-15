"""Unit tests for the GitHub connector (offline: respx mocks + monkeypatched gh)."""

from __future__ import annotations

from datetime import date

import httpx
import respx

from quarterly_pulse.connectors import github as github_module
from quarterly_pulse.connectors.github import (
    _dedupe_by_url,
    fetch_github_activity,
    map_graphql_contributions,
    map_search_results,
)
from quarterly_pulse.models import ActivityItem

HANDLE = "caldeirav"
START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 9, 30)


class TestMapGraphqlContributions:
    def test_maps_pr_issue_commit_and_review_contributions(
        self, github_graphql_response: dict
    ) -> None:
        warnings: list[str] = []
        items = map_graphql_contributions(github_graphql_response, HANDLE, warnings)

        assert len(items) == 7  # 2 PRs + 1 issue + 2 commit repos + 2 review-repo aggregates
        titles = [item.title for item in items]
        assert "PR: Add GPU scheduling support to fleet operator (upstream-project/fleet-operator)" in titles
        assert "Issue: RFC: multi-cluster telemetry contract (upstream-project/fleet-operator)" in titles
        assert "Commits: acme-internal/platform (27 commits)" in titles
        assert "PR reviews: upstream-project/fleet-operator (2 reviews)" in titles
        assert "PR reviews: acme-internal/platform (1 reviews)" in titles
        assert warnings == []

    def test_pr_fields(self, github_graphql_response: dict) -> None:
        items = map_graphql_contributions(github_graphql_response, HANDLE, [])
        pr = next(item for item in items if item.title.startswith("PR: Add GPU"))
        assert pr.date == date(2026, 8, 5)
        assert pr.impact == "merged"
        assert pr.url == "https://github.com/upstream-project/fleet-operator/pull/42"
        assert "topology-aware GPU scheduling" in pr.summary
        assert pr.source == "github"

    def test_missing_user_adds_warning(self) -> None:
        warnings: list[str] = []
        items = map_graphql_contributions({"data": {"user": None}}, HANDLE, warnings)
        assert items == []
        assert any("not found" in warning for warning in warnings)

    def test_graphql_errors_add_warning(self) -> None:
        warnings: list[str] = []
        payload = {"errors": [{"message": "Bad credentials"}], "data": {"user": None}}
        map_graphql_contributions(payload, HANDLE, warnings)
        assert any("Bad credentials" in warning for warning in warnings)


class TestMapSearchResults:
    def test_maps_prs_and_issues(self, github_search_response: dict) -> None:
        items = map_search_results(github_search_response)
        assert len(items) == 2
        pr = items[0]
        assert pr.title == "PR: Support custom schedulers in workload API (kubernetes-sigs/kueue)"
        assert pr.impact == "merged"
        assert pr.date == date(2026, 8, 15)
        assert pr.url == "https://github.com/kubernetes-sigs/kueue/pull/1234"

    def test_empty_response(self) -> None:
        assert map_search_results({"total_count": 0, "items": []}) == []


class TestDedupe:
    def test_dedupe_by_url(self) -> None:
        items = [
            ActivityItem(source="github", title="PR: same", url="https://example.com/1"),
            ActivityItem(source="github", title="PR: duplicate", url="https://example.com/1"),
            ActivityItem(source="github", title="PR: other", url="https://example.com/2"),
            ActivityItem(source="github", title="no url a"),
            ActivityItem(source="github", title="no url b"),
        ]
        assert len(_dedupe_by_url(items)) == 4

    def test_repo_aggregates_do_not_collide(self) -> None:
        repo_url = "https://github.com/acme-internal/platform"
        items = [
            ActivityItem(source="github", title="Commits: acme-internal/platform (27 commits)", url=repo_url),
            ActivityItem(source="github", title="PR reviews: acme-internal/platform (1 reviews)", url=repo_url),
        ]
        assert len(_dedupe_by_url(items)) == 2

    def test_identical_items_without_urls_collapse(self) -> None:
        items = [
            ActivityItem(source="github", title="no url"),
            ActivityItem(source="github", title="no url"),
        ]
        assert len(_dedupe_by_url(items)) == 1


class TestFetchViaHttpx:
    @respx.mock
    async def test_token_strategy_queries_both_apis_and_dedupes(
        self, github_graphql_response: dict, github_search_response: dict, make_settings
    ) -> None:
        graphql_route = respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(200, json=github_graphql_response)
        )
        search_route = respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(200, json=github_search_response)
        )

        result = await fetch_github_activity(
            HANDLE, START_DATE, END_DATE, make_settings(github_token="ghp_test")
        )

        assert result.strategy == "graphql_api + search_api"
        assert result.warnings == []
        assert len(result.items) == 8  # 7 GraphQL + 2 search - 1 URL duplicate
        urls = {item.url for item in result.items}
        assert "https://github.com/upstream-project/fleet-operator/pull/42" in urls

        auth = graphql_route.calls.last.request.headers["authorization"]
        assert auth == "Bearer ghp_test"
        query = search_route.calls.last.request.url.params["q"]
        assert f"author:{HANDLE}" in query
        assert "created:>=2026-08-01" in query
        assert "created:<2026-10-01" in query

    @respx.mock
    async def test_http_failures_degrade_gracefully(self, make_settings) -> None:
        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(403, json={"message": "rate limited"})
        )

        result = await fetch_github_activity(
            HANDLE, START_DATE, END_DATE, make_settings(github_token="bad")
        )

        assert result.items == []
        assert len(result.warnings) == 2
        assert any("GraphQL" in warning for warning in result.warnings)
        assert any("Search API" in warning for warning in result.warnings)


class TestFetchViaGhCli:
    async def test_gh_fallback_when_token_unset(
        self, github_graphql_response: dict, github_search_response: dict, make_settings, monkeypatch
    ) -> None:
        async def fake_graphql(query: str, variables: dict[str, str]) -> dict:
            assert variables["handle"] == HANDLE
            assert variables["start"] == "2026-08-01T00:00:00Z"
            assert variables["end"] == "2026-10-01T00:00:00Z"
            return github_graphql_response

        async def fake_search(query: str) -> dict:
            assert f"author:{HANDLE}" in query
            return github_search_response

        monkeypatch.setattr(github_module, "_run_gh_graphql", fake_graphql)
        monkeypatch.setattr(github_module, "_run_gh_search", fake_search)

        result = await fetch_github_activity(HANDLE, START_DATE, END_DATE, make_settings())

        assert result.strategy == "gh_cli_fallback"
        assert result.warnings == []
        assert len(result.items) == 8

    async def test_gh_missing_degrades_gracefully(self, make_settings, monkeypatch) -> None:
        async def boom(args: list[str]) -> dict:
            raise FileNotFoundError("gh executable not found")

        monkeypatch.setattr(github_module, "_run_gh", boom)

        result = await fetch_github_activity(HANDLE, START_DATE, END_DATE, make_settings())

        assert result.strategy == "gh_cli_fallback"
        assert result.items == []
        assert any("gh auth login" in warning for warning in result.warnings)
