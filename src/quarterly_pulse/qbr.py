"""QBR Architect core: aggregate populated quarterly reports into a leader QBR.

Implements the content contract in ``skills/qbr-architect/SKILL.md``:

* Loads every populated quarterly report (``*_populated.md`` or any ``.md``
  following the template structure) from a source directory, reusing
  :func:`quarterly_pulse.parser.parse_template` for Section 1 profiles and
  Sections 2-9 table rows.
* Plans a prioritized clarifying interview (max 10 questions) from the gaps
  the sources leave against the four-slide contract. The CLI asks the
  questions; answers are merged into the synthesis context.
* Synthesizes a :class:`QbrContent` whose slot lists mirror the template's
  exact inventory (3+3, 4+4+4, 4+4+4, 2+2+3) via LCEL structured output,
  with per-slot source-row citations the renderer resolves into locators —
  the LLM never writes URLs itself, so slide links cannot be fabricated.
* Chunked pre-summarization (same pattern as the quarterly synthesizer)
  engages when aggregated reports exceed the context budget.

The module is provider-agnostic: the chat model is injected as a
``langchain_core`` ``BaseChatModel`` built by the CLI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from quarterly_pulse.models import TemplateProfile
from quarterly_pulse.parser import parse_template

#: Character budgets guarding the model context window (mirrors synthesizer).
_CONTEXT_CHAR_BUDGET = 150_000
_CHUNK_CHAR_BUDGET = 50_000
_DETAIL_CLIP = 400
#: Maximum questions in the initial clarifying interview (SKILL.md contract).
INTERVIEW_BUDGET = 10

CONFIG_FILENAME = "qbr-config.yaml"

_QUARTER_RE = re.compile(r"^Q([1-4])\s*(\d{4})$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class QbrConfig(BaseModel):
    """Deck metadata; loaded from ``qbr-config.yaml`` beside the reports."""

    leader: str | None = Field(default=None, description="Leader name for slide 1 identity.")
    org: str | None = Field(default=None, description="Organization / team / GEO.")
    review_quarter: str | None = Field(
        default=None, description="Review quarter label, e.g. 'Q3 2026'."
    )
    preview_quarter: str | None = Field(
        default=None, description="Preview quarter label, e.g. 'Q4 2026'."
    )
    audience: str | None = Field(default=None, description="Executive audience, if known.")

    @classmethod
    def load(cls, reports_dir: Path) -> QbrConfig:
        """Read ``qbr-config.yaml`` from *reports_dir* when present."""
        path = reports_dir / CONFIG_FILENAME
        if not path.is_file():
            return cls()
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def merged(self, **overrides: str | None) -> QbrConfig:
        """Apply non-empty CLI overrides on top of the file configuration."""
        data = self.model_dump()
        for key, value in overrides.items():
            if value:
                data[key] = value
        return QbrConfig.model_validate(data)


def quarter_token(label: str) -> str:
    """The 'Q3' token of a quarter label like 'Q3 2026'."""
    return label.split()[0].upper() if label.split() else label.upper()


def next_quarter(label: str) -> str:
    """The quarter following *label*, handling Q4 -> Q1 year rollover."""
    match = _QUARTER_RE.match(label.strip())
    if not match:
        raise ValueError(f"quarter label must look like 'Q3 2026', got {label!r}")
    quarter, year = int(match.group(1)), int(match.group(2))
    if quarter == 4:
        return f"Q1 {year + 1}"
    return f"Q{quarter + 1} {year}"


def resolve_deck_config(
    reports: list[TeamReport], config: QbrConfig, **overrides: str | None
) -> QbrConfig:
    """Merge file config, CLI overrides, and report consensus for quarters.

    The review quarter comes from the config, a flag, or — only when every
    report's Section 1 states the same quarter — that consensus. The preview
    quarter rolls over from the review quarter (Q4 -> Q1). Never guessed from
    filenames or today's date.
    """
    merged = config.merged(**overrides)
    if not merged.review_quarter:
        quarters = {
            report.profile.quarter.strip()
            for report in reports
            if report.profile.quarter and report.profile.quarter.strip()
        }
        if len(quarters) == 1:
            merged.review_quarter = quarters.pop()
    if not merged.review_quarter:
        raise ValueError(
            "review quarter unknown: set review_quarter in qbr-config.yaml "
            "or pass --review-quarter (e.g. 'Q3 2026')"
        )
    if not merged.preview_quarter:
        merged.preview_quarter = next_quarter(merged.review_quarter)
    if not merged.org:
        geos = {
            report.profile.team_geo.strip()
            for report in reports
            if report.profile.team_geo and report.profile.team_geo.strip()
        }
        if len(geos) == 1:
            merged.org = geos.pop()
    return merged


# ---------------------------------------------------------------------------
# Source reports
# ---------------------------------------------------------------------------


class ReportRow(BaseModel):
    """One populated table row from a contributor's quarterly report."""

    row_id: str
    contributor: str
    section: int
    section_title: str
    columns: list[str] = Field(default_factory=list, description="Section column headers.")
    title: str
    detail: str
    links: str


class TeamReport(BaseModel):
    """One contributor's populated quarterly report."""

    path: Path
    profile: TemplateProfile
    rows: list[ReportRow]


def load_team_reports(reports_dir: Path) -> list[TeamReport]:
    """Load every Markdown report in *reports_dir* (sorted by filename).

    Placeholder rows (``*e.g., ...*`` examples and copy/paste rows) are
    skipped so empty sections do not pollute the synthesis context.
    """
    reports: list[TeamReport] = []
    for path in sorted(reports_dir.glob("*.md")):
        if path.name.startswith("."):
            continue
        parsed = parse_template(path)
        contributor = (parsed.profile.name_role or path.stem).split(",")[0].strip()
        rows: list[ReportRow] = []
        for section in parsed.sections:
            for index, line in enumerate(section.body_rows):
                cells = _split_row(line)
                if cells is None or _is_placeholder(cells):
                    continue
                padded = (cells + ["", "", ""])[:3]
                rows.append(
                    ReportRow(
                        row_id=f"{path.stem}-s{section.number}-{index:02d}",
                        contributor=contributor,
                        section=section.number,
                        section_title=section.title,
                        columns=section.columns,
                        title=padded[0].strip(),
                        detail=padded[1].strip(),
                        links=padded[2].strip(),
                    )
                )
        reports.append(TeamReport(path=path, profile=parsed.profile, rows=rows))
    return reports


def _split_row(line: str) -> list[str] | None:
    """Split a Markdown table row into cells; None for non-row lines."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    parts = stripped.strip("|").split("|")
    if len(parts) < 2:
        return None
    return [part.replace("\\|", "|").strip() for part in parts]


def _is_placeholder(cells: list[str]) -> bool:
    """True for template example rows and copy/paste placeholders."""
    joined = " ".join(cells)
    return (
        not any(cells)
        or joined.startswith("*e.g.")
        or "Copy/paste row to add" in joined
        or joined.startswith("*Enter")
    )


def source_manifest(reports: list[TeamReport]) -> dict[str, ReportRow]:
    """Map every row_id to its row for locator resolution and grounding."""
    return {row.row_id: row for report in reports for row in report.rows}


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------


class SlotItem(BaseModel):
    """One template slot: a bold title run plus a regular detail run."""

    title: str = Field(description="Short bold slot name, <= 60 characters, contributor-POV.")
    detail: str = Field(
        description=(
            "One concise sentence (<= 220 characters): outcome, status, milestone, "
            "or metric. Lowlights leave mitigation to the separate field."
        )
    )
    mitigation: str = Field(
        default="",
        description="Lowlights only: corrective action with recovery timing or owner.",
    )
    source_row_ids: list[str] = Field(
        default_factory=list,
        description="IDs of the source report rows this slot is grounded in.",
    )
    objective_kr: str = Field(
        default="",
        description="Slide 2 items: the annual objective / team KR this maps to, or '' when unknown.",
    )
    stop_link: str = Field(
        default="",
        description="Commitments only: the stop/deprioritization entry this must-win is funded by.",
    )
    contributor: str = Field(
        default="",
        description="Aggregated decks: the contributor whose report supplied this item (notes only).",
    )


class GapRecord(BaseModel):
    """An unresolved material gap, surfaced in the companion record."""

    slide: int = Field(description="Slide number (1-4) the gap affects.")
    slot: str = Field(description="Slot or field the gap concerns.")
    description: str = Field(description="What is missing and why it matters.")


class QbrContent(BaseModel):
    """The full four-slide content contract, mirroring the template inventory."""

    org_team: str = Field(description="Org/team name for the slide 1 title field.")
    leader_identity: str = Field(description="Leader identity for the slide 1 identity field.")
    highlights: list[SlotItem] = Field(default_factory=list, max_length=3)
    lowlights: list[SlotItem] = Field(default_factory=list, max_length=3)
    statement_of_impact: str = Field(description="Appended after the 'Statement of impact:' label.")
    execution_metadata: str = Field(description="Replaces [EXECUTION AGAINST KEY OBJECTIVES].")
    pillar_programs: list[SlotItem] = Field(default_factory=list, max_length=4)
    pillar_customers: list[SlotItem] = Field(default_factory=list, max_length=4)
    pillar_ecosystem: list[SlotItem] = Field(default_factory=list, max_length=4)
    guardrails_metadata: str = Field(description="Replaces [EXECUTION GUARDRAILS & COMMITMENTS].")
    commitments: list[SlotItem] = Field(default_factory=list, max_length=4)
    opportunities: list[SlotItem] = Field(default_factory=list, max_length=4)
    stops: list[SlotItem] = Field(default_factory=list, max_length=4)
    action_metadata: str = Field(description="Replaces [ACTION & ALIGNMENT REGISTER].")
    asks: list[SlotItem] = Field(default_factory=list, max_length=2)
    decision_date: str = Field(description="Target Decision Date field, or 'Date to confirm'.")
    interlocks: list[SlotItem] = Field(default_factory=list, max_length=2)
    agreement_date: str = Field(description="Target Agreement Date field, or 'Date to confirm'.")
    radar: list[SlotItem] = Field(default_factory=list, max_length=3)
    gaps: list[GapRecord] = Field(default_factory=list)


class InterviewQuestion(BaseModel):
    """One clarifying question resolving one coherent decision or gap."""

    topic: str = Field(description="Short topic, e.g. 'must-win owner/date'.")
    question: str = Field(description="The question asked verbatim, self-contained.")


class InterviewPlan(BaseModel):
    """Prioritized interview plan derived from the source inventory."""

    context_summary: str = Field(description="What was understood from the sources, 2-4 sentences.")
    questions: list[InterviewQuestion] = Field(
        default_factory=list, description="At most 10, highest priority first."
    )


class DistilledRow(BaseModel):
    """A compacted source row produced by chunked pre-summarization."""

    row_id: str = Field(description="Preserved verbatim from the source row.")
    contributor: str = ""
    section: int = 0
    title: str = ""
    summary: str = Field(default="", description="One sentence preserving key facts and numbers.")


class DistilledRows(BaseModel):
    """Wrapper for a batch of distilled source rows."""

    rows: list[DistilledRow] = Field(default_factory=list)


class QbrSynthesisResult(BaseModel):
    """Synthesis outcome plus grounding audit details."""

    content: QbrContent
    rows_received: int = 0
    rows_for_synthesis: int = 0
    chunked: bool = False
    grounding_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

INTERVIEW_SYSTEM = """You plan a clarifying interview for a team leader's quarterly business review.

You see the leader's deck configuration and an inventory of aggregated team
activity rows parsed from the team's populated quarterly reports (sections:
2 upstream contributions, 3 reusable assets, 4 customer field work,
5 executive/partner/industry engagements, 6 custom workstreams,
7 challenges & mitigations, 8 looking ahead, 9 help needed & team radar).

Rules:
- Ask at most 10 questions, highest-priority first; ask zero when the sources
  already support a grounded four-slide draft.
- Each question resolves ONE coherent decision or information gap: concrete
  outcomes and evidence, lowlight consequences and recovery, objective/KR
  alignment, must-win owner/target/date, stop-list trade-offs, or
  decision owner/blocker/date for asks.
- Never ask the leader to repeat facts already visible in the inventory or
  configuration.
- Material gaps only: skip anything that does not affect an executive
  decision on slides 1-4.
- Number nothing; write each question self-contained and answerable with
  'unknown' or 'skip'."""


INTERVIEW_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", INTERVIEW_SYSTEM),
        (
            "human",
            """Deck configuration:
- Leader: {leader}
- Org / team: {org}
- Review quarter: {review_quarter}
- Preview quarter: {preview_quarter}

Activity row inventory (JSON):
{records}

Previously answered questions (JSON, do not re-ask):
{answers}

Plan the interview.""",
        ),
    ]
)


SYNTHESIS_SYSTEM = """You are an executive QBR writer for a team leader, following the QBR Architect content contract.

GROUNDING RULES:
- Use ONLY facts, customer names, metrics, numbers, dates, owners, and outcomes that appear in the activity rows, the deck configuration, or the leader's interview answers.
- NEVER fabricate or guess numbers, owners, or dates. Distinguish observed results (bookings, customer validation, engineering deliverables) from leader-reported statements; keep pipeline, influence, and closed wins distinct.
- Cite source_row_ids for every slot; copy them verbatim from the records. The renderer resolves them into links — never write URLs yourself.

TEAM VOICE:
- Write from the leader's point of view ('we'), concise and candid, executive tone. Attribute items to contributors only via the contributor field (speaker notes), never in slot text.

SLOT RULES:
- Respect capacities: 3 highlights, 3 lowlights, 4 items per pillar (slide 2), 4 per column (slide 3), 2 asks, 2 interlocks, 3 radar (slide 4). Never fabricate entries to reach capacity; omit what is unsupported.
- Example topics in the source rows' sections are prompts, not mandatory categories: pick the strongest distinct outcomes for highlights (customer/deal influence, program/technical delivery, ecosystem/enablement adapted to actual scope).
- Lowlights: name what missed, cause and business consequence; the mitigation field carries a specific corrective action with recovery timing or owner ('Unknown owner' beats an invented one).
- Slide 2: every populated item maps to a supplied annual objective / team KR in objective_kr; leave '' when the sources do not state one — never invent a strategic objective.
- Slide 3: commitments are must-wins with deliverable, target, owner, and date when supplied; opportunities are conditional upside, never approved commitments; stops name work to cease and the focus it frees. Every commitment carries the stop_link that funds it, or 'Trade-off to confirm'.
- Slide 4: asks and interlocks name the requested decision, the decision owner (distinct from the executor), the blocker, and the target date; decision_date/agreement_date summarize the deadlines or read 'Date to confirm'.
- Titles are concise names (<= 60 chars); details are ONE sentence (<= 220 chars). No Markdown formatting inside fields.
- Statement of impact: a short supported statement of overall quarter impact.
- Record unresolved material gaps in gaps (slide number, slot, what is missing) instead of leaving example text.
- When the leader answered interview questions, those answers outrank the parsed rows."""


SYNTHESIS_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYNTHESIS_SYSTEM),
        (
            "human",
            """Deck configuration:
- Leader: {leader}
- Org / team: {org}
- Review quarter: {review_quarter}
- Preview quarter: {preview_quarter}

Leader interview answers (JSON):
{answers}

Contributor reports:
{contributors}

Activity rows (JSON, with row_id for citations):
{records}

Map these into the four-slide QBR following the output schema.""",
        ),
    ]
)


DISTILL_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You compact activity rows as a pre-step for QBR synthesis.
STRICT RULES:
- Preserve row_id, contributor, and section verbatim.
- Preserve every distinct fact, customer name, metric, number, and owner; never invent new ones.
- Compress each row's title and detail into one summary sentence.
- Do not drop any rows; each must appear in the output.""",
        ),
        ("human", "Activity rows (JSON):\n{records}\n\nReturn the distilled rows."),
    ]
)


# ---------------------------------------------------------------------------
# Interview planning
# ---------------------------------------------------------------------------


async def plan_interview(
    config: QbrConfig,
    reports: list[TeamReport],
    model: BaseChatModel,
    answers: list[dict[str, str]] | None = None,
) -> InterviewPlan:
    """Plan (or re-plan) the prioritized clarifying interview."""
    chain: Runnable = INTERVIEW_PROMPT | model.with_structured_output(InterviewPlan)
    plan = await chain.ainvoke(
        {
            "leader": config.leader or "Leader to confirm",
            "org": config.org or "Team to confirm",
            "review_quarter": config.review_quarter or "Quarter to confirm",
            "preview_quarter": config.preview_quarter or "Next quarter",
            "records": _records_json(_compact_rows(_all_rows(reports))),
            "answers": _answers_json(answers or []),
        }
    )
    plan.questions = plan.questions[:INTERVIEW_BUDGET]
    return plan


def _answers_json(answers: list[dict[str, str]]) -> str:
    return json.dumps(answers, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


async def synthesize_qbr(
    config: QbrConfig,
    reports: list[TeamReport],
    answers: list[dict[str, str]],
    model: BaseChatModel,
) -> QbrSynthesisResult:
    """Aggregate the team reports (plus interview answers) into a QbrContent."""
    rows = _all_rows(reports)
    records = _compact_rows(rows)
    chunked = False

    if records and _records_chars(records) > _CONTEXT_CHAR_BUDGET:
        chunked = True
        distill_chain: Runnable = DISTILL_PROMPT | model.with_structured_output(DistilledRows)
        chunks = _chunk_records(records, _CHUNK_CHAR_BUDGET)
        batches = await distill_chain.abatch(
            [{"records": _records_json(chunk)} for chunk in chunks]
        )
        records = [
            row.model_dump()
            for batch in batches
            for row in batch.rows
        ]

    chain: Runnable = SYNTHESIS_PROMPT | model.with_structured_output(QbrContent)
    content = await chain.ainvoke(
        {
            "leader": config.leader or "Leader to confirm",
            "org": config.org or "Team to confirm",
            "review_quarter": config.review_quarter or "Quarter to confirm",
            "preview_quarter": config.preview_quarter or "Next quarter",
            "answers": _answers_json(answers),
            "contributors": "\n".join(
                f"- {report.path.name}: {(report.profile.name_role or 'unknown')}"
                for report in reports
            ),
            "records": _records_json(records),
        }
    )

    warnings = _validate_row_citations(content, source_manifest(reports))
    return QbrSynthesisResult(
        content=content,
        rows_received=len(rows),
        rows_for_synthesis=len(records),
        chunked=chunked,
        grounding_warnings=warnings,
    )


def _validate_row_citations(
    content: QbrContent, manifest: dict[str, ReportRow]
) -> list[str]:
    """Flag slots citing row_ids that do not exist in the source manifest."""
    warnings: list[str] = []
    for field in _SLOT_FIELDS:
        for index, slot in enumerate(getattr(content, field)):
            unknown = [row_id for row_id in slot.source_row_ids if row_id not in manifest]
            if unknown:
                warnings.append(
                    f"{field}[{index}] ({slot.title}): unknown source row(s) {', '.join(unknown)}"
                )
    return warnings


_SLOT_FIELDS = (
    "highlights",
    "lowlights",
    "pillar_programs",
    "pillar_customers",
    "pillar_ecosystem",
    "commitments",
    "opportunities",
    "stops",
    "asks",
    "interlocks",
    "radar",
)


# ---------------------------------------------------------------------------
# Compaction helpers (same patterns as the quarterly synthesizer)
# ---------------------------------------------------------------------------


def _all_rows(reports: list[TeamReport]) -> list[ReportRow]:
    return [row for report in reports for row in report.rows]


def _compact_rows(rows: list[ReportRow]) -> list[dict[str, Any]]:
    return [
        {
            "row_id": row.row_id,
            "contributor": row.contributor,
            "section": row.section,
            "section_title": row.section_title,
            "columns": row.columns,
            "title": row.title,
            "detail": _clip(row.detail, _DETAIL_CLIP),
            "links": row.links,
        }
        for row in rows
    ]


def _records_chars(records: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(record, ensure_ascii=False)) for record in records)


def _records_json(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=1)


def _chunk_records(
    records: list[dict[str, Any]], budget: int
) -> list[list[dict[str, Any]]]:
    """Split records into batches whose serialized size stays under *budget*."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for record in records:
        record_chars = len(json.dumps(record, ensure_ascii=False))
        if current and current_chars + record_chars > budget:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(record)
        current_chars += record_chars
    if current:
        chunks.append(current)
    return chunks


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"
