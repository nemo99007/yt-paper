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

    extracted_title: str = Field(description="원문에서 파악한 정확한 제목 (원문 언어 그대로)")
    authors_or_venue: str = Field(description="저자 및 발표 학회/출처. 예: 'Jane Doe, John Roe (DVCon US 2026)'")
    category: Category = Field(description="가장 적합한 기술 카테고리 하나")
    tags: list[str] = Field(description="전문 기술 태그 3~5개 (영문 또는 국문)")
    one_line_summary: str = Field(description="한국어 1줄 핵심 요약")
    key_insights: list[str] = Field(description="엔지니어 관점의 핵심 인사이트 3~5개 (한국어)")
    detailed_summary: str = Field(
        description=(
            "마크다운 상세 요약 (한국어). 반드시 '### 배경 및 문제 정의', '### 핵심 아키텍처 및 방법론', "
            "'### 실험 결과 및 결론' 섹션을 포함. 필요시 $...$ 수식이나 ```mermaid 코드 블록 사용"
        )
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
