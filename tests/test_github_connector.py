"""Unit tests for the GitHub connector (offline: respx mocks + monkeypatched gh)."""

from __future__ import annotations

from datetime import date

import httpx
import respx

from quarterly_pulse.connectors import github as github_module
from quarterly_pulse.connectors.github import (
    CONTRIBUTIONS_QUERY,
    _aggregate_by_repo,
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
        # Errors already explain the failure; no misleading "not found" on top.
        assert not any("not found" in warning for warning in warnings)

    def test_truncated_nodes_warn(self) -> None:
        warnings: list[str] = []
        payload = {
            "data": {
                "user": {
                    "contributionsCollection": {
                        "issueContributions": {"totalCount": 103, "nodes": []},
                    }
                }
            }
        }
        map_graphql_contributions(payload, HANDLE, warnings)
        assert any("truncated" in warning for warning in warnings)


class TestQueryShape:
    """Regression guards for GitHub GraphQL schema changes (e.g. ContributionOrder)."""

    def test_order_by_has_no_field_argument(self) -> None:
        assert "field: OCCURRED_AT" not in CONTRIBUTIONS_QUERY
        assert "orderBy: {direction: DESC}" in CONTRIBUTIONS_QUERY

    def test_cursor_variables_default_to_null(self) -> None:
        assert "$prAfter: String = null" in CONTRIBUTIONS_QUERY
        assert "$issueAfter: String = null" in CONTRIBUTIONS_QUERY
        assert "$reviewAfter: String = null" in CONTRIBUTIONS_QUERY

    def test_connections_expose_page_info(self) -> None:
        assert CONTRIBUTIONS_QUERY.count("pageInfo { hasNextPage endCursor }") == 3


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


class TestAggregateByRepo:
    def _pr(self, repo: str, number: int, merged: bool = True) -> ActivityItem:
        owner, name = repo.split("/")
        return ActivityItem(
            source="github",
            date=None,
            title=f"PR: change {number} ({repo})",
            summary="did a thing",
            impact="merged" if merged else "open",
            url=f"https://github.com/{owner}/{name}/pull/{number}",
        )

    def test_items_merge_into_one_per_repo(self) -> None:
        items = [
            self._pr("acme/platform", 1),
            self._pr("acme/platform", 2, merged=False),
            ActivityItem(
                source="github",
                title="Commits: acme/platform (27 commits)",
                impact="27 commits",
                url="https://github.com/acme/platform",
            ),
            self._pr("other/repo", 9),
        ]
        aggregated = _aggregate_by_repo(items)

        assert [item.title for item in aggregated] == ["GitHub: acme/platform", "GitHub: other/repo"]
        acme = aggregated[0]
        assert acme.impact == "2 pull requests (1 merged), 27 commits"
        assert acme.url == "https://github.com/acme/platform"
        assert "https://github.com/acme/platform/pull/1" in acme.raw_context
        assert "https://github.com/acme/platform/pull/2" in acme.raw_context
        assert "change 1" in acme.summary and "change 2" in acme.summary

    def test_repo_extracted_from_title_when_no_url(self) -> None:
        items = [ActivityItem(source="github", title="Issue: RFC thing (upstream/proj)")]
        aggregated = _aggregate_by_repo(items)
        assert [item.title for item in aggregated] == ["GitHub: upstream/proj"]
        assert aggregated[0].impact == "1 issue"

    def test_unrecognizable_items_stay_standalone(self) -> None:
        items = [ActivityItem(source="github", title="weird item", url=None)]
        assert _aggregate_by_repo(items) == items


class TestFetchViaHttpx:
    @respx.mock
    async def test_token_strategy_aggregates_per_repo(
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
        # One aggregated item per repository (GraphQL + Search, deduped by URL).
        assert [item.title for item in result.items] == [
            "GitHub: acme-internal/platform",
            "GitHub: kubernetes-sigs/kueue",
            "GitHub: quarterly-pulse/quarterly-pulse",
            "GitHub: upstream-project/fleet-operator",
        ]

        upstream = result.items[3]
        assert upstream.url == "https://github.com/upstream-project/fleet-operator"
        assert upstream.impact == "1 pull request (1 merged), 1 issue, 2 PR reviews"
        assert upstream.date == date(2026, 8, 20)  # latest activity in the group
        assert "https://github.com/upstream-project/fleet-operator/pull/42" in upstream.raw_context
        assert "https://github.com/upstream-project/fleet-operator/issues/100" in upstream.raw_context

        acme = result.items[0]
        assert acme.impact == "1 pull request, 27 commits, 1 PR review"

        auth = graphql_route.calls.last.request.headers["authorization"]
        assert auth == "Bearer ghp_test"
        body = graphql_route.calls.last.request.content.decode()
        assert "direction: DESC" in body
        assert "OCCURRED_AT" not in body
        assert "description" in body
        query = search_route.calls.last.request.url.params["q"]
        assert f"author:{HANDLE}" in query
        # Inclusive range syntax: repeated created: qualifiers get OR'd by GitHub.
        assert "created:2026-08-01..2026-09-30" in query

    @respx.mock
    async def test_out_of_window_items_dropped_client_side(self, make_settings) -> None:
        """Even if an API leaks pre-window items, the connector must drop them."""
        stale_payload = {
            "data": {
                "user": {
                    "login": HANDLE,
                    "contributionsCollection": {
                        "pullRequestContributions": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "occurredAt": "2024-05-22T00:00:00Z",
                                    "pullRequest": {
                                        "title": "[TOC Candidacy] Vincent Caldeira",
                                        "state": "CLOSED",
                                        "merged": False,
                                        "url": "https://github.com/finos/technical-oversight-committee/issues/1",
                                        "body": "",
                                        "repository": {
                                            "nameWithOwner": "finos/technical-oversight-committee",
                                            "url": "https://github.com/finos/technical-oversight-committee",
                                            "isPrivate": False,
                                            "description": "FINOS Technical Oversight Committee",
                                        },
                                    },
                                }
                            ],
                        },
                    },
                }
            }
        }
        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(200, json=stale_payload)
        )
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(200, json={"total_count": 0, "items": []})
        )

        result = await fetch_github_activity(
            HANDLE, START_DATE, END_DATE, make_settings(github_token="ghp_test")
        )

        assert result.items == []  # 2024 entry dropped despite being returned
        assert result.warnings == []

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


class TestSearchPagination:
    @respx.mock
    async def test_search_follows_pages_beyond_the_first(self, make_settings) -> None:
        def _search_item(index: int) -> dict:
            return {
                "title": f"item {index}",
                "html_url": f"https://github.com/org-{index // 100}/repo-{index}/pull/{index}",
                "state": "open",
                "created_at": "2026-08-15T00:00:00Z",
                "body": "",
                "repository_url": f"https://api.github.com/repos/org-{index // 100}/repo-{index}",
                "pull_request": {"merged_at": None},
            }

        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"user": {"login": HANDLE, "contributionsCollection": {}}}}
            )
        )
        respx.get("https://api.github.com/search/issues").mock(
            side_effect=[
                httpx.Response(200, json={"total_count": 103, "items": [_search_item(i) for i in range(100)]}),
                httpx.Response(200, json={"total_count": 103, "items": [_search_item(i) for i in range(100, 103)]}),
            ]
        )

        result = await fetch_github_activity(
            HANDLE, START_DATE, END_DATE, make_settings(github_token="ghp_test")
        )

        assert result.warnings == []
        assert len(result.items) == 103  # every page collected, one repo item each

    @respx.mock
    async def test_search_truncation_warns(self, make_settings) -> None:
        respx.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"user": {"login": HANDLE, "contributionsCollection": {}}}}
            )
        )
        respx.get("https://api.github.com/search/issues").mock(
            side_effect=[
                httpx.Response(200, json={"total_count": 1500, "items": []}),
            ]
        )

        result = await fetch_github_activity(
            HANDLE, START_DATE, END_DATE, make_settings(github_token="ghp_test")
        )

        assert any("truncated" in warning and "1500" in warning for warning in result.warnings)


class TestFetchViaGhCli:
    async def test_gh_fallback_when_token_unset(
        self, github_graphql_response: dict, github_search_response: dict, make_settings, monkeypatch
    ) -> None:
        async def fake_graphql(query: str, variables: dict[str, str | None]) -> dict:
            assert variables["handle"] == HANDLE
            assert variables["start"] == "2026-08-01T00:00:00Z"
            assert variables["end"] == "2026-10-01T00:00:00Z"
            assert variables.get("prAfter") is None  # page-one cursors are null
            return github_graphql_response

        async def fake_search(query: str, page: int = 1) -> dict:
            assert f"author:{HANDLE}" in query
            if page == 1:
                return github_search_response
            return {"total_count": github_search_response["total_count"], "items": []}

        monkeypatch.setattr(github_module, "_run_gh_graphql", fake_graphql)
        monkeypatch.setattr(github_module, "_run_gh_search", fake_search)

        result = await fetch_github_activity(HANDLE, START_DATE, END_DATE, make_settings())

        assert result.strategy == "gh_cli_fallback"
        assert result.warnings == []
        assert [item.title for item in result.items] == [
            "GitHub: acme-internal/platform",
            "GitHub: kubernetes-sigs/kueue",
            "GitHub: quarterly-pulse/quarterly-pulse",
            "GitHub: upstream-project/fleet-operator",
        ]

    async def test_gh_missing_degrades_gracefully(self, make_settings, monkeypatch) -> None:
        async def boom(args: list[str]) -> dict:
            raise FileNotFoundError("gh executable not found")

        monkeypatch.setattr(github_module, "_run_gh", boom)

        result = await fetch_github_activity(HANDLE, START_DATE, END_DATE, make_settings())

        assert result.strategy == "gh_cli_fallback"
        assert result.items == []
        assert any("gh auth login" in warning for warning in result.warnings)
