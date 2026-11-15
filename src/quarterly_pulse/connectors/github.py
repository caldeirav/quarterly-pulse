"""Async GitHub connector.

Gathers a contributor's GitHub activity within a date window:

* ``contributionsCollection(from: $start, to: $end)`` via the GraphQL API,
  covering ``pullRequestContributions`` (title, repo, state/merged, body, URL),
  ``issueContributions`` (RFCs, design proposals, upstream issues),
  ``commitContributionsByRepository`` (commit counts per repo), and
  ``pullRequestReviewContributions``; and
* the Search API (``author:<handle> created:>=<start-date>``) to catch
  external upstream PRs/issues across public orgs.

When ``GITHUB_TOKEN`` is unset, both calls fall back to the authenticated
``gh`` CLI (``gh api graphql`` / ``gh api search/issues``). Any failure is
reported as a warning and degrades gracefully — never raising to the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from quarterly_pulse.config import Settings
from quarterly_pulse.models import ActivityItem, ConnectorResult

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
SEARCH_URL = "https://api.github.com/search/issues"
_PER_PAGE = 100
_BODY_CLIP = 400
_SUMMARY_CLIP = 280

CONTRIBUTIONS_QUERY = """query ($handle: String!, $start: DateTime!, $end: DateTime!) {
  user(login: $handle) {
    login
    contributionsCollection(from: $start, to: $end) {
      pullRequestContributions(first: 100, orderBy: {field: OCCURRED_AT, direction: DESC}) {
        totalCount
        nodes {
          occurredAt
          pullRequest {
            title
            state
            merged
            url
            body
            repository { nameWithOwner url isPrivate }
          }
        }
      }
      issueContributions(first: 100, orderBy: {field: OCCURRED_AT, direction: DESC}) {
        totalCount
        nodes {
          occurredAt
          issue {
            title
            state
            url
            body
            repository { nameWithOwner url }
          }
        }
      }
      commitContributionsByRepository(maxRepositories: 50) {
        repository { nameWithOwner url isPrivate }
        contributions { totalCount }
      }
      pullRequestReviewContributions(first: 100, orderBy: {field: OCCURRED_AT, direction: DESC}) {
        totalCount
        nodes {
          occurredAt
          pullRequest { title url repository { nameWithOwner url } }
        }
      }
    }
  }
}
"""


async def fetch_github_activity(
    handle: str,
    start_date: date,
    end_date: date,
    settings: Settings,
) -> ConnectorResult:
    """Gather GitHub activity for *handle* within ``[start_date, end_date]``."""
    warnings: list[str] = []
    items: list[ActivityItem] = []
    strategy = "unconfigured"

    token = settings.github_token
    if token:
        strategy = "graphql_api + search_api"
        graphql_payload, search_payload = await _fetch_via_httpx(token, handle, start_date, end_date, warnings)
    else:
        strategy = "gh_cli_fallback"
        graphql_payload, search_payload = await _fetch_via_gh(handle, start_date, end_date, warnings)

    if graphql_payload is not None:
        items.extend(map_graphql_contributions(graphql_payload, handle, warnings))
    if search_payload is not None:
        items.extend(map_search_results(search_payload))

    deduped = _dedupe_by_url(items)
    return ConnectorResult(source="github", strategy=strategy, items=deduped, warnings=warnings)


# ---------------------------------------------------------------------------
# HTTP transports
# ---------------------------------------------------------------------------


async def _fetch_via_httpx(
    token: str,
    handle: str,
    start_date: date,
    end_date: date,
    warnings: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch GraphQL contributions and Search results via httpx."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    graphql_payload: dict[str, Any] | None = None
    search_payload: dict[str, Any] | None = None
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        try:
            response = await client.post(
                GRAPHQL_URL,
                json={
                    "query": CONTRIBUTIONS_QUERY,
                    "variables": {
                        "handle": handle,
                        "start": _gh_datetime(start_date),
                        "end": _gh_datetime(end_date + timedelta(days=1)),
                    },
                },
            )
            response.raise_for_status()
            graphql_payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            warnings.append(f"GitHub GraphQL query failed: {exc}")

        try:
            response = await client.get(
                SEARCH_URL,
                params={
                    "q": _search_query(handle, start_date, end_date),
                    "per_page": _PER_PAGE,
                    "sort": "created",
                    "order": "desc",
                },
            )
            response.raise_for_status()
            search_payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            warnings.append(f"GitHub Search API query failed: {exc}")

    return graphql_payload, search_payload


async def _fetch_via_gh(
    handle: str,
    start_date: date,
    end_date: date,
    warnings: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch the same data through the authenticated ``gh`` CLI."""
    graphql_payload: dict[str, Any] | None = None
    search_payload: dict[str, Any] | None = None
    try:
        graphql_payload = await _run_gh_graphql(
            CONTRIBUTIONS_QUERY,
            {
                "handle": handle,
                "start": _gh_datetime(start_date),
                "end": _gh_datetime(end_date + timedelta(days=1)),
            },
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        warnings.append(
            f"GitHub `gh api graphql` fallback failed ({exc}); "
            "set GITHUB_TOKEN or run `gh auth login`."
        )

    try:
        search_payload = await _run_gh_search(_search_query(handle, start_date, end_date))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        warnings.append(f"GitHub `gh api search/issues` fallback failed: {exc}")

    return graphql_payload, search_payload


async def _run_gh_graphql(query: str, variables: dict[str, str]) -> dict[str, Any]:
    """Execute ``gh api graphql``; separated as a module function for testability."""
    args: list[str] = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    return await _run_gh(args)


async def _run_gh_search(query: str) -> dict[str, Any]:
    """Execute ``gh api search/issues``; separated as a module function for testability."""
    args: list[str] = [
        "gh", "api", "-X", "GET", "search/issues",
        "-f", f"q={query}", "-f", f"per_page={_PER_PAGE}",
    ]
    return await _run_gh(args)


async def _run_gh(args: list[str]) -> dict[str, Any]:
    """Run a ``gh`` subprocess and return its parsed JSON output."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or f"gh exited {proc.returncode}")
    return json.loads(stdout.decode())


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------


def map_graphql_contributions(
    payload: dict[str, Any], handle: str, warnings: list[str] | None = None
) -> list[ActivityItem]:
    """Map a GitHub GraphQL contributionsCollection response to ActivityItems."""
    if warnings is None:
        warnings = []
    for error in payload.get("errors", []) or []:
        warnings.append(f"GitHub GraphQL error: {error.get('message', error)}")

    user = (payload.get("data") or {}).get("user")
    if user is None:
        warnings.append(f"GitHub user '{handle}' not found (or token lacks access).")
        return []

    collection = user.get("contributionsCollection") or {}
    items: list[ActivityItem] = []

    prs = collection.get("pullRequestContributions") or {}
    if prs.get("totalCount", 0) > _PER_PAGE:
        warnings.append(f"GitHub PR contributions truncated ({prs['totalCount']} > {_PER_PAGE} fetched).")
    for node in prs.get("nodes") or []:
        pr = node.get("pullRequest") or {}
        repo = (pr.get("repository") or {}).get("nameWithOwner", "unknown/repo")
        items.append(
            ActivityItem(
                source="github",
                date=_parse_day(node.get("occurredAt")),
                title=f"PR: {pr.get('title', 'untitled')} ({repo})",
                summary=_clip(pr.get("body") or "", _SUMMARY_CLIP),
                impact="merged" if pr.get("merged") else str(pr.get("state", "unknown")).lower(),
                url=pr.get("url"),
                raw_context=f"pull request by {handle} in {repo}: {pr.get('title', '')}",
            )
        )

    issues = collection.get("issueContributions") or {}
    if issues.get("totalCount", 0) > _PER_PAGE:
        warnings.append(f"GitHub issue contributions truncated ({issues['totalCount']} > {_PER_PAGE} fetched).")
    for node in issues.get("nodes") or []:
        issue = node.get("issue") or {}
        repo = (issue.get("repository") or {}).get("nameWithOwner", "unknown/repo")
        items.append(
            ActivityItem(
                source="github",
                date=_parse_day(node.get("occurredAt")),
                title=f"Issue: {issue.get('title', 'untitled')} ({repo})",
                summary=_clip(issue.get("body") or "", _SUMMARY_CLIP),
                impact=str(issue.get("state", "unknown")).lower(),
                url=issue.get("url"),
                raw_context=f"issue filed by {handle} in {repo}: {issue.get('title', '')}",
            )
        )

    for entry in collection.get("commitContributionsByRepository") or []:
        repo = entry.get("repository") or {}
        count = (entry.get("contributions") or {}).get("totalCount", 0)
        if count <= 0:
            continue
        items.append(
            ActivityItem(
                source="github",
                date=None,
                title=f"Commits: {repo.get('nameWithOwner', 'unknown/repo')} ({count} commits)",
                summary=f"{count} commits contributed during the reporting window.",
                impact=f"{count} commits",
                url=repo.get("url"),
                raw_context=f"commits by {handle} in {repo.get('nameWithOwner', '')}",
            )
        )

    reviews = collection.get("pullRequestReviewContributions") or {}
    if reviews.get("totalCount", 0) > _PER_PAGE:
        warnings.append(f"GitHub review contributions truncated ({reviews['totalCount']} > {_PER_PAGE} fetched).")
    by_repo: dict[str, dict[str, Any]] = {}
    for node in reviews.get("nodes") or []:
        pr = node.get("pullRequest") or {}
        repo = (pr.get("repository") or {}).get("nameWithOwner", "unknown/repo")
        bucket = by_repo.setdefault(repo, {"titles": [], "url": (pr.get("repository") or {}).get("url")})
        bucket["titles"].append(pr.get("title", "untitled"))
    for repo, bucket in sorted(by_repo.items()):
        titles = bucket["titles"]
        items.append(
            ActivityItem(
                source="github",
                date=None,
                title=f"PR reviews: {repo} ({len(titles)} reviews)",
                summary=_clip("; ".join(titles), _SUMMARY_CLIP),
                impact=f"{len(titles)} pull requests reviewed",
                url=bucket.get("url"),
                raw_context=f"pull request reviews by {handle} in {repo}",
            )
        )

    return items


def map_search_results(payload: dict[str, Any]) -> list[ActivityItem]:
    """Map a GitHub Search API (issues endpoint) response to ActivityItems."""
    items: list[ActivityItem] = []
    for entry in payload.get("items") or []:
        repo = _repo_from_api_url(entry.get("repository_url", ""))
        is_pr = "pull_request" in entry
        kind = "PR" if is_pr else "Issue"
        merged_at = (entry.get("pull_request") or {}).get("merged_at")
        items.append(
            ActivityItem(
                source="github",
                date=_parse_day(entry.get("created_at")),
                title=f"{kind}: {entry.get('title', 'untitled')} ({repo})",
                summary=_clip(entry.get("body") or "", _SUMMARY_CLIP),
                impact="merged" if merged_at else str(entry.get("state", "unknown")).lower(),
                url=entry.get("html_url"),
                raw_context=f"{kind.lower()} by author in {repo} (public search): {entry.get('title', '')}",
            )
        )
    return items


def _gh_datetime(day: date) -> str:
    """Format a date as a GitHub GraphQL DateTime (UTC midnight, ISO8601)."""
    return f"{day.isoformat()}T00:00:00Z"


def _search_query(handle: str, start_date: date, end_date: date) -> str:
    """Build the Search API qualifier: author + creation window."""
    end_exclusive = (end_date + timedelta(days=1)).isoformat()
    return f"author:{handle} created:>={start_date.isoformat()} created:<{end_exclusive}"


def _repo_from_api_url(url: str) -> str:
    """Extract ``owner/repo`` from a Search API repository_url."""
    if "/repos/" in url:
        return url.rsplit("/repos/", 1)[-1]
    return url


def _parse_day(value: Any) -> date | None:
    """Parse an ISO-8601 timestamp (e.g. ``2026-08-05T10:00:00Z``) to a date."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _clip(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters on a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _dedupe_by_url(items: list[ActivityItem]) -> list[ActivityItem]:
    """Drop duplicates sharing the same URL and item kind.

    The item kind (the ``PR:``/``Issue:``/``Commits:``/``PR reviews:`` title
    prefix) is part of the key so per-repo commit and review aggregates for
    the same repository do not collapse into each other, while the same PR or
    issue found via both GraphQL and the Search API still dedupes.
    """
    seen: set[str] = set()
    unique: list[ActivityItem] = []
    for item in items:
        kind = item.title.split(":", 1)[0] if ":" in item.title else ""
        if item.url:
            key = f"{kind}|{item.url}"
        else:
            key = f"{item.source}|{item.title}|{item.date}|{item.raw_context}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
