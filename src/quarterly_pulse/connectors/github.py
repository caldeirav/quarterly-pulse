"""Async GitHub connector.

Gathers a contributor's GitHub activity within a date window:

* ``contributionsCollection(from: $start, to: $end)`` via the GraphQL API,
  covering ``pullRequestContributions`` (title, repo, state/merged, body, URL),
  ``issueContributions`` (RFCs, design proposals, upstream issues),
  ``commitContributionsByRepository`` (commit counts per repo), and
  ``pullRequestReviewContributions`` — with cursor pagination when a
  collection exceeds one page; and
* the Search API (``author:<handle> created:>=<start-date>``) paged up to
  GitHub's 1,000-result cap, to catch external upstream PRs/issues across
  public orgs.

Results are **aggregated per repository** into one summary item per repo so
the report renders one row per repo (with the individual PR/issue URLs kept
in ``raw_context`` for citation and grounding).

When ``GITHUB_TOKEN`` is unset, both calls fall back to the authenticated
``gh`` CLI. Any failure is reported as a warning and degrades gracefully —
never raising to the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from quarterly_pulse.config import Settings
from quarterly_pulse.models import ActivityItem, ConnectorResult

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
SEARCH_URL = "https://api.github.com/search/issues"
_PER_PAGE = 100
_MAX_GRAPHQL_PAGES = 10
_MAX_SEARCH_PAGES = 10  # GitHub caps search at 1,000 results
_SUMMARY_CLIP = 280
_REPO_SUMMARY_CLIP = 600
_REPO_CONTEXT_CLIP = 1500

#: Cursor variables default to null so page-one callers can omit them
#: (important for the ``gh`` CLI, whose ``-f`` flags are strings only).
CONTRIBUTIONS_QUERY = """query ($handle: String!, $start: DateTime!, $end: DateTime!, $prAfter: String = null, $issueAfter: String = null, $reviewAfter: String = null) {
  user(login: $handle) {
    login
    contributionsCollection(from: $start, to: $end) {
      pullRequestContributions(first: 100, after: $prAfter, orderBy: {direction: DESC}) {
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
        pageInfo { hasNextPage endCursor }
      }
      issueContributions(first: 100, after: $issueAfter, orderBy: {direction: DESC}) {
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
        pageInfo { hasNextPage endCursor }
      }
      commitContributionsByRepository(maxRepositories: 50) {
        repository { nameWithOwner url isPrivate }
        contributions { totalCount }
      }
      pullRequestReviewContributions(first: 100, after: $reviewAfter, orderBy: {direction: DESC}) {
        totalCount
        nodes {
          occurredAt
          pullRequest { title url repository { nameWithOwner url } }
        }
        pageInfo { hasNextPage endCursor }
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
    """Gather GitHub activity for *handle* within ``[start_date, end_date]``, one item per repo."""
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
        total = search_payload.get("total_count", 0)
        fetched = len(search_payload.get("items") or [])
        if total > fetched:
            warnings.append(
                f"GitHub Search results truncated ({total} matching, {fetched} fetched; the API caps at 1,000)."
            )

    deduped = _dedupe_by_url(items)
    aggregated = _aggregate_by_repo(deduped)
    return ConnectorResult(source="github", strategy=strategy, items=aggregated, warnings=warnings)


# ---------------------------------------------------------------------------
# Transports (httpx and `gh` CLI), with pagination
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

        async def _run_query(variables: dict[str, str | None]) -> dict[str, Any]:
            response = await client.post(
                GRAPHQL_URL, json={"query": CONTRIBUTIONS_QUERY, "variables": variables}
            )
            response.raise_for_status()
            return response.json()

        try:
            graphql_payload = await _fetch_graphql_pages(
                _run_query, handle, start_date, end_date, warnings
            )
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            warnings.append(f"GitHub GraphQL query failed: {exc}")

        async def _fetch_page(page: int) -> dict[str, Any]:
            response = await client.get(
                SEARCH_URL,
                params={
                    "q": _search_query(handle, start_date, end_date),
                    "per_page": _PER_PAGE,
                    "page": page,
                    "sort": "created",
                    "order": "desc",
                },
            )
            response.raise_for_status()
            return response.json()

        try:
            search_payload = await _search_all_pages(_fetch_page)
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

    async def _run_query(variables: dict[str, str | None]) -> dict[str, Any]:
        return await _run_gh_graphql(CONTRIBUTIONS_QUERY, variables)

    try:
        graphql_payload = await _fetch_graphql_pages(_run_query, handle, start_date, end_date, warnings)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        warnings.append(
            f"GitHub `gh api graphql` fallback failed ({exc}); "
            "set GITHUB_TOKEN or run `gh auth login`."
        )

    async def _fetch_page(page: int) -> dict[str, Any]:
        return await _run_gh_search(_search_query(handle, start_date, end_date), page)

    try:
        search_payload = await _search_all_pages(_fetch_page)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        warnings.append(f"GitHub `gh api search/issues` fallback failed: {exc}")

    return graphql_payload, search_payload


async def _fetch_graphql_pages(
    run_query: Callable[[dict[str, str | None]], Awaitable[dict[str, Any]]],
    handle: str,
    start_date: date,
    end_date: date,
    warnings: list[str],
) -> dict[str, Any] | None:
    """Fetch contributionsCollection following cursor pagination; merge pages.

    Returns a single synthetic payload containing every node, the original
    payload when the user is missing (so :func:`map_graphql_contributions`
    can report it), or ``None`` on transport errors handled by the caller.
    """
    pr_nodes: list[dict[str, Any]] = []
    issue_nodes: list[dict[str, Any]] = []
    review_nodes: list[dict[str, Any]] = []
    commit_repos: list[dict[str, Any]] = []
    totals = {"prs": 0, "issues": 0, "reviews": 0}
    pr_after: str | None = None
    issue_after: str | None = None
    review_after: str | None = None

    for _page_index in range(_MAX_GRAPHQL_PAGES):
        payload = await run_query(
            {
                "handle": handle,
                "start": _gh_datetime(start_date),
                "end": _gh_datetime(end_date + timedelta(days=1)),
                "prAfter": pr_after,
                "issueAfter": issue_after,
                "reviewAfter": review_after,
            }
        )

        errors = payload.get("errors") or []
        for error in errors:
            warnings.append(f"GitHub GraphQL error: {error.get('message', error)}")
        user = (payload.get("data") or {}).get("user")
        if user is None:
            # Errors already explain the failure; avoid stacking a misleading
            # "user not found" warning on top of them.
            if errors and (pr_nodes or issue_nodes or review_nodes):
                break  # keep the partial data collected so far
            return payload

        collection = user.get("contributionsCollection") or {}

        prs = collection.get("pullRequestContributions") or {}
        totals["prs"] = prs.get("totalCount", totals["prs"])
        pr_nodes.extend(prs.get("nodes") or [])
        pr_after = _next_cursor(prs)

        issues = collection.get("issueContributions") or {}
        totals["issues"] = issues.get("totalCount", totals["issues"])
        issue_nodes.extend(issues.get("nodes") or [])
        issue_after = _next_cursor(issues)

        reviews = collection.get("pullRequestReviewContributions") or {}
        totals["reviews"] = reviews.get("totalCount", totals["reviews"])
        review_nodes.extend(reviews.get("nodes") or [])
        review_after = _next_cursor(reviews)

        commit_repos = collection.get("commitContributionsByRepository") or commit_repos

        if pr_after is None and issue_after is None and review_after is None:
            break

    return {
        "data": {
            "user": {
                "contributionsCollection": {
                    "pullRequestContributions": {"totalCount": totals["prs"], "nodes": pr_nodes},
                    "issueContributions": {"totalCount": totals["issues"], "nodes": issue_nodes},
                    "commitContributionsByRepository": commit_repos,
                    "pullRequestReviewContributions": {
                        "totalCount": totals["reviews"],
                        "nodes": review_nodes,
                    },
                }
            }
        }
    }


def _next_cursor(connection: dict[str, Any]) -> str | None:
    """The connection's endCursor when another page exists, else None."""
    page_info = connection.get("pageInfo") or {}
    return page_info.get("endCursor") if page_info.get("hasNextPage") else None


async def _search_all_pages(
    fetch_page: Callable[[int], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Page through the Search API (GitHub caps search at 1,000 results)."""
    items: list[dict[str, Any]] = []
    total_count = 0
    for page in range(1, _MAX_SEARCH_PAGES + 1):
        payload = await fetch_page(page)
        total_count = payload.get("total_count", 0)
        batch = payload.get("items") or []
        items.extend(batch)
        if len(batch) < _PER_PAGE:
            break
    return {"total_count": total_count, "items": items}


async def _run_gh_graphql(query: str, variables: dict[str, str | None]) -> dict[str, Any]:
    """Execute ``gh api graphql``; separated as a module function for testability."""
    args: list[str] = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        if value is not None:  # gh -f flags are strings; null cursors are simply omitted
            args += ["-f", f"{name}={value}"]
    return await _run_gh(args)


async def _run_gh_search(query: str, page: int = 1) -> dict[str, Any]:
    """Execute ``gh api search/issues``; separated as a module function for testability."""
    args: list[str] = [
        "gh", "api", "-X", "GET", "search/issues",
        "-f", f"q={query}", "-f", f"per_page={_PER_PAGE}", "-f", f"page={page}",
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
    """Map a GitHub GraphQL contributionsCollection response to per-activity ActivityItems."""
    if warnings is None:
        warnings = []
    errors = payload.get("errors") or []
    for error in errors:
        warnings.append(f"GitHub GraphQL error: {error.get('message', error)}")

    user = (payload.get("data") or {}).get("user")
    if user is None:
        if not errors:
            warnings.append(f"GitHub user '{handle}' not found (or token lacks access).")
        return []

    collection = user.get("contributionsCollection") or {}
    items: list[ActivityItem] = []

    prs = collection.get("pullRequestContributions") or {}
    if prs.get("totalCount", 0) > len(prs.get("nodes") or []):
        warnings.append(f"GitHub PR contributions truncated ({prs['totalCount']} total, {len(prs.get('nodes') or [])} fetched).")
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
    if issues.get("totalCount", 0) > len(issues.get("nodes") or []):
        warnings.append(f"GitHub issue contributions truncated ({issues['totalCount']} total, {len(issues.get('nodes') or [])} fetched).")
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
    if reviews.get("totalCount", 0) > len(reviews.get("nodes") or []):
        warnings.append(f"GitHub review contributions truncated ({reviews['totalCount']} total, {len(reviews.get('nodes') or [])} fetched).")
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


# ---------------------------------------------------------------------------
# Per-repository aggregation
# ---------------------------------------------------------------------------

_REPO_URL_RE = re.compile(r"https://github\.com/([^/\s?#]+)/([^/\s?#]+)")
_REPO_TITLE_RE = re.compile(r"\(([^()/]+/[^()/]+)\)\s*$")


def _aggregate_by_repo(items: list[ActivityItem]) -> list[ActivityItem]:
    """Collapse per-activity items into one summary item per GitHub repository."""
    groups: dict[str, list[ActivityItem]] = {}
    standalone: list[ActivityItem] = []
    for item in items:
        repo = _repo_of(item)
        if repo is None:
            standalone.append(item)
        else:
            groups.setdefault(repo, []).append(item)
    aggregated = [_merge_repo_group(repo, group) for repo, group in sorted(groups.items())]
    return aggregated + standalone


def _repo_of(item: ActivityItem) -> str | None:
    """Extract ``owner/repo`` from an item's GitHub URL or ``(owner/repo)`` title suffix."""
    if item.url:
        match = _REPO_URL_RE.match(item.url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    match = _REPO_TITLE_RE.search(item.title)
    return match.group(1) if match else None


def _merge_repo_group(repo: str, group: list[ActivityItem]) -> ActivityItem:
    """Merge one repository's per-activity items into a single summary item."""
    prs = [item for item in group if item.title.startswith("PR: ")]
    issues = [item for item in group if item.title.startswith("Issue: ")]
    commits = [item for item in group if item.title.startswith("Commits: ")]
    reviews = [item for item in group if item.title.startswith("PR reviews: ")]

    parts: list[str] = []
    if prs:
        merged = sum(1 for item in prs if (item.impact or "") == "merged")
        label = f"{len(prs)} pull request" + ("s" if len(prs) != 1 else "")
        if merged:
            label += f" ({merged} merged)"
        parts.append(label)
    if issues:
        parts.append(f"{len(issues)} issue" + ("s" if len(issues) != 1 else ""))
    commit_total = sum(_leading_count(item.impact) for item in commits)
    if commit_total:
        parts.append(f"{commit_total} commits")
    review_total = sum(_leading_count(item.impact) for item in reviews)
    if review_total:
        parts.append(f"{review_total} PR review" + ("s" if review_total != 1 else ""))

    highlights: list[str] = []
    for item in (*prs, *issues):
        clean = item.title.split(": ", 1)[-1]
        if clean.endswith(")") and " (" in clean:
            clean = clean.rsplit(" (", 1)[0]
        highlights.append(clean)

    dates = [item.date for item in group if item.date is not None]
    urls = list(dict.fromkeys(item.url for item in group if item.url))
    repo_url = f"https://github.com/{repo}"

    return ActivityItem(
        source="github",
        date=max(dates) if dates else None,
        title=f"GitHub: {repo}",
        summary=_clip("; ".join(highlights), _REPO_SUMMARY_CLIP),
        impact=", ".join(parts),
        url=repo_url,
        raw_context=_clip(f"repo: {repo_url}; " + "; ".join(urls), _REPO_CONTEXT_CLIP),
    )


def _leading_count(text: str | None) -> int:
    """First integer embedded in a string like ``27 commits`` or ``3 pull requests reviewed``."""
    match = re.search(r"(\d+)", text or "")
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


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
