"""LangChain synthesis pipeline.

Takes deduplicated :class:`~quarterly_pulse.models.ActivityItem` records plus
the contributor profile and produces a :class:`QuarterReport` whose row models
match the template's Sections 2-9 column schemas.

Design:

* The chat model is injected as a ``langchain_core`` ``BaseChatModel`` so the
  CLI can build it via ``langchain.chat_models.init_chat_model`` (provider
  selection stays configurable via ``LLM_MODEL`` / ``--model``).
* The chains are plain LCEL: ``ChatPromptTemplate | model.with_structured_output(schema)``,
  executed with ``.ainvoke()`` (and ``.abatch()`` for chunked pre-summarization
  when the activity context would exceed the token budget).
* A grounding guardrail strips any URL in the generated rows that does not
  exist in the retrieved source items, recording violations for the audit
  footer. Customer names, metrics, and numbers are constrained via strict
  system-prompt rules: never fabricate anything absent from the records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from quarterly_pulse.models import ActivityItem, TemplateProfile

#: Character budgets guarding the model context window. Generous on purpose:
#: the chunked pre-summarization path is lossy, so it should only engage for
#: genuinely huge quarters (gpt-4o / claude-sonnet both handle 128k+ tokens).
_CONTEXT_CHAR_BUDGET = 150_000
_CHUNK_CHAR_BUDGET = 50_000
_SUMMARY_CLIP = 1000
_IMPACT_CLIP = 200
_RAW_CONTEXT_CLIP = 1500

SYSTEM_PROMPT = """You are a meticulous quarterly-activity report writer for a technology contributor.

STRICT GROUNDING RULES:
- Use ONLY facts, customer names, metrics, numbers, dates, and URLs that appear in the provided activity records.
- NEVER fabricate or guess customer names, metrics, or URLs. If no link exists in the records, leave the links cell empty.
- Prefer concrete outcomes, counts, and statuses found in the records over generic phrasing.
- Keep row-title and links cells concise. Write the activity/outcome (middle) column as 2-3 full sentences: what was done, the concrete outcome, audience, or adoption, and why it matters. A single short sentence is too thin — never stop at the first sentence. No Markdown formatting inside cells.
- Set section_6_title only when section 6 rows exist, deriving a short title from their content (e.g. "Research & Internal Tooling").

COMPLETENESS RULES:
- GitHub records arrive pre-aggregated per repository: render exactly ONE row per GitHub repository item, citing the repository URL and the most notable PR/issue links from its raw_context.
- Include EVERY LinkedIn post and EVERY local document record as its own row in the appropriate section; merge only exact duplicates of the same underlying item. NEVER drop or consolidate records merely because there are many of them.
- When merging duplicates that describe the same underlying work, keep the richest description and all distinct links.
- EVERY gathered item must appear somewhere in the report, even when no section fits perfectly: choose the closest plausible section, and use Section 6 (Custom Workstream) as the catch-all for overflow work. An item placed in the wrong section is far better than a missing item — the contributor re-places entries manually.

ROW CONTENT RULES:
- GitHub repository rows: lead with what the repository is and does (use its description from the record) and what the contributor actually did there; keep pull request/issue/commit counts as supporting detail only, never as the main content.
- LinkedIn posts: give the row a concise, descriptive name derived from the post (for example "Agent Substrate Day talk on instant circuit breakers"), never prefixed with "LinkedIn" and never a sentence quoted from the post. Write a 2-3 sentence summary drawn from the post's own content; when the post coincides with presenting at, moderating, or attending an event, name the event and the talk. Do not mention reaction, comment, or follower counts.
- Event series (for example Summit Connect across cities, or several EBC briefings): create ONE row PER event or location. Never merge multiple events or locations into a single row.
- Row titles are concise, descriptive names written from the contributor's point of view — what the activity IS for the person reporting (for example "ITMX executive briefing", "Multi-cluster RAG reference architecture"). Never use a sentence quoted or clipped from the source text.
- The activity/outcome (middle) column is an external summary and assessment: describe what was accomplished and why it matters as a knowledgeable observer assessing the contributor's quarter would — never a repetition or light rewording of the extracted source text.
- Local documents and email exports: classify each into its best-fitting section — Section 3 (repeatable solution templates & reusable assets), Section 4 (customer-specific opportunities & field work, including customer summit and briefing readouts), Section 5 (industry and community engagements, including AI Center of Excellence programs), or Section 6 (custom workstream). Correlate email threads with the event they concern (EBC briefings, Summit Connect and other field events) instead of listing each email as a separate entry. Use the document's own title as the row title and summarize its actual content in 1-3 sentences.
- Section 8 (Looking Ahead) and Section 9 (Help Needed & Team Radar): PROPOSE concrete recommendations derived from the records — work with momentum to continue or ship next quarter, customer engagements to follow up, emerging opportunities, activities to stop or deprioritize, and plausible escalations or decisions where leadership help is needed. These are suggestions the contributor will edit manually; never invent URLs, owners, dates, or metrics that are not in the records.
- Copy URLs character-for-character from the records; never shorten, paraphrase, or reconstruct them.

SECTION MAPPING GUIDE:
- Section 2, Open Source & Upstream Contributions: upstream PRs, commits, issues/RFCs, maintainership, community leadership.
- Section 3, Repeatable Solution Templates & Reusable Assets: reference architectures, demo repos, automation scripts, playbooks, workshops, internal tools built for reuse.
- Section 4, Customer-Specific Opportunities & Field Work: customer POCs, architecture sprints, RFP/tender support, design partner trials, and customer-facing events such as summit series (e.g., Summit Connect) and executive briefings.
- Section 5, Executive, Partner & Industry Engagements: talks, webinars, LinkedIn posts/articles, EBC briefings, partner co-builds, product RFEs, and industry/community programs such as AI Center of Excellence (AI CoE) initiatives.
- Section 6, Custom Workstream: meaningful overflow work such as research spikes, internal tooling, standards bodies, mentoring.
- Section 7, Challenges, Friction & Mitigations: blockers, slipped timelines, or dependency issues evidenced in the records.
- Section 8, Looking Ahead: recommended next-quarter commitments and suggestions (Must-Win Priority / Emerging Opportunity / Stop / Deprioritize).
- Section 9, Help Needed & Team Radar: suggested escalations, decisions needing leadership input, and dates and context the team should know."""

FINAL_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        (
            "human",
            """Contributor profile:
- Name & Role: {name_role}
- Team / GEO: {team_geo}
- Reporting Quarter & Year: {quarter}
- GitHub handle: {github_handle}
- LinkedIn profile: {linkedin_url}
- Key Goals / OKRs: {okrs}

Activity records since the start of the reporting window (JSON):
{records}

Map these records into the quarterly report sections following the output schema.""",
        ),
    ]
)

SUMMARIZE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You compress activity records as a pre-step for quarterly report synthesis.
STRICT RULES:
- Preserve every distinct fact, customer name, metric, and URL; never invent new ones.
- Merge exact duplicate records only.
- Do not drop any records; the final report must include every LinkedIn post, every document, and every repository.""",
        ),
        ("human", "Activity records (JSON):\n{records}\n\nReturn the distilled records."),
    ]
)

RETRY_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        (
            "human",
            """A quarterly report draft may have accidentally left out the activity records below.

First compare each left-out record against the existing draft: when a record's content is already adequately represented by an existing row — even under different wording or without a link — do NOT create a new row for it. Create rows only for genuinely missing content, placed in the correct section following every rule above, with contributor-POV titles and external assessments.

Contributor profile:
- Name & Role: {name_role}
- Team / GEO: {team_geo}
- Reporting Quarter & Year: {quarter}
- GitHub handle: {github_handle}
- LinkedIn profile: {linkedin_url}
- Key Goals / OKRs: {okrs}

Existing report draft (JSON):
{draft}

Left-out activity records (JSON):
{records}

Return rows only for the left-out records that are genuinely missing from the draft.""",
        ),
    ]
)


# ---------------------------------------------------------------------------
# Structured output schemas (Sections 2-9)
# ---------------------------------------------------------------------------


class UpstreamContributionRow(BaseModel):
    """Section 2: Open Source & Upstream Contributions."""

    project_repo_contribution: str = Field(
        description="Project / repo / contribution, e.g. 'upstream-project/fleet-operator'."
    )
    what_you_did_and_why: str = Field(
        description="What was done and why it matters: deliverable, milestone, adoption."
    )
    links: str = Field(description="Repo / PR / issue URLs from the records; empty when none.")


class ReusableAssetRow(BaseModel):
    """Section 3: Repeatable Solution Templates & Reusable Assets."""

    template_asset_name: str = Field(description="Template or asset name.")
    what_you_built_status_reuse: str = Field(
        description="What was built, status (draft/live/GA), and who is reusing it."
    )
    links: str = Field(description="Git repo / Google Docs / Slides URLs from the records.")


class CustomerFieldWorkRow(BaseModel):
    """Section 4: Customer-Specific Opportunities & Field Work."""

    customer_segment_opportunity: str = Field(description="Customer / segment / opportunity.")
    activity_and_outcome: str = Field(
        description="Activity and outcome; note closed win, active pipeline, or tech validation."
    )
    links: str = Field(description="CRM / sprint readout / architecture deck URLs from the records.")


class EngagementRow(BaseModel):
    """Section 5: Executive, Partner & Industry Engagements."""

    engagement_event_partner: str = Field(description="Engagement / event / partner.")
    activity_audience_outcome: str = Field(
        description="Activity, audience, and outcome (briefings, reach, joint build, RFE impact)."
    )
    links: str = Field(description="Slides / blog / event / RFE tracker URLs from the records.")


class CustomWorkstreamRow(BaseModel):
    """Section 6: Custom Workstream."""

    initiative_activity_name: str = Field(description="Initiative or activity name.")
    what_you_did_outcome: str = Field(description="Summary, status, and impact.")
    links: str = Field(description="Docs / repos / tickets / notes URLs from the records.")


class ChallengeRow(BaseModel):
    """Section 7: Challenges, Friction & Mitigations."""

    challenge_blocker: str = Field(description="Challenge or blocker.")
    what_happened_and_impact: str = Field(
        description="Root cause and what was delayed or blocked."
    )
    mitigation_next_step_and_links: str = Field(
        description="Action, owner, timeline, and links from the records."
    )


class LookingAheadRow(BaseModel):
    """Section 8: Looking Ahead to Next Quarter."""

    type: Literal["Must-Win Priority", "Emerging Opportunity", "Stop / Deprioritize"] = Field(
        description="Row type."
    )
    initiative_activity: str = Field(description="Initiative or activity.")
    target_outcome_timing_or_capacity: str = Field(
        description="Target outcome, timing, or capacity freed."
    )


class HelpNeededRow(BaseModel):
    """Section 9: Help Needed & Team Radar."""

    category: Literal["Help Needed / Escalation", "Team Heads-Up / Radar"] = Field(
        description="Row category."
    )
    topic_or_request: str = Field(description="Topic or request.")
    owner_target_date_and_links: str = Field(
        description="Who needs to act, by when, and links from the records."
    )


class QuarterReport(BaseModel):
    """Structured output: one list of rows per template section (2-9)."""

    section_2_upstream: list[UpstreamContributionRow] = Field(default_factory=list)
    section_3_assets: list[ReusableAssetRow] = Field(default_factory=list)
    section_4_customers: list[CustomerFieldWorkRow] = Field(default_factory=list)
    section_5_engagements: list[EngagementRow] = Field(default_factory=list)
    section_6_custom: list[CustomWorkstreamRow] = Field(default_factory=list)
    section_6_title: str | None = Field(
        default=None,
        description="Short rename for Section 6 derived from its rows; null when section 6 is empty.",
    )
    section_7_challenges: list[ChallengeRow] = Field(default_factory=list)
    section_8_looking_ahead: list[LookingAheadRow] = Field(default_factory=list)
    section_9_help_needed: list[HelpNeededRow] = Field(default_factory=list)


class DistilledActivity(BaseModel):
    """A compact activity record produced by the chunked pre-summarization step."""

    source: str = Field(description="Source connector: github, linkedin, or docs.")
    date: str | None = Field(description="ISO date (YYYY-MM-DD) when known.")
    title: str = Field(description="Short title of the activity.")
    summary: str = Field(default="", description="One-sentence description with key facts.")
    impact: str = Field(default="", description="Outcome or importance.")
    url: str | None = Field(default=None, description="Source URL when available.")
    raw_context: str = Field(default="", description="Extra grounding context (paths, extra URLs).")


class DistilledActivities(BaseModel):
    """Wrapper for a batch of distilled activity records."""

    activities: list[DistilledActivity] = Field(default_factory=list)


class SynthesisResult(BaseModel):
    """Outcome of the synthesis pipeline, including grounding audit details."""

    report: QuarterReport
    items_received: int = 0
    items_after_dedup: int = 0
    items_for_synthesis: int = 0
    chunked: bool = False
    backfilled_count: int = Field(
        default=0,
        description="Items auto-added to Section 6 by the deterministic fallback (retry failure only).",
    )
    unplaced_items: list[str] = Field(
        default_factory=list,
        description="Titles the retry pass judged already covered by existing rows (audit footer only).",
    )
    grounding_violations: list[str] = Field(default_factory=list)


_ROW_SECTIONS: tuple[str, ...] = (
    "section_2_upstream",
    "section_3_assets",
    "section_4_customers",
    "section_5_engagements",
    "section_6_custom",
    "section_7_challenges",
    "section_8_looking_ahead",
    "section_9_help_needed",
)


# ---------------------------------------------------------------------------
# Public pipeline
# ---------------------------------------------------------------------------


async def synthesize_report(
    profile: TemplateProfile,
    items: list[ActivityItem],
    model: BaseChatModel,
) -> SynthesisResult:
    """Deduplicate *items*, optionally pre-summarize in chunks, and map them to sections."""
    deduped = deduplicate_items(items)
    records = [_compact_item(item) for item in deduped]
    chunked = False

    if records and _records_chars(records) > _CONTEXT_CHAR_BUDGET:
        chunked = True
        chunks = _chunk_records(records, _CHUNK_CHAR_BUDGET)
        summarize_chain: Runnable = SUMMARIZE_PROMPT | model.with_structured_output(
            DistilledActivities
        )
        distilled_batches = await summarize_chain.abatch(
            [{"records": json.dumps(chunk, ensure_ascii=False)} for chunk in chunks]
        )
        records = [
            activity.model_dump()
            for batch in distilled_batches
            for activity in batch.activities
        ]

    report = QuarterReport()
    if records:
        final_chain: Runnable = FINAL_PROMPT | model.with_structured_output(QuarterReport)
        report = await final_chain.ainvoke(_prompt_vars(profile, records))

    report = _drop_empty_rows(report)
    backfilled = 0
    unplaced: list[str] = []
    missing = _find_unrepresented_items(report, deduped)
    if missing:
        # Second chance: the model sees the draft plus the left-out records
        # and places only genuinely missing ones. Items it judges already
        # covered are surfaced in the audit footer — never duplicated as
        # Section 6 rows. The deterministic backfill only engages when the
        # retry pass itself fails.
        retry_ok, still_missing = await _retry_missing_items(report, profile, missing, model)
        if retry_ok:
            unplaced = [_clip(item.title, 100) for item in still_missing]
        else:
            backfilled = _backfill_unrepresented(report, still_missing)
    violations = _validate_and_sanitize_grounding(report, _allowed_urls(deduped))
    return SynthesisResult(
        report=report,
        items_received=len(items),
        items_after_dedup=len(deduped),
        items_for_synthesis=len(records),
        chunked=chunked,
        backfilled_count=backfilled,
        unplaced_items=unplaced,
        grounding_violations=violations,
    )


def deduplicate_items(items: list[ActivityItem]) -> list[ActivityItem]:
    """Deterministic dedup.

    Items carrying a URL dedupe strictly by ``(source, URL)`` — two posts with
    different URLs must never collapse, even when their titles and dates match
    (event-series posts often share an opening line). URL-less items fall back
    to ``(source, normalized title, date, raw_context)``.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[ActivityItem] = []
    for item in items:
        if item.url:
            key = ("url", f"{item.source}:{item.url}")
        else:
            key = (
                "title",
                f"{item.source}:{_normalize_title(item.title)}:{item.date}:{item.raw_context}",
            )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Compaction and chunking
# ---------------------------------------------------------------------------


def _compact_item(item: ActivityItem) -> dict[str, str | None]:
    """Render an ActivityItem as a compact JSON-serializable dict for prompts."""
    return {
        "source": item.source,
        "date": item.date.isoformat() if item.date else None,
        "title": item.title,
        "summary": _clip(item.summary, _SUMMARY_CLIP),
        "impact": _clip(item.impact, _IMPACT_CLIP),
        "url": item.url,
        "raw_context": _clip(item.raw_context, _RAW_CONTEXT_CLIP),
    }


def _records_chars(records: list[dict[str, str | None]]) -> int:
    return sum(len(json.dumps(record, ensure_ascii=False)) for record in records)


def _chunk_records(
    records: list[dict[str, str | None]], budget: int
) -> list[list[dict[str, str | None]]]:
    """Split records into batches whose serialized size stays under *budget* chars."""
    chunks: list[list[dict[str, str | None]]] = []
    current: list[dict[str, str | None]] = []
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


def _prompt_vars(
    profile: TemplateProfile, records: list[dict[str, str | None]]
) -> dict[str, str]:
    """Template variables for the final synthesis prompt."""
    return {
        "name_role": profile.name_role or "unknown",
        "team_geo": profile.team_geo or "unknown",
        "quarter": profile.quarter or "unknown",
        "github_handle": profile.github_handle or "unknown",
        "linkedin_url": profile.linkedin_url or "unknown",
        "okrs": profile.okrs or "not specified",
        "records": json.dumps(records, ensure_ascii=False, indent=1),
    }


# ---------------------------------------------------------------------------
# Grounding guardrail
# ---------------------------------------------------------------------------

_CELL_URL_RE = re.compile(r"https?://[^\s)|]+")
_DOC_PATH_RE = re.compile(r"document: ([^;]+)")
_RAW_URL_RE = re.compile(r"https?://[^\s;,|]+")
_REPO_FRAGMENT_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
#: URLs must share at least this many characters before prefix-matching counts
#: as grounded; short prefixes would match unrelated links on the same host.
_MIN_GROUNDED_URL_PREFIX = 40
_REPO_FRAGMENT_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
#: URLs must share at least this many characters before prefix-matching counts
#: as grounded; short prefixes would match unrelated links on the same host.
_MIN_GROUNDED_URL_PREFIX = 40


def _allowed_urls(items: Iterable[ActivityItem]) -> set[str]:
    """URLs and document paths that may appear in generated report cells.

    Besides each item's canonical URL, every URL embedded in an item's
    ``raw_context`` counts as grounded (per-repo GitHub aggregates list their
    individual PR/issue links there).
    """
    allowed: set[str] = set()
    for item in items:
        if item.url:
            allowed.add(item.url)
        for match in _RAW_URL_RE.finditer(item.raw_context or ""):
            allowed.add(match.group(0).rstrip(".,;:"))
        match = _DOC_PATH_RE.search(item.raw_context or "")
        if match:
            allowed.add(match.group(1).strip())
    return allowed


def _validate_and_sanitize_grounding(report: QuarterReport, allowed: set[str]) -> list[str]:
    """Strip fabricated URLs from report cells; return human-readable violations."""
    violations: list[str] = []
    for row, field_name in _iter_row_fields(report):
        value: str = getattr(row, field_name) or ""
        if not value:
            continue
        sanitized, removed = _sanitize_cell(value, allowed)
        if removed:
            violations.extend(
                f"{type(row).__name__}.{field_name}: removed ungrounded URL {url}"
                for url in removed
            )
            setattr(row, field_name, sanitized)
    return violations


def _sanitize_cell(cell: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove unknown URLs from a cell, keeping grounded ones; also cleans link syntax."""

    removed: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        url = match.group(0)
        if _is_grounded(url, allowed):
            return url
        removed.append(url)
        return ""

    sanitized = _CELL_URL_RE.sub(_replace, cell)
    # Drop dangling markdown link syntax left behind by removed URLs.
    sanitized = re.sub(r"\[([^\]]*)\]\(\s*\)", r"\1", sanitized)
    return " ".join(sanitized.split()), removed


def _is_grounded(url: str, allowed: set[str]) -> bool:
    """Grounded when exact, or when one side is a long-enough prefix of the other.

    Models occasionally truncate very long URLs (LinkedIn activity links run
    ~100 characters) when citing them; a truncated real link shares its prefix
    with an allowed URL, while fabricated links diverge early.
    """
    candidate = url.rstrip(".,;:")
    if candidate in allowed or url in allowed:
        return True
    if len(candidate) < _MIN_GROUNDED_URL_PREFIX:
        return False
    return any(
        len(allowed_url) >= _MIN_GROUNDED_URL_PREFIX
        and (candidate.startswith(allowed_url) or allowed_url.startswith(candidate))
        for allowed_url in allowed
    )


def _iter_row_fields(report: QuarterReport) -> Iterator[tuple[BaseModel, str]]:
    """Yield every ``(row, field_name)`` pair across all report sections."""
    for section in _ROW_SECTIONS:
        for row in getattr(report, section):
            for field_name in type(row).model_fields:
                yield row, field_name


# ---------------------------------------------------------------------------
# Completeness backstop: every gathered item must land somewhere in the report
# ---------------------------------------------------------------------------


def _backfill_unrepresented(report: QuarterReport, missing: list[ActivityItem]) -> int:
    """Append still-missing items to Section 6; return how many were added.

    The contributor prefers misplaced-but-present rows over missing
    information, so anything unrepresented is auto-added to the Custom
    Workstream catch-all, marked for manual re-placement.
    """
    if not missing:
        return 0
    for item in missing:
        doc = _DOC_PATH_RE.search(item.raw_context or "")
        links = item.url or (doc.group(1).strip() if doc else "")
        outcome = _clip(" ".join(part for part in (item.summary, item.impact) if part), 400)
        report.section_6_custom.append(
            CustomWorkstreamRow(
                initiative_activity_name=item.title,
                what_you_did_outcome=f"{outcome} (auto-added; verify section)".strip(),
                links=links,
            )
        )
    if not report.section_6_title:
        report.section_6_title = "Additional Gathered Activity"
    return len(missing)


async def _retry_missing_items(
    report: QuarterReport,
    profile: TemplateProfile,
    missing: list[ActivityItem],
    model: BaseChatModel,
) -> tuple[bool, list[ActivityItem]]:
    """Second chance for left-out items; returns ``(retry_ok, still_missing)``.

    The retry sees the existing draft alongside the left-out records and is
    instructed to create rows only for genuinely missing content — items it
    judges already covered come back as ``still_missing`` for the audit
    footer rather than as duplicate rows.
    """
    try:
        retry_chain: Runnable = RETRY_PROMPT | model.with_structured_output(QuarterReport)
        retry_report = await retry_chain.ainvoke(
            _retry_vars(profile, [_compact_item(item) for item in missing], report)
        )
    except Exception:  # best effort: signal failure so the deterministic backfill engages
        return False, missing
    _merge_report_rows(report, _drop_empty_rows(retry_report))
    return True, _find_unrepresented_items(report, missing)


def _retry_vars(
    profile: TemplateProfile,
    records: list[dict[str, str | None]],
    draft: QuarterReport,
) -> dict[str, str]:
    """Template variables for the retry prompt: profile, left-out records, and the draft."""
    variables = _prompt_vars(profile, records)
    variables["draft"] = draft.model_dump_json(indent=1)
    return variables


def _merge_report_rows(base: QuarterReport, extra: QuarterReport) -> None:
    """Append every row of *extra* into the matching sections of *base*."""
    for section in _ROW_SECTIONS:
        getattr(base, section).extend(getattr(extra, section))
    if not base.section_6_title and extra.section_6_title:
        base.section_6_title = extra.section_6_title


def _find_unrepresented_items(
    report: QuarterReport, items: list[ActivityItem]
) -> list[ActivityItem]:
    """Items whose identifying markers (URLs, doc paths, repo, title) appear nowhere in the report."""
    cell_text = _report_cell_text(report)
    lowered = cell_text.lower()
    cell_urls = [match.group(0) for match in _CELL_URL_RE.finditer(cell_text)]

    missing: list[ActivityItem] = []
    for item in items:
        if not any(
            _marker_present(marker, lowered, cell_urls) for marker in _item_markers(item)
        ):
            missing.append(item)
    return missing


def _item_markers(item: ActivityItem) -> list[str]:
    """Lowercase strings that identify an item inside generated report cells."""
    markers: list[str] = [item.url or ""]
    markers.extend(match.group(0) for match in _RAW_URL_RE.finditer(item.raw_context or ""))
    doc = _DOC_PATH_RE.search(item.raw_context or "")
    if doc:
        markers.append(doc.group(1).strip())
    if item.source == "github":
        fragment = _REPO_FRAGMENT_RE.search(item.title)
        if fragment:
            markers.append(fragment.group(1))
    markers.append(item.title)
    return [marker.lower() for marker in markers if marker]


def _marker_present(marker: str, lowered_cell_text: str, cell_urls: list[str]) -> bool:
    """A marker matches when found verbatim (non-URL) or prefix-tolerantly (URL)."""
    if marker.startswith("http"):
        return any(_is_grounded(cell_url, {marker}) for cell_url in cell_urls)
    return marker in lowered_cell_text


def _report_cell_text(report: QuarterReport) -> str:
    """All generated cell values joined for completeness matching."""
    return "\n".join(
        str(getattr(row, field_name) or "") for row, field_name in _iter_row_fields(report)
    )


def _drop_empty_rows(report: QuarterReport) -> QuarterReport:
    """Remove rows whose every cell is blank."""
    for section in _ROW_SECTIONS:
        rows = getattr(report, section)
        kept = [
            row
            for row in rows
            if any((getattr(row, field) or "").strip() for field in type(row).model_fields)
        ]
        setattr(report, section, kept)
    return report


def _normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"
