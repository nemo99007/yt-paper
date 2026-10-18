"""Text extraction from local PDFs, arXiv, and web pages."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pymupdf
import requests

from . import config
from .schemas import ParsedDocument, SourceType

log = logging.getLogger(__name__)

ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/(?P<id>\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)
ATOM = {"a": "http://www.w3.org/2005/Atom"}


class ParseError(RuntimeError):
    pass


def _http_get(url: str) -> requests.Response:
    try:
        r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        raise ParseError(f"다운로드 실패: {url} ({e})") from e


def _normalize(text: str) -> tuple[str, bool]:
    """Collapse whitespace and cut at MAX_TEXT_CHARS on a word boundary."""
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    limit = config.MAX_TEXT_CHARS
    if len(text) <= limit:
        return text, False
    cut = max(text.rfind(" ", 0, limit), text.rfind("\n", 0, limit))
    cut = cut if cut > limit * 0.9 else limit
    log.warning("본문 %d자 → %d자로 절단 (MAX_TEXT_CHARS)", len(text), cut)
    return text[:cut].rstrip(), True


def _pdf_text(doc: pymupdf.Document) -> tuple[str, str | None, str | None]:
    pages = [page.get_text("text") for page in doc]
    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip() or None
    author = (meta.get("author") or "").strip() or None
    return "\n".join(pages), title, author


def arxiv_id(source: str) -> str | None:
    m = ARXIV_RE.search(source)
    return m.group("id") if m else None


def canonical_source(source: str) -> str:
    """Stable identifier for idempotency, computed without network access."""
    if (aid := arxiv_id(source)) is not None:
        return f"https://arxiv.org/abs/{aid}"
    if source.lower().startswith(("http://", "https://")):
        p = urlparse(source.strip())
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", p.query, ""))
    return Path(source).name


def infer_type(source: str) -> SourceType:
    if arxiv_id(source):
        return "ARXIV"
    if source.lower().startswith(("http://", "https://")):
        return "PAPER" if urlparse(source).path.lower().endswith(".pdf") else "BLOG"
    return "PAPER"


def parse_pdf_file(path: Path, source_type: SourceType) -> ParsedDocument:
    if not path.is_file():
        raise ParseError(f"파일이 없습니다: {path}")
    try:
        with pymupdf.open(path) as doc:
            raw, title, author = _pdf_text(doc)
    except Exception as e:  # pymupdf raises various internal errors
        raise ParseError(f"PDF 파싱 실패: {path} ({e})") from e
    text, truncated = _normalize(raw)
    if not text:
        raise ParseError(f"PDF에서 텍스트를 추출하지 못했습니다 (스캔 이미지일 수 있음): {path}")
    return ParsedDocument(source_url=path.name, source_type=source_type, text=text,
                          title_hint=title, authors_hint=author, truncated=truncated)


def parse_pdf_url(url: str, source_type: SourceType) -> ParsedDocument:
    data = _http_get(url).content
    if not data.startswith(b"%PDF"):
        raise ParseError(f"PDF가 아닙니다: {url}")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            raw, title, author = _pdf_text(doc)
    except Exception as e:
        raise ParseError(f"PDF 파싱 실패: {url} ({e})") from e
    text, truncated = _normalize(raw)
    return ParsedDocument(source_url=canonical_source(url), source_type=source_type, text=text,
                          title_hint=title, authors_hint=author, truncated=truncated)


def parse_arxiv(source: str) -> ParsedDocument:
    aid = arxiv_id(source)
    if aid is None:
        raise ParseError(f"arXiv ID를 찾지 못했습니다: {source}")
    title = authors = abstract = None
    try:
        feed = ET.fromstring(_http_get(f"https://export.arxiv.org/api/query?id_list={aid}").content)
        entry = feed.find("a:entry", ATOM)
        if entry is not None:
            title = " ".join((entry.findtext("a:title", "", ATOM) or "").split()) or None
            names = [n.text for n in entry.findall("a:author/a:name", ATOM) if n.text]
            authors = (", ".join(names) + " (arXiv)") if names else "arXiv"
            abstract = (entry.findtext("a:summary", "", ATOM) or "").strip() or None
    except (ParseError, ET.ParseError) as e:
        log.warning("arXiv 메타데이터 조회 실패: %s", e)

    body = ""
    try:
        body = parse_pdf_url(f"https://arxiv.org/pdf/{aid}", "ARXIV").text
    except ParseError as e:
        log.warning("arXiv PDF 본문 추출 실패, 초록만 사용: %s", e)
    raw = "\n\n".join(x for x in (f"Title: {title}" if title else "", f"Abstract: {abstract}" if abstract else "", body) if x)
    if not raw:
        raise ParseError(f"arXiv 문서에서 텍스트를 얻지 못했습니다: {aid}")
    text, truncated = _normalize(raw)
    return ParsedDocument(source_url=f"https://arxiv.org/abs/{aid}", source_type="ARXIV", text=text,
                          title_hint=title, authors_hint=authors, truncated=truncated)


def parse_web(url: str, source_type: SourceType) -> ParsedDocument:
    import trafilatura  # slow import; only needed for web pages

    html = _http_get(url).text
    try:
        body = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                                   favor_precision=True)
        meta = trafilatura.extract_metadata(html, default_url=url)
    except Exception as e:
        raise ParseError(f"본문 추출 실패: {url} ({e})") from e
    if not body:
        raise ParseError(f"본문을 찾지 못했습니다 (JS 렌더링 페이지일 수 있음): {url}")
    text, truncated = _normalize(body)
    return ParsedDocument(
        source_url=canonical_source(url), source_type=source_type, text=text,
        title_hint=getattr(meta, "title", None), authors_hint=getattr(meta, "author", None) or getattr(meta, "sitename", None),
        truncated=truncated,
    )


def parse_source(source: str, source_type: SourceType | None = None) -> ParsedDocument:
    """Dispatch on the shape of `source` (local PDF, arXiv URL, PDF URL, web URL)."""
    stype = source_type or infer_type(source)
    if arxiv_id(source):
        return parse_arxiv(source)
    if source.lower().startswith(("http://", "https://")):
        if urlparse(source).path.lower().endswith(".pdf"):
            return parse_pdf_url(source, stype)
        return parse_web(source, stype)
    path = Path(source).expanduser()
    if path.suffix.lower() != ".pdf":
        raise ParseError(f"지원하지 않는 입력입니다 (로컬은 .pdf만 지원): {source}")
    return parse_pdf_file(path, stype)


# --- YouTube -----------------------------------------------------------------

YOUTUBE_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\s\"'\]\)]*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)"
    r"(?P<id>[A-Za-z0-9_-]{11})"
)
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_id(text: str) -> str | None:
    """Extract an 11-char video ID from a URL, a markdown link, or a bare ID."""
    text = text.strip()
    if m := YOUTUBE_RE.search(text):
        return m.group("id")
    return text if BARE_ID_RE.match(text) else None


def youtube_title(video_id: str) -> str | None:
    """Look up the video title via oEmbed (no API key needed). None on failure."""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            headers={"User-Agent": config.USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("title")
    except (requests.RequestException, ValueError) as e:
        log.warning("유튜브 제목 조회 실패 (%s): %s", video_id, e)
        return None
