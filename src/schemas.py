"""Pydantic models: LLM output schema and DB DTOs."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["PAPER", "ARXIV", "BOOK", "BLOG"]
Category = Literal["Emulation", "Verification", "RISC-V", "Architecture", "EDA", "System-SW", "General"]
CATEGORIES: tuple[str, ...] = ("Emulation", "Verification", "RISC-V", "Architecture", "EDA", "System-SW", "General")
SOURCE_TYPES: tuple[str, ...] = ("PAPER", "ARXIV", "BOOK", "BLOG")


class LLMExtractionResult(BaseModel):
    """Structured summary Claude must return for one document."""

    model_config = ConfigDict(extra="forbid")

    extracted_title: str = Field(description="Exact title as written in the document")
    authors_or_venue: str = Field(description="Only the authors with affiliations, e.g. 'Jane Doe (Samsung), John Roe (Siemens EDA)'; no comments")
    category: Category = Field(description="The single best-fitting technical category")
    tags: list[str] = Field(description="3-5 short technical keywords in English")
    one_line_summary: str = Field(description="One-sentence English summary of the core contribution")
    key_insights: list[str] = Field(description="3-5 key takeaways for engineers, in English")
    detailed_summary: str = Field(
        description=(
            "Detailed Markdown summary in English with the sections '### Background and Problem', "
            "'### Architecture and Methodology', '### Results and Conclusions'. Use $...$ math or "
            "```mermaid blocks where helpful"
        )
    )
    one_line_summary_ko: str = Field(description="Korean translation of one_line_summary")
    key_insights_ko: list[str] = Field(description="Korean translation of key_insights, same order and count")
    detailed_summary_ko: str = Field(
        description=(
            "Korean translation of detailed_summary with the sections '### 배경 및 문제', "
            "'### 구조 및 방법론', '### 결과 및 결론' in the same order. Keep technical terms in English. "
            "Do not repeat mermaid blocks (they are shown once from the English version)"
        )
    )
    representative_figure: int | None = Field(
        description="Number N of the figure (from the provided 'Figure N' caption list) that best represents "
                    "the paper's architecture or method. Prefer a block diagram, architecture, flow or key-result "
                    "chart; avoid code listings, tables, screenshots and waveforms unless nothing else exists. "
                    "Null if no caption list is given"
    )


class ParsedDocument(BaseModel):
    """Text and metadata pulled from a source before summarization."""

    source_url: str
    source_type: SourceType
    text: str
    title_hint: str | None = None
    authors_hint: str | None = None
    truncated: bool = False


class Resource(BaseModel):
    id: str
    title: str
    source_type: SourceType
    source_url: str
    authors: str = ""
    category: str = "General"
    tags: list[str] = Field(default_factory=list)
    one_line_summary: str = ""
    key_insights: list[str] = Field(default_factory=list)
    detailed_summary: str = ""
    one_line_summary_ko: str = ""
    key_insights_ko: list[str] = Field(default_factory=list)
    detailed_summary_ko: str = ""
    figure_path: str | None = None     # relative to docs/, e.g. assets/figures/ref-xxx.png
    figure_caption: str = ""
    venue: str = ""          # e.g. "DVCon", "DATE" ("" → Others)
    region: str = ""         # e.g. "US", "Europe", "India"
    year: int | None = None
    markdown_path: str | None = None
    created_at: str = ""


class YouTubeVideo(BaseModel):
    video_id: str
    video_url: str
    video_title: str | None = None
    published_at: str | None = None


class VideoLink(BaseModel):
    """A video attached to a resource (one row of resource_video_map)."""

    video: YouTubeVideo
    timestamp_note: str | None = None


class ResourceLink(BaseModel):
    """A resource attached to a video (reverse direction)."""

    resource: Resource
    timestamp_note: str | None = None
