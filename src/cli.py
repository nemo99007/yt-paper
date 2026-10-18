"""yt-paper CLI: python -m src.cli {add|import|link|rebuild|list}."""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config, figures, generator, llm, parser, publisher, youtube
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


def _figure(db: Database, res: Resource, pdf: Path | None, number: int | None) -> None:
    """Crop the representative figure into docs/assets/figures/{id}.png."""
    if pdf is None or not pdf.exists():
        return
    rel = f"assets/figures/{res.id}.png"
    try:
        cap = figures.representative(pdf, number, config.DOCS_DIR / rel)
    except RuntimeError as e:
        log.warning("그림 추출 실패: %s (%s)", res.id, e)
        return
    db.set_figure(res.id, rel if cap else None, cap.text if cap else "")


def _ingest(db: Database, source: str, stype: str | None, title: str | None, refresh: bool,
            local_pdf: str | None = None, figure: int | None = None) -> tuple[Resource, bool]:
    """자료를 파싱·요약해 DB에 넣는다. 이미 있으면(refresh 아님) LLM 호출 없이 기존 자료를 돌려준다.

    local_pdf가 주어지면 본문은 로컬 파일에서 읽고, 출처(source_url)는 source URL로 기록한다.
    """
    key = parser.canonical_source(source)
    existing = db.get_by_source(key)
    if existing and not refresh:
        log.info("이미 등록된 자료입니다: %s (%s). 요약을 건너뜁니다.", existing.id, existing.title)
        if figure is not None:
            _figure(db, existing, Path(local_pdf) if local_pdf else None, figure)
        return existing, False
    caps = [c.text for c in figures.captions(Path(local_pdf))] if local_pdf else None
    if local_pdf:
        doc = parser.parse_pdf_file(Path(local_pdf), stype or "PAPER")  # type: ignore[arg-type]
        doc = doc.model_copy(update={"source_url": key})
    else:
        doc = parser.parse_source(source, stype)  # type: ignore[arg-type]
    result = llm.extract(doc, title_hint=title, figure_captions=caps)
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
        "one_line_summary_ko": result.one_line_summary_ko,
        "key_insights_ko": result.key_insights_ko,
        "detailed_summary_ko": result.detailed_summary_ko,
    })
    _figure(db, res, Path(local_pdf) if local_pdf else None,
            figure if figure is not None else result.representative_figure)
    return db.get_resource(res.id) or res, created


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
        res, created = _ingest(db, source, stype, title, refresh)
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


@app.command("import")
def import_(
    csv_path: Path = typer.Option(Path("data/reading_list.csv"), "--csv", help="source,local_pdf,youtube,title,type,timestamp 열을 가진 CSV"),
    limit: Optional[int] = typer.Option(None, "--limit", help="새로 요약할 최대 건수 (시험용)"),
    refresh: bool = typer.Option(False, "--refresh", help="이미 등록된 자료도 다시 요약 (예: 언어/프롬프트 변경 후)"),
    resume: bool = typer.Option(False, "--resume", help="--refresh 중단 후 재개: 한국어 번역이 이미 있는 자료는 건너뜀"),
    push: bool = typer.Option(False, "--push"),
    commit: bool = typer.Option(True, "--commit/--no-commit"),
) -> None:
    """CSV 목록을 한 번에 등록합니다. 이미 등록된 자료는 건너뛰고(영상 매핑만), 실패해도 다음 줄로 계속합니다."""
    if not csv_path.exists():
        _fail(f"CSV 파일이 없습니다: {csv_path}")
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("source") or "").strip()]
    db = Database()
    new = skipped = 0
    failed: list[tuple[str, str]] = []
    try:
        for i, row in enumerate(rows, 1):
            source = row["source"].strip()
            if limit is not None and new >= limit and (refresh or db.get_by_source(parser.canonical_source(source)) is None):
                continue
            stype = (row.get("type") or "").strip().upper() or None
            local_pdf = (row.get("local_pdf") or "").strip() or None
            if local_pdf and not Path(local_pdf).exists():
                local_pdf = None
            try:
                prev = db.get_by_source(parser.canonical_source(source))
                existed = prev is not None
                redo = refresh and not (resume and prev is not None and prev.detailed_summary_ko)
                fig = (row.get("figure") or "").strip()
                res, created = _ingest(db, source, stype, (row.get("title") or "").strip() or None, redo,
                                       local_pdf, int(fig) if fig.isdigit() else None)
                created = created or (redo and existed)
                year = (row.get("year") or "").strip()
                db.set_meta(res.id, (row.get("venue") or "").strip(), (row.get("region") or "").strip(),
                            int(year) if year.isdigit() else None)
                yt_urls = [u for u in (row.get("youtube") or "").split() if u]
                _attach_videos(db, res, yt_urls, (row.get("timestamp") or "").strip() or None)
                generator.refresh(db, db.get_resource(res.id) or res)
                new += created
                skipped += not created
                console.print(f"[{i}/{len(rows)}] {'[green]추가[/green]' if created else '건너뜀'} {res.id} {res.title}")
            except llm.LLMAuthError as e:
                console.print(f"[{i}/{len(rows)}] [red]중단[/red] {e}  (지금까지 결과는 저장하고, 다시 실행하면 이어서 진행)")
                break
            except (parser.ParseError, llm.LLMError, generator.GeneratorError) as e:
                failed.append((source, str(e)))
                console.print(f"[{i}/{len(rows)}] [red]실패[/red] {source}: {e}")
        try:
            n = youtube.sync_video_meta(db, youtube.service(interactive=False))
            log.info("YouTube 업로드 날짜 갱신: %d개", n)
        except Exception as e:  # 로그인 전이면 날짜만 생략
            log.info("YouTube 메타데이터 갱신 생략: %s", e)
        generator.rebuild_all(db)
        console.print(f"[green]완료:[/green] 새로 추가 {new}건, 기존 {skipped}건, 실패 {len(failed)}건")
        for src, err in failed:
            console.print(f"  [red]-[/red] {src}: {err}")
        if commit and (new or skipped):
            _publish(db, f"docs: Import {new} resources from {csv_path.name}", push)
    except DatabaseError as e:
        _fail(str(e))
    finally:
        db.close()


@app.command("yt-auth")
def yt_auth() -> None:
    """YouTube 계정 로그인 (최초 1회). 출력되는 주소를 브라우저에서 엽니다."""
    try:
        yt = youtube.service()
        ch = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
        console.print(f"[green]로그인 완료:[/green] {ch[0]['snippet']['title'] if ch else '(채널 없음)'}")
    except youtube.YouTubeError as e:
        _fail(str(e))


@app.command("yt-meta")
def yt_meta(
    push: bool = typer.Option(False, "--push"),
    commit: bool = typer.Option(True, "--commit/--no-commit"),
) -> None:
    """등록된 영상의 제목·업로드 날짜를 YouTube에서 가져와 페이지를 다시 만듭니다."""
    db = Database()
    try:
        n = youtube.sync_video_meta(db, youtube.service(interactive=False))
        generator.rebuild_all(db)
        console.print(f"[green]영상 정보 갱신:[/green] {n}개")
        if commit:
            _publish(db, "docs: Sync video titles and upload dates", push)
    except (youtube.YouTubeError, DatabaseError, generator.GeneratorError) as e:
        _fail(str(e))
    finally:
        db.close()


@app.command("yt-desc")
def yt_desc(
    video: list[str] = typer.Option([], "--video", "-v", help="특정 영상 ID만 (여러 번 지정 가능)"),
    apply_: bool = typer.Option(False, "--apply", help="실제로 YouTube에 반영 (없으면 미리보기만)"),
    show: int = typer.Option(1, "--show", help="미리보기로 전체 설명란을 출력할 개수"),
) -> None:
    """유튜브 설명란 맨 위에 저자·요약·태그·링크 블록을 넣습니다 (기존 설명은 유지)."""
    db = Database()
    try:
        yt = youtube.service(interactive=False)
        plans = youtube.plans(db, yt, only=[parser.youtube_id(v) or v for v in video] or None)
        todo = [p for p in plans if p.changed]
        console.print(f"대상 영상 {len(plans)}개 중 변경 {len(todo)}개")
        for p in todo[:show]:
            console.rule(f"{p.video_id}  {p.title}")
            console.print(p.new, markup=False, highlight=False)
        if not apply_:
            console.print("[yellow]미리보기만 했습니다. 반영하려면 --apply 를 붙이세요.[/yellow]")
            return
        ok = 0
        for p in todo:
            try:
                youtube.apply(yt, p)
                ok += 1
                console.print(f"[green]반영[/green] {p.video_id} {p.title}")
            except youtube.YouTubeError as e:
                console.print(f"[red]실패[/red] {e}")
                if "quota" in str(e).lower():
                    break
        console.print(f"[green]완료:[/green] {ok}/{len(todo)}개 반영")
    except youtube.YouTubeError as e:
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
