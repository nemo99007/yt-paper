"""yt-paper CLI: python -m src.cli {add|link|rebuild|list}."""
from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config, generator, llm, parser, publisher
from .database import Database, DatabaseError
from .schemas import CATEGORIES, SOURCE_TYPES, Resource, YouTubeVideo

app = typer.Typer(add_completion=False, help="기술 자료 요약 + 유튜브 매핑 + MkDocs 퍼블리싱")
console = Console()
log = logging.getLogger("yt-paper")


def _fail(msg: str) -> None:
    console.print(f"[red]오류:[/red] {msg}")
    raise typer.Exit(1)


def _attach_videos(db: Database, res: Resource, urls: list[str], timestamp: str | None) -> int:
    added = 0
    for raw in urls:
        vid = parser.youtube_id(raw)
        if vid is None:
            log.warning("유튜브 영상 ID를 찾지 못했습니다: %s", raw)
            continue
        if db.get_video(vid) is None:
            db.upsert_video(YouTubeVideo(video_id=vid, video_url=f"https://www.youtube.com/watch?v={vid}",
                                         video_title=parser.youtube_title(vid)))
        if db.link(res.id, vid, timestamp):
            added += 1
            log.info("영상 연결: %s ↔ %s", res.id, vid)
    return added


def _publish(db: Database, message: str, push: bool) -> None:
    if not publisher.publish(db, message, push=push):
        raise typer.Exit(2)


@app.command()
def add(
    source: str = typer.Option(..., "--source", "-s", help="로컬 PDF 경로, arXiv URL, PDF URL, 웹 URL"),
    type_: Optional[str] = typer.Option(None, "--type", "-t", help="PAPER|ARXIV|BOOK|BLOG (생략 시 자동 판별)"),
    youtube: list[str] = typer.Option([], "--youtube", "-y", help="유튜브 URL (여러 번 지정 가능)"),
    title: Optional[str] = typer.Option(None, "--title", help="제목 힌트"),
    timestamp: Optional[str] = typer.Option(None, "--timestamp", help="영상 내 시간/메모 (예: 04:15)"),
    refresh: bool = typer.Option(False, "--refresh", help="이미 등록된 자료도 다시 요약"),
    push: bool = typer.Option(False, "--push", help="커밋 후 git push"),
    commit: bool = typer.Option(True, "--commit/--no-commit", help="Git 커밋 여부"),
) -> None:
    """새 자료를 수집·요약하고 마크다운을 생성합니다 (같은 출처면 영상 매핑만 추가)."""
    stype = type_.upper() if type_ else None
    if stype and stype not in SOURCE_TYPES:
        _fail(f"--type은 {', '.join(SOURCE_TYPES)} 중 하나여야 합니다.")
    db = Database()
    try:
        key = parser.canonical_source(source)
        existing = db.get_by_source(key)
        if existing and not refresh:
            log.info("이미 등록된 자료입니다: %s (%s). 요약을 건너뛰고 영상 매핑만 처리합니다.", existing.id, existing.title)
            res, created = existing, False
        else:
            doc = parser.parse_source(source, stype)  # type: ignore[arg-type]
            result = llm.extract(doc, title_hint=title)
            res, created = db.upsert_resource({
                "title": result.extracted_title or title or doc.title_hint or key,
                "source_type": stype or doc.source_type,
                "source_url": doc.source_url,
                "authors": result.authors_or_venue or doc.authors_hint or "",
                "category": result.category,
                "tags": result.tags,
                "one_line_summary": result.one_line_summary,
                "key_insights": result.key_insights,
                "detailed_summary": result.detailed_summary,
            })
        _attach_videos(db, res, youtube, timestamp)
        res = db.get_resource(res.id) or res
        generator.refresh(db, res)
        res = db.get_resource(res.id) or res
        console.print(f"[green]{'추가' if created else '업데이트'}:[/green] {res.id}  {res.title}\n"
                      f"  → docs/{res.markdown_path}")
        if commit:
            _publish(db, f"docs({res.category}): {'Add' if created else 'Update'} '{res.title}'", push)
    except (parser.ParseError, llm.LLMError, DatabaseError, generator.GeneratorError) as e:
        _fail(str(e))
    finally:
        db.close()


@app.command()
def link(
    ref_id: str = typer.Option(..., "--ref-id", "-r", help="자료 ID (예: ref-20261018-a1b2)"),
    youtube: list[str] = typer.Option(..., "--youtube", "-y", help="유튜브 URL (여러 번 지정 가능)"),
    timestamp: Optional[str] = typer.Option(None, "--timestamp", help="영상 내 시간/메모 (예: 05:30)"),
    push: bool = typer.Option(False, "--push"),
    commit: bool = typer.Option(True, "--commit/--no-commit"),
) -> None:
    """등록된 자료에 유튜브 영상을 연결하고 페이지를 다시 만듭니다."""
    db = Database()
    try:
        res = db.get_resource(ref_id)
        if res is None:
            _fail(f"자료를 찾을 수 없습니다: {ref_id}  (python -m src.cli list 로 ID 확인)")
        n = _attach_videos(db, res, youtube, timestamp)
        generator.refresh(db, db.get_resource(ref_id))
        console.print(f"[green]영상 {n}개 새로 연결:[/green] {res.id}  {res.title}")
        if commit:
            _publish(db, f"docs({res.category}): Update '{res.title}'", push)
    except (DatabaseError, generator.GeneratorError) as e:
        _fail(str(e))
    finally:
        db.close()


@app.command()
def rebuild(
    push: bool = typer.Option(False, "--push"),
    commit: bool = typer.Option(True, "--commit/--no-commit"),
) -> None:
    """DB 기준으로 모든 마크다운과 인덱스를 다시 생성합니다."""
    db = Database()
    try:
        n = generator.rebuild_all(db)
        console.print(f"[green]재생성 완료:[/green] 자료 {n}건")
        if commit:
            _publish(db, "docs: Rebuild all pages", push)
    except (DatabaseError, generator.GeneratorError) as e:
        _fail(str(e))
    finally:
        db.close()


@app.command("list")
def list_(
    category: Optional[str] = typer.Option(None, "--category", "-c", help=" | ".join(CATEGORIES)),
    tag: Optional[str] = typer.Option(None, "--tag"),
    type_: Optional[str] = typer.Option(None, "--type", "-t"),
) -> None:
    """로컬 DB를 검색합니다."""
    db = Database()
    try:
        rows = db.list_resources(category=category, tag=tag, source_type=type_)
        table = Table(title=f"자료 {len(rows)}건")
        for col in ("ID", "날짜", "카테고리", "유형", "제목", "태그", "영상"):
            table.add_column(col, overflow="fold")
        for r in rows:
            table.add_row(r.id, r.created_at[:10], r.category, r.source_type, r.title,
                          ", ".join(r.tags), str(len(db.videos_for(r.id))))
        console.print(table)
    finally:
        db.close()


@app.callback()
def _main() -> None:
    config.setup_logging()


if __name__ == "__main__":
    app()
