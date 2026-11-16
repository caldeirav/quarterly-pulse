"""Async local documents connector.

Recursively scans ``--docs-dir`` for ``.md``, ``.txt``, ``.pdf``, ``.docx``
and ``.pptx`` documents (skipping LinkedIn export artifacts handled by the
LinkedIn connector). A document is included when its filesystem
``mtime``/``ctime`` is on or after the start date, **or** an explicit date in
its frontmatter/body falls inside ``[start_date, end_date]``.

Text extraction uses ``pypdf``, ``python-docx`` and ``python-pptx``; corrupt
or unreadable files are reported as warnings and skipped. Embedded Google
Docs/Slides/GitHub URLs are captured for the report's Links column.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from quarterly_pulse.models import ActivityItem, ConnectorResult

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".pptx"}

ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
INTERESTING_URL_PREFIXES = (
    "https://docs.google.com/",
    "https://drive.google.com/",
    "https://github.com/",
    "https://raw.githubusercontent.com/",
)
_SUMMARY_CLIP = 1000
_MAX_URLS_CAPTURED = 5


async def fetch_docs_activity(
    docs_dir: Path,
    start_date: date,
    end_date: date,
    exclude: set[Path] | None = None,
) -> ConnectorResult:
    """Gather ActivityItems from contributed documents under *docs_dir*."""
    exclusions = exclude or set()
    candidates = [
        path
        for path in sorted(docs_dir.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not _is_excluded(path, exclusions)
        and not _is_hidden(path, docs_dir)
    ]

    results = await asyncio.gather(
        *(asyncio.to_thread(_process_document, path, docs_dir, start_date, end_date) for path in candidates)
    )

    items: list[ActivityItem] = []
    warnings: list[str] = []
    for item, warning in results:
        if warning is not None:
            warnings.append(warning)
        if item is not None:
            items.append(item)

    return ConnectorResult(source="docs", strategy="local_documents", items=items, warnings=warnings)


# ---------------------------------------------------------------------------
# Document processing
# ---------------------------------------------------------------------------


def _process_document(
    path: Path,
    docs_dir: Path,
    start_date: date,
    end_date: date,
) -> tuple[ActivityItem | None, str | None]:
    """Extract, date-filter and map one document; returns ``(item, warning)``."""
    try:
        text = extract_text(path)
    except Exception as exc:  # unreadable/corrupt file: degrade gracefully
        return None, f"Skipping unreadable document {path.name}: {exc}"

    explicit_dates = _find_iso_dates(text)
    in_window = [day for day in explicit_dates if start_date <= day <= end_date]
    file_date = _filesystem_date(path)

    if file_date < start_date and not in_window:
        return None, None

    item_date = in_window[0] if in_window else file_date
    relative_path = path.relative_to(docs_dir).as_posix()
    urls = _extract_urls(text)

    return (
        ActivityItem(
            source="docs",
            date=item_date,
            title=_document_title(text, path),
            summary=_clip(_strip_markup(text), _SUMMARY_CLIP),
            impact="",
            url=_primary_url(urls),
            raw_context=(
                f"document: {relative_path}; dates found: "
                f"{', '.join(day.isoformat() for day in explicit_dates[:5]) or 'none'}; "
                f"urls: {', '.join(urls[:_MAX_URLS_CAPTURED]) or 'none'}"
            ),
        ),
        None,
    )


def extract_text(path: Path) -> str:
    """Extract plain text from a supported document."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    if suffix == ".pptx":
        from pptx import Presentation

        presentation = Presentation(str(path))
        texts: list[str] = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(shape.text_frame.text)
        return "\n".join(texts)
    raise ValueError(f"unsupported document extension: {suffix}")


def _filesystem_date(path: Path) -> date:
    """The document's filesystem date: the later of mtime and ctime."""
    stat = path.stat()
    return datetime.fromtimestamp(max(stat.st_mtime, stat.st_ctime), tz=timezone.utc).date()


def _document_title(text: str, path: Path) -> str:
    """Prefer a frontmatter ``title:`` value; otherwise humanize the file name."""
    frontmatter = _frontmatter(text)
    if frontmatter:
        match = re.search(r"^title:\s*(.+)$", frontmatter, re.MULTILINE | re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("\"'")
    return path.stem.replace("-", " ").replace("_", " ").strip().title()


def _frontmatter(text: str) -> str | None:
    """Return the YAML frontmatter block of a Markdown document, if present."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1]


def _find_iso_dates(text: str) -> list[date]:
    """Find every ``YYYY-MM-DD`` date in the document text (frontmatter included)."""
    found: list[date] = []
    for match in ISO_DATE_RE.finditer(text):
        try:
            found.append(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return found


def _extract_urls(text: str) -> list[str]:
    """Extract URLs, preferring Google Docs/Slides/GitHub links for the Links column."""
    urls = [match.group(0).rstrip(".,;") for match in URL_RE.finditer(text)]
    interesting = [url for url in urls if url.startswith(INTERESTING_URL_PREFIXES)]
    ordered: list[str] = []
    for url in interesting + [url for url in urls if url not in interesting]:
        if url not in ordered:
            ordered.append(url)
    return ordered


def _primary_url(urls: list[str]) -> str | None:
    """The URL used for the ActivityItem, preferring Google Docs/GitHub links."""
    return urls[0] if urls else None


def _strip_markup(text: str) -> str:
    """Drop Markdown/HTML noise for a clean summary."""
    without_frontmatter = text.split("---", 2)[-1] if text.startswith("---") else text
    without_markup = re.sub(r"[#*`>\[\]()!]", " ", without_frontmatter)
    return " ".join(without_markup.split())


def _clip(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters on a word boundary."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _is_excluded(path: Path, exclusions: set[Path]) -> bool:
    """True when *path* is inside any excluded (LinkedIn-owned) path."""
    return any(path == excluded or excluded in path.parents for excluded in exclusions)


def _is_hidden(path: Path, docs_dir: Path) -> bool:
    """True when any path component under *docs_dir* is a dotfile/dotdir."""
    return any(part.startswith(".") for part in path.relative_to(docs_dir).parts)
