"""Async LinkedIn connector with a prioritized fallback chain.

Strategies, in order:

a. **LinkedIn Member Data Portability API** — when ``LINKEDIN_ACCESS_TOKEN``
   is set, calls ``GET https://api.linkedin.com/rest/memberSnapshotData``
   (with the ``Linkedin-Version`` header) for ``SHARE_HISTORY`` /
   ``ARTICLES`` / ``COMMENTS`` snapshot types, filtered by the start date.
b. **Local LinkedIn export archive** — a LinkedIn data export folder or
   ``.zip`` inside ``--docs-dir`` (``Shares.csv``, ``Articles/*.html``,
   ``Positions.csv``), parsed with the same schema mapper as strategy (a).
c. **Third-party public post APIs** — an Apify LinkedIn profile-posts Actor
   (``APIFY_API_TOKEN``), else the ScrapeCreators profile/posts endpoint
   (``SCRAPECREATORS_API_KEY``), else an Exa ``site:linkedin.com`` search
   (``EXA_API_KEY``).
d. **Graceful degradation** — a clear warning explaining which env var or
   export file to add; the pipeline continues without failing.

Deprecated APIs (e.g. Proxycurl) are deliberately not used.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from quarterly_pulse.config import Settings
from quarterly_pulse.models import ActivityItem, ConnectorResult

logger = logging.getLogger(__name__)

PORTABILITY_URL = "https://api.linkedin.com/rest/memberSnapshotData"
LINKEDIN_VERSION = "202411"
SNAPSHOT_TYPES = ("SHARE_HISTORY", "ARTICLES", "COMMENTS")

#: Default Apify Actor for LinkedIn profile posts (no cookies required).
#: Overridable via ``APIFY_LINKEDIN_ACTOR`` because Store actors come and go.
DEFAULT_APIFY_ACTOR = "apimaestro/linkedin-profile-posts"
SCRAPECREATORS_POSTS_URL = "https://api.scrapecreators.com/api/v1/linkedin/profile/posts"
EXA_SEARCH_URL = "https://api.exa.ai/search"

_EXPORT_DIR_HINTS = ("linkedin",)
_EXPORT_MARKER_FILES = ("Shares.csv", "Positions.csv", "Connections.csv")
_TEXT_CLIP = 900
#: Max length for a post's extracted title sentence.
_TITLE_CLIP = 140


class ShareRecord(BaseModel):
    """Normalized LinkedIn post/article/comment record shared by all strategies."""

    kind: str  # "post" | "article" | "comment" | "position"
    occurred_at: str | int | None = None
    title: str | None = None
    text: str = ""
    url: str | None = None
    visibility: str | None = None


async def fetch_linkedin_activity(
    profile_url: str | None,
    contributor_name: str | None,
    start_date: date,
    end_date: date,
    docs_dir: Path,
    settings: Settings,
) -> ConnectorResult:
    """Gather LinkedIn activity using the prioritized fallback chain."""
    warnings: list[str] = []

    # a) Member Data Portability API
    if settings.linkedin_access_token:
        try:
            records = await _fetch_via_portability_api(settings.linkedin_access_token)
            items = _records_to_items(records, start_date, end_date)
            return ConnectorResult(
                source="linkedin", strategy="member_data_portability_api", items=items, warnings=warnings
            )
        except Exception as exc:  # secondary source: degrade, never crash the pipeline
            warnings.append(f"LinkedIn Member Data Portability API failed ({exc}); trying next strategy.")
    else:
        warnings.append(
            "LinkedIn Member Data Portability API skipped: LINKEDIN_ACCESS_TOKEN is not set."
        )

    # b) Local export archive
    archive = find_linkedin_export(docs_dir)
    if archive is not None:
        try:
            records = await _fetch_from_export_archive(archive)
            items = _records_to_items(records, start_date, end_date)
            return ConnectorResult(
                source="linkedin", strategy="local_export_archive", items=items, warnings=warnings
            )
        except Exception as exc:  # secondary source: degrade, never crash the pipeline
            warnings.append(f"LinkedIn export archive parsing failed ({exc}); trying next strategy.")
    else:
        warnings.append(
            "No LinkedIn export archive found in --docs-dir "
            "(expected Shares.csv / Articles/ / Positions.csv or a LinkedIn .zip)."
        )

    # c) Third-party public post APIs
    if settings.apify_api_token and profile_url:
        try:
            records = await _fetch_via_apify(settings, profile_url)
            items = _records_to_items(records, start_date, end_date)
            return ConnectorResult(
                source="linkedin", strategy="apify_actor", items=items, warnings=warnings
            )
        except Exception as exc:  # secondary source: degrade, never crash the pipeline
            warnings.append(f"Apify LinkedIn scraper failed ({exc}); trying next strategy.")
    if settings.scrapecreators_api_key and profile_url:
        try:
            records = await _fetch_via_scrapecreators(settings.scrapecreators_api_key, profile_url)
            items = _records_to_items(records, start_date, end_date)
            return ConnectorResult(
                source="linkedin", strategy="scrapecreators_api", items=items, warnings=warnings
            )
        except Exception as exc:  # secondary source: degrade, never crash the pipeline
            warnings.append(f"ScrapeCreators LinkedIn endpoint failed ({exc}); trying next strategy.")
    if settings.exa_api_key and (contributor_name or profile_url):
        try:
            records = await _fetch_via_exa(
                settings.exa_api_key, contributor_name, profile_url, start_date, end_date
            )
            items = _records_to_items(records, start_date, end_date)
            return ConnectorResult(
                source="linkedin", strategy="exa_search", items=items, warnings=warnings
            )
        except Exception as exc:  # secondary source: degrade, never crash the pipeline
            warnings.append(f"Exa LinkedIn search failed ({exc}); trying next strategy.")

    # d) Graceful degradation
    warnings.append(
        "LinkedIn data unavailable: set LINKEDIN_ACCESS_TOKEN, add a LinkedIn data export to "
        "--docs-dir, or set APIFY_API_TOKEN / SCRAPECREATORS_API_KEY / EXA_API_KEY. "
        "Continuing without LinkedIn activity."
    )
    return ConnectorResult(source="linkedin", strategy="unconfigured", items=[], warnings=warnings)


# ---------------------------------------------------------------------------
# Strategy (a): Member Data Portability API
# ---------------------------------------------------------------------------


async def _fetch_via_portability_api(token: str) -> list[ShareRecord]:
    """Fetch SHARE_HISTORY / ARTICLES / COMMENTS snapshots and normalize them.

    Assumes the Member Data Portability API element schema::

        {"elements": [{"snapshotType": ..., "createdAt": ...,
                       "shareMetadata" | "articleMetadata" | "commentMetadata": {...}}]}
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    elements: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        for snapshot_type in SNAPSHOT_TYPES:
            response = await client.get(PORTABILITY_URL, params={"snapshotType": snapshot_type})
            response.raise_for_status()
            payload = response.json()
            # The API returns elements for the requested snapshot type; filter
            # defensively in case the response includes other types.
            elements.extend(
                element
                for element in payload.get("elements", []) or []
                if element.get("snapshotType") in (None, snapshot_type)
            )
    return _normalize_portability_elements(elements)


def _normalize_portability_elements(elements: list[dict[str, Any]]) -> list[ShareRecord]:
    """Normalize Portability API elements into :class:`ShareRecord` objects."""
    records: list[ShareRecord] = []
    for element in elements:
        snapshot_type = element.get("snapshotType")
        if snapshot_type == "SHARE_HISTORY":
            meta = element.get("shareMetadata") or {}
            records.append(
                ShareRecord(
                    kind="post",
                    occurred_at=element.get("createdAt"),
                    text=meta.get("commentary") or "",
                    url=meta.get("shareUrl"),
                    visibility=meta.get("visibility"),
                )
            )
        elif snapshot_type == "ARTICLES":
            meta = element.get("articleMetadata") or {}
            records.append(
                ShareRecord(
                    kind="article",
                    occurred_at=element.get("createdAt"),
                    title=meta.get("title"),
                    text=meta.get("content") or "",
                    url=meta.get("url"),
                )
            )
        elif snapshot_type == "COMMENTS":
            meta = element.get("commentMetadata") or {}
            records.append(
                ShareRecord(
                    kind="comment",
                    occurred_at=element.get("createdAt"),
                    text=meta.get("text") or "",
                    url=meta.get("parentPostUrl"),
                )
            )
    return records


# ---------------------------------------------------------------------------
# Strategy (b): local export archive
# ---------------------------------------------------------------------------


def find_linkedin_export(docs_dir: Path) -> Path | None:
    """Locate a LinkedIn data export (directory or ``.zip``) inside *docs_dir*.

    Returns the export directory, the export zip path, or ``docs_dir`` itself
    when loose export marker files (``Shares.csv`` etc.) sit directly inside
    it. Returns ``None`` when no export is present.
    """
    for child in sorted(docs_dir.rglob("*")):
        if not child.is_dir():
            continue
        if _looks_like_export_dir(child):
            return child

    for child in sorted(docs_dir.rglob("*.zip")):
        if child.name.lower().startswith(_EXPORT_DIR_HINTS) or _zip_has_export_markers(child):
            return child

    for marker in ("Shares.csv", "Positions.csv"):
        if (docs_dir / marker).is_file():
            return docs_dir
    return None


def linkedin_exclusions(archive: Path | None, docs_dir: Path) -> set[Path]:
    """Filesystem paths the docs connector must skip because LinkedIn owns them."""
    if archive is None:
        return set()
    if archive == docs_dir:
        exclusions = {
            docs_dir / marker for marker in _EXPORT_MARKER_FILES if (docs_dir / marker).is_file()
        }
        articles = docs_dir / "Articles"
        if articles.is_dir():
            exclusions.add(articles)
        return exclusions
    return {archive}


def _looks_like_export_dir(path: Path) -> bool:
    """A directory is an export when it holds export markers, or is LinkedIn-named with an Articles/ dir."""
    if any((path / marker).is_file() for marker in ("Shares.csv", "Positions.csv", "Connections.csv")):
        return True
    return "linkedin" in path.name.lower() and (path / "Articles").is_dir()


def _zip_has_export_members(members: list[str]) -> bool:
    names = {name.rsplit("/", 1)[-1] for name in members}
    return bool(names & {"Shares.csv", "Positions.csv"})


def _zip_has_export_markers(zip_path: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            return _zip_has_export_members(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


async def _fetch_from_export_archive(archive: Path) -> list[ShareRecord]:
    """Parse Shares.csv / Articles/*.html / Positions.csv from an export archive.

    Uses the same :class:`ShareRecord` schema mapper as the Portability API
    strategy, then applies the shared date filter.
    """
    records: list[ShareRecord] = []

    shares_csv = _read_export_member(archive, "Shares.csv")
    if shares_csv is not None:
        records.extend(_parse_shares_csv(shares_csv))

    positions_csv = _read_export_member(archive, "Positions.csv")
    if positions_csv is not None:
        records.extend(_parse_positions_csv(positions_csv))

    for name, content, modified in _iter_export_articles(archive):
        records.append(_parse_article_html(name, content, modified))

    return records


def _read_export_member(archive: Path, member_name: str) -> str | None:
    """Read a named member from a zip archive or directory export."""
    if archive.is_file():
        with zipfile.ZipFile(archive) as zip_archive:
            for name in zip_archive.namelist():
                if name.rsplit("/", 1)[-1] == member_name:
                    return zip_archive.read(name).decode("utf-8", errors="replace")
        return None
    member = archive / member_name
    if member.is_file():
        return member.read_text(encoding="utf-8", errors="replace")
    return None


def _iter_export_articles(archive: Path) -> list[tuple[str, str, str | None]]:
    """Yield ``(name, html, last_modified_iso)`` for every Articles/*.html member."""
    articles: list[tuple[str, str, str | None]] = []
    if archive.is_file():
        with zipfile.ZipFile(archive) as zip_archive:
            for info in zip_archive.infolist():
                if "/Articles/" in f"/{info.filename}" and info.filename.lower().endswith(".html"):
                    content = zip_archive.read(info.filename).decode("utf-8", errors="replace")
                    try:
                        modified = datetime(*info.date_time, tzinfo=timezone.utc).isoformat()
                    except ValueError:
                        modified = None
                    articles.append((info.filename, content, modified))
        return articles
    articles_dir = archive / "Articles"
    if not articles_dir.is_dir():
        return articles
    for html_file in sorted(articles_dir.glob("*.html")):
        modified = datetime.fromtimestamp(html_file.stat().st_mtime, tz=timezone.utc).isoformat()
        articles.append((html_file.name, html_file.read_text(encoding="utf-8", errors="replace"), modified))
    return articles


def _parse_shares_csv(text: str) -> list[ShareRecord]:
    """Map export ``Shares.csv`` rows to :class:`ShareRecord` via the shared schema."""
    records: list[ShareRecord] = []
    for row in csv.DictReader(io.StringIO(text)):
        records.append(
            ShareRecord(
                kind="post",
                occurred_at=_pick_column(row, "Date"),
                text=_pick_column(row, "Share Commentary", "Commentary", "Description", "Text"),
                url=_pick_column(row, "Shared Link (URL)", "Shared Link", "Link", "URL"),
                visibility=_pick_column(row, "Visibility", "Share Visibility"),
            )
        )
    return records


def _parse_positions_csv(text: str) -> list[ShareRecord]:
    """Map export ``Positions.csv`` rows to :class:`ShareRecord` objects."""
    records: list[ShareRecord] = []
    for row in csv.DictReader(io.StringIO(text)):
        started = _pick_column(row, "Started On")
        records.append(
            ShareRecord(
                kind="position",
                occurred_at=started,
                title=_pick_column(row, "Title"),
                text=f"{_pick_column(row, 'Title')} at {_pick_column(row, 'Company Name')}",
            )
        )
    return records


def _parse_article_html(name: str, html: str, modified_iso: str | None) -> ShareRecord:
    """Extract title and text from an exported article HTML file."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = _strip_tags(title_match.group(1)) if title_match else Path(name).stem
    text = _strip_tags(re.sub(r"<[^>]+>", " ", html))
    return ShareRecord(kind="article", occurred_at=modified_iso, title=title, text=text)


def _pick_column(row: dict[str, str | None], *candidates: str) -> str:
    """Pick the first non-empty CSV column matching any candidate name."""
    normalized = {
        (key or "").strip().lower(): (value or "").strip() for key, value in row.items()
    }
    for candidate in candidates:
        value = normalized.get(candidate.lower(), "")
        if value:
            return value
    return ""


def _strip_tags(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())


# ---------------------------------------------------------------------------
# Strategy (c): third-party public post APIs
# ---------------------------------------------------------------------------


async def _fetch_via_apify(settings: Settings, profile_url: str) -> list[ShareRecord]:
    """Run a LinkedIn profile-posts scraper Actor via ``apify-client``."""
    run_input = _apify_run_input(profile_url, settings.apify_linkedin_input)
    raw_items = await asyncio.to_thread(
        _run_apify_actor, settings.apify_api_token, settings.apify_linkedin_actor, run_input
    )
    records: list[ShareRecord] = []
    for post in _apify_post_items(raw_items):
        records.append(
            ShareRecord(
                kind="post",
                occurred_at=_apify_post_date(post),
                text=_first_key(post, "text", "commentary", "content") or "",
                url=_first_key(post, "url", "postUrl", "post_url"),
            )
        )
    return records


def _apify_run_input(profile_url: str, override: str | None) -> dict[str, Any]:
    """Build the Actor run input.

    The default targets ``apimaestro/linkedin-profile-posts`` (username-based,
    no cookies). ``APIFY_LINKEDIN_INPUT`` overrides it with raw JSON supporting
    ``{username}`` and ``{profile_url}`` placeholders, for Actors with
    different schemas.
    """
    if override:
        rendered = override.replace("{username}", _handle_from_profile_url(profile_url)).replace(
            "{profile_url}", profile_url
        )
        parsed: dict[str, Any] = json.loads(rendered)
        return parsed
    return {"username": _handle_from_profile_url(profile_url), "page_number": 1, "limit": 100}


def _apify_post_items(raw_items: list[Any]) -> list[dict[str, Any]]:
    """Normalize Apify dataset items: unwrap response wrappers, keep post objects.

    Handles both shapes seen in the wild: one post per dataset item, and the
    wrapped ``{"data": {"posts": [...]}}`` response objects.
    """
    posts: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        wrapped = item.get("data") if isinstance(item.get("data"), dict) else item
        inner = wrapped.get("posts")
        if isinstance(inner, list):
            posts.extend(post for post in inner if isinstance(post, dict))
        elif any(key in wrapped for key in ("text", "urn", "url")):
            posts.append(wrapped)
    return posts


def _apify_post_date(post: dict[str, Any]) -> str | int | None:
    """Extract a post date, handling the nested ``posted_at`` object shape."""
    posted_at = post.get("posted_at")
    if isinstance(posted_at, dict):
        return posted_at.get("date") or posted_at.get("timestamp")
    return _first_key(post, "createdAt", "postedAt", "publishedAt", "date", "timestamp")


def _run_apify_actor(token: str, actor_id: str, run_input: dict[str, Any]) -> list[Any]:
    """Synchronous Apify Actor run; separated as a module function for testability."""
    from apify_client import ApifyClient  # lazy import; optional dependency at runtime

    client = ApifyClient(token)
    run = client.actor(actor_id).call(run_input=run_input)
    if run is None:
        return []
    dataset_id = _run_dataset_id(run)
    if dataset_id is None:
        return []
    return list(client.dataset(dataset_id).iterate_items())


def _run_dataset_id(run: Any) -> str | None:
    """Extract the default dataset id from a run, tolerating key/attribute styles."""
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    return getattr(run, "defaultDatasetId", None) or getattr(run, "default_dataset_id", None)


async def _fetch_via_scrapecreators(api_key: str, profile_url: str) -> list[ShareRecord]:
    """Call the ScrapeCreators LinkedIn profile/posts endpoint."""
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        response = await client.get(SCRAPECREATORS_POSTS_URL, params={"profileUrl": profile_url})
        response.raise_for_status()
        payload = response.json()

    posts = payload.get("data") or payload.get("posts") or payload.get("results") or []
    records: list[ShareRecord] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        records.append(
            ShareRecord(
                kind="post",
                occurred_at=_first_key(post, "createdAt", "postedAt", "publishedAt"),
                text=_first_key(post, "text", "commentary", "content"),
                url=_first_key(post, "url", "postUrl", "post_url"),
            )
        )
    return records


async def _fetch_via_exa(
    api_key: str,
    contributor_name: str | None,
    profile_url: str | None,
    start_date: date,
    end_date: date,
) -> list[ShareRecord]:
    """Search Exa for ``site:linkedin.com`` posts/articles mentioning the contributor."""
    subject = contributor_name or _handle_from_profile_url(profile_url or "")
    body = {
        "query": f"site:linkedin.com {subject}",
        "type": "keyword",
        "numResults": 20,
        "startPublishedDate": start_date.isoformat(),
        "endPublishedDate": end_date.isoformat(),
    }
    async with httpx.AsyncClient(headers={"x-api-key": api_key}, timeout=30.0) as client:
        response = await client.post(EXA_SEARCH_URL, json=body)
        response.raise_for_status()
        payload = response.json()

    records: list[ShareRecord] = []
    for result in payload.get("results") or []:
        records.append(
            ShareRecord(
                kind="article",
                occurred_at=result.get("publishedDate"),
                title=result.get("title"),
                text=_first_key(result, "text", "summary", ""),
                url=result.get("url"),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Shared schema mapper
# ---------------------------------------------------------------------------


def _records_to_items(records: list[ShareRecord], start_date: date, end_date: date) -> list[ActivityItem]:
    """Map :class:`ShareRecord` objects to filtered ActivityItems (shared by all strategies)."""
    items: list[ActivityItem] = []
    for record in records:
        occurred = _parse_linkedin_datetime(record.occurred_at)
        if occurred is not None and not (start_date <= occurred <= end_date):
            continue
        # Keep the post's own headline as the title (no "LinkedIn" prefix);
        # the longer summary gives the synthesizer enough text to describe the
        # post and connect it to any event presentation it coincides with.
        label = record.title or _first_sentence(record.text) or "LinkedIn activity"
        items.append(
            ActivityItem(
                source="linkedin",
                date=occurred,
                title=label,
                summary=_clip(record.text, _TEXT_CLIP),
                impact="",
                url=_clean_share_url(record.url),
                raw_context=f"linkedin {record.kind}: {label}",
            )
        )
    return items


def _first_sentence(text: str) -> str:
    """The post's first complete sentence — a proper row title, never a hard clip.

    Character-clipped titles read as cut off mid-thought in the report; a
    full sentence (capped at ``_TITLE_CLIP``) always ends naturally.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    match = re.search(r"[^.!?]*[.!?]", cleaned)
    sentence = match.group(0).strip() if match else cleaned
    if len(sentence) > _TITLE_CLIP:
        return sentence[: _TITLE_CLIP - 1].rsplit(" ", 1)[0] + "…"
    return sentence


def _clean_share_url(url: str | None) -> str | None:
    """Strip tracking parameters (``utm_*``, ``rcm``) from share URLs for clean report links."""
    if not url:
        return url
    split = urlsplit(url)
    if not split.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(split.query)
        if not key.lower().startswith("utm_") and key.lower() not in ("rcm", "rm")
    ]
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(kept), split.fragment))


def _parse_linkedin_datetime(value: str | int | None) -> date | None:
    """Parse LinkedIn dates: ISO timestamps, ``YYYY-MM-DD[ HH:MM]``, or epoch ms."""
    if value is None:
        return None
    if isinstance(value, int):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    candidates = (
        text.replace("Z", "+00:00"),
        text.replace(" ", "T", 1) if " " in text and "T" not in text else text,
    )
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M").date()
    except ValueError:
        return None


def _handle_from_profile_url(profile_url: str) -> str:
    """Extract the vanity handle from a LinkedIn profile URL."""
    match = re.search(r"/in/([^/?#]+)/?", profile_url)
    return match.group(1) if match else profile_url


def _first_key(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among *keys* in *data*."""
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def _clip(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters on a word boundary."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"
