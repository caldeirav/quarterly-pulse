"""Unit tests for the local documents connector (offline)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from quarterly_pulse.connectors import docs as docs_module
from quarterly_pulse.connectors.docs import (
    _document_title,
    _extract_urls,
    _find_iso_dates,
    fetch_docs_activity,
)

START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 9, 30)


class TestFetchDocsActivity:
    async def test_includes_and_extracts_all_supported_formats(self, docs_dir: Path) -> None:
        result = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)

        # Every freshly created file has a recent ctime, so each parseable
        # document passes the filesystem-date filter; only corrupt.pdf drops out.
        assert len(result.items) == 7
        by_title = {item.title: item for item in result.items}

        poc = by_title["Acme Corp POC readout"]
        assert poc.date == date(2026, 8, 20)  # explicit frontmatter date
        assert poc.url == "https://docs.google.com/presentation/d/abc123/edit"
        assert "Acme Corp" in poc.summary

        assert by_title["Generated Report"].date == date(2026, 9, 10)  # date inside PDF text
        assert any("Globex" in item.summary for item in result.items)
        assert any("Partner co-build" in item.summary for item in result.items)  # docx
        assert any("AI Workshop" in item.summary for item in result.items)  # pptx

        assert any(
            "unreadable" in warning and "corrupt.pdf" in warning for warning in result.warnings
        )

    async def test_filesystem_date_includes_recent_files(self, docs_dir: Path) -> None:
        result = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)
        # reference-architecture.md has no explicit dates; its fresh mtime includes it.
        ref = next(item for item in result.items if item.title == "Reference Architecture")
        assert ref.url == "https://github.com/quarterly-pulse/rag-reference-arch"
        assert "https://docs.google.com/document/d/def456/edit" in ref.raw_context

    async def test_excludes_old_files_without_window_dates(
        self, docs_dir: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(docs_module, "_filesystem_date", lambda path: date(2026, 1, 15))

        result = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)

        # Only documents carrying an explicit in-window date survive.
        assert {item.title for item in result.items} == {
            "Acme Corp POC readout",
            "Old Dated",
            "Generated Report",
        }

    async def test_linkedin_export_paths_excluded(
        self, docs_dir: Path, export_dir: Path
    ) -> None:
        stray = export_dir / "internal-notes.md"
        stray.write_text("# export notes\nno dates in this text\n")

        with_exclude = await fetch_docs_activity(docs_dir, START_DATE, END_DATE, exclude={export_dir})
        assert all("internal-notes" not in item.raw_context for item in with_exclude.items)

        without_exclude = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)
        assert any("internal-notes" in item.raw_context for item in without_exclude.items)

    async def test_hidden_files_skipped(self, docs_dir: Path) -> None:
        hidden_dir = docs_dir / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "secret.md").write_text("secret notes dated 2026-08-01\n")

        result = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)

        assert all("secret" not in item.raw_context for item in result.items)


class TestEmailDetection:
    async def test_email_export_uses_subject_as_title(self, docs_dir: Path) -> None:
        (docs_dir / "ebc-email.txt").write_text(
            "From: organizer@redhat.com\n"
            "To: vincent@redhat.com\n"
            "Subject: EBC briefing with ITMX - agenda and logistics\n"
            "Sent: Monday, August 10, 2026 9:00 AM\n"
            "\n"
            "Hi Vincent, confirming next week's executive briefing agenda...\n"
        )

        result = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)

        item = next(item for item in result.items if "ITMX" in item.title)
        assert item.title == "EBC briefing with ITMX - agenda and logistics"
        assert "email" in item.raw_context
        assert "subject: EBC briefing with ITMX" in item.raw_context
        assert "document: ebc-email.txt" in item.raw_context

    async def test_non_email_documents_not_flagged(self, docs_dir: Path) -> None:
        (docs_dir / "notes.md").write_text("# Meeting notes\n\nDiscussed the agenda for the summit.\n")

        result = await fetch_docs_activity(docs_dir, START_DATE, END_DATE)

        item = next(item for item in result.items if item.title == "Notes")
        assert "email" not in item.raw_context


class TestHelpers:
    def test_extract_urls_prefers_google_and_github(self) -> None:
        text = (
            "See https://example.com/x and https://docs.google.com/document/d/1/edit "
            "plus https://github.com/org/repo"
        )
        urls = _extract_urls(text)
        assert urls[0] == "https://docs.google.com/document/d/1/edit"
        assert urls[1] == "https://github.com/org/repo"
        assert "https://example.com/x" in urls

    def test_extract_urls_strips_trailing_punctuation(self) -> None:
        assert _extract_urls("link: https://github.com/org/repo.") == ["https://github.com/org/repo"]

    def test_find_iso_dates_ignores_invalid_dates(self) -> None:
        assert _find_iso_dates("on 2026-08-15 and 2026-13-99") == [date(2026, 8, 15)]

    def test_document_title_prefers_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "my-notes.md"
        text = "---\ntitle: Custom Title\n---\n\nbody"
        assert _document_title(text, path) == "Custom Title"

    def test_document_title_falls_back_to_stem(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint-readout_v2.md"
        assert _document_title("# just a heading", path) == "Sprint Readout V2"
