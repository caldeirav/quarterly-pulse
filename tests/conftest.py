"""Shared pytest fixtures for quarterly-pulse tests."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from quarterly_pulse.config import Settings

from tests.fakes import StructuredFakeChatModel

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

#: Fixed reporting window used across connector tests.
START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 9, 30)

#: Environment variables that could leak from a developer machine into tests.
_ISOLATED_ENV_VARS = (
    "GITHUB_TOKEN",
    "LLM_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LINKEDIN_ACCESS_TOKEN",
    "APIFY_API_TOKEN",
    "APIFY_LINKEDIN_ACTOR",
    "APIFY_LINKEDIN_INPUT",
    "SCRAPECREATORS_API_KEY",
    "EXA_API_KEY",
)


@pytest.fixture()
def make_settings(monkeypatch: pytest.MonkeyPatch):
    """Factory for isolated ``Settings`` instances free of real env/.env leakage."""

    def _factory(**kwargs: object) -> Settings:
        for var in _ISOLATED_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]

    return _factory


# ---------------------------------------------------------------------------
# Deterministic fake chat model with scripted structured outputs
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_model() -> StructuredFakeChatModel:
    return StructuredFakeChatModel(responses=["unused"])


def _set_mtime(path: Path, day: date, hour: int = 12) -> None:
    """Pin a file's mtime to a fixed UTC timestamp."""
    timestamp = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).timestamp()
    os.utime(path, (timestamp, timestamp))


@pytest.fixture()
def template_path() -> Path:
    """Path to the canonical quarterly template shipped in the repository."""
    return REPO_ROOT / "templates" / "quarterly-template.md"


@pytest.fixture()
def template_text(template_path: Path) -> str:
    """Raw text of the canonical quarterly template."""
    return template_path.read_text(encoding="utf-8")


@pytest.fixture()
def github_graphql_response() -> dict:
    return json.loads((FIXTURES_DIR / "github_graphql_response.json").read_text(encoding="utf-8"))


@pytest.fixture()
def github_search_response() -> dict:
    return json.loads((FIXTURES_DIR / "github_search_response.json").read_text(encoding="utf-8"))


@pytest.fixture()
def linkedin_portability_response() -> dict:
    return json.loads((FIXTURES_DIR / "linkedin_portability_response.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# LinkedIn export archives
# ---------------------------------------------------------------------------


@pytest.fixture()
def export_source_dir() -> Path:
    """Static LinkedIn export fixture directory."""
    return FIXTURES_DIR / "linkedin_export"


@pytest.fixture()
def export_dir(tmp_path: Path, export_source_dir: Path) -> Path:
    """A LinkedIn export directory copied into a temporary docs dir."""
    target = tmp_path / "LinkedIn Data Export"
    shutil.copytree(export_source_dir, target)
    _set_mtime(target / "Articles" / "field-notes.html", date(2026, 9, 2))
    return target


@pytest.fixture()
def export_zip(tmp_path: Path, export_source_dir: Path) -> Path:
    """A zipped LinkedIn export placed inside a temporary docs dir."""
    zip_path = tmp_path / "linkedin-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for member in sorted(export_source_dir.rglob("*")):
            if member.is_file():
                info = zipfile.ZipInfo(
                    filename=member.relative_to(export_source_dir).as_posix(),
                    date_time=(2026, 9, 2, 10, 0, 0),
                )
                archive.writestr(info, member.read_bytes())
    return zip_path


# ---------------------------------------------------------------------------
# Local documents
# ---------------------------------------------------------------------------


def _build_minimal_pdf(text: str) -> bytes:
    """Build a tiny but valid single-page PDF containing *text*."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_position = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF"
    ).encode()
    return bytes(out)


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    from docx import Document

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(str(path))


def _write_pptx(path: Path, slide_texts: list[str]) -> None:
    from pptx import Presentation

    presentation = Presentation()
    for text in slide_texts:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = text
    presentation.save(str(path))


@pytest.fixture()
def docs_dir(tmp_path: Path) -> Path:
    """An assembled documents directory covering every supported format and filter path.

    Contents (reporting window = 2026-08-01 .. 2026-09-30):

    * ``customer-poc-readout.md``  — old mtime, frontmatter date in window -> included
    * ``reference-architecture.md`` — fresh mtime, no explicit dates       -> included
    * ``old-dated.md``             — old mtime, explicit date in text      -> included
    * ``stale-notes.txt``          — old mtime, no in-window dates         -> excluded
    * ``generated-report.pdf``     — old mtime, explicit date in text      -> included
    * ``sprint-notes.docx``        — fresh mtime                           -> included
    * ``workshop-deck.pptx``       — fresh mtime                           -> included
    * ``corrupt.pdf``              — unreadable                            -> warning
    """
    for name in (
        "customer-poc-readout.md",
        "reference-architecture.md",
        "old-dated.md",
        "stale-notes.txt",
        "corrupt.pdf",
    ):
        shutil.copy2(FIXTURES_DIR / "docs" / name, tmp_path / name)

    (tmp_path / "generated-report.pdf").write_bytes(
        _build_minimal_pdf("Customer POC readout for Globex dated 2026-09-10")
    )
    _write_docx(tmp_path / "sprint-notes.docx", ["Partner co-build notes", "Joint demo delivered"])
    _write_pptx(tmp_path / "workshop-deck.pptx", ["AI Workshop for Field Teams"])

    _set_mtime(tmp_path / "customer-poc-readout.md", date(2026, 1, 15))
    _set_mtime(tmp_path / "old-dated.md", date(2026, 2, 1))
    _set_mtime(tmp_path / "stale-notes.txt", date(2026, 3, 1))
    _set_mtime(tmp_path / "corrupt.pdf", date(2026, 3, 1))
    _set_mtime(tmp_path / "generated-report.pdf", date(2026, 1, 20))
    _set_mtime(tmp_path / "reference-architecture.md", date(2026, 8, 25))
    _set_mtime(tmp_path / "sprint-notes.docx", date(2026, 8, 18))
    _set_mtime(tmp_path / "workshop-deck.pptx", date(2026, 9, 5))
    return tmp_path
