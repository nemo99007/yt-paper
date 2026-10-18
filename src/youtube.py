"""YouTube Data API v3: write archive info (authors, summary, links) into video descriptions.

The managed part sits between two marker lines at the top of the description; text outside
the markers (written by the channel owner) is never touched, and re-running only replaces
what is between them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .database import Database
from .schemas import Resource, YouTubeVideo

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
MARK = "── yt-paper ──"
MAX_DESC = 5000
_BLOCK_RE = re.compile(rf"{re.escape(MARK)}.*?{re.escape(MARK)}\n*", re.S)
_YT_ID_RE = re.compile(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


class YouTubeError(RuntimeError):
    pass


def _client_secret() -> Path:
    files = sorted(config.SECRETS_DIR.glob("client_secret*.json"))
    if not files:
        raise YouTubeError(f"client_secret*.json이 없습니다: {config.SECRETS_DIR}")
    return files[0]


def service(interactive: bool = True):
    """Authorized YouTube client. First run opens a browser consent flow and caches the token."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if config.YT_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(config.YT_TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            log.warning("저장된 YouTube 토큰이 만료되었습니다. 다시 로그인합니다.")
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise YouTubeError("YouTube 로그인이 필요합니다: python -m src.cli yt-auth")
        flow = InstalledAppFlow.from_client_secrets_file(str(_client_secret()), SCOPES)
        # WSL에는 브라우저가 없으므로 URL만 출력 → Windows 브라우저에서 열면 localhost로 돌아온다.
        creds = flow.run_local_server(host="localhost", port=config.YT_OAUTH_PORT, open_browser=False,
                                      authorization_prompt_message="아래 주소를 브라우저에서 열어 로그인하세요:\n{url}\n")
        config.YT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.YT_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        config.YT_TOKEN_PATH.chmod(0o600)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def sync_video_meta(db: Database, yt) -> int:
    """Fill title and upload date (publishedAt) of every stored video from the API (1 unit per 50 videos)."""
    videos = db.list_videos()
    n = 0
    for i in range(0, len(videos), 50):
        chunk = {v.video_id: v for v in videos[i:i + 50]}
        resp = yt.videos().list(part="snippet", id=",".join(chunk), maxResults=50).execute()
        for item in resp.get("items", []):
            sn = item["snippet"]
            v = chunk[item["id"]]
            if v.published_at != sn.get("publishedAt") or v.video_title != sn.get("title"):
                db.upsert_video(YouTubeVideo(video_id=v.video_id, video_url=v.video_url,
                                             video_title=sn.get("title"), published_at=sn.get("publishedAt")))
                n += 1
    return n


def site_url(md_path: str, anchor: str = "") -> str:
    """docs-relative .md path -> public URL (MkDocs directory URLs)."""
    path = md_path.removesuffix(".md")
    path = "" if path == "index" else path.removesuffix("/index") + "/"
    return config.SITE_URL.rstrip("/") + "/" + path + (f"#{anchor}" if anchor else "")


def page_url(res: Resource) -> str | None:
    if not res.markdown_path or not config.SITE_URL:
        return None
    return site_url(res.markdown_path)


def hierarchy_links(res: Resource) -> list[str]:
    """Links from a video to the collection pages its paper belongs to."""
    from . import generator as g

    venue = g.venue_of(res)
    vs = g.venue_slug(venue)
    out = [f"📚 {venue}: {site_url(f'{vs}/index.md')}"]
    if res.region:
        label = " ".join(x for x in (venue, res.region, str(res.year or "")) if x)
        out.append(f"📅 {label}: {site_url(f'{vs}/{g.region_slug(res.region)}.md', str(res.year or ''))}")
    out.append(f"🏷 {venue} · {res.category}: {site_url(f'{vs}/by-topic.md', g.slugify(res.category))}")
    return out


def _clean(text: str) -> str:
    """YouTube rejects '<' and '>' in descriptions; also flatten markdown links/emphasis."""
    text = _MD_LINK_RE.sub(r"\1: \2", text)
    text = text.replace("**", "").replace("`", "")
    return text.replace("<", "‹").replace(">", "›")


def _hashtag(tag: str) -> str:
    return "#" + re.sub(r"[^0-9A-Za-z가-힣]", "", tag)


@dataclass
class Plan:
    video_id: str
    title: str
    old: str
    new: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


def build_block(res: Resource, extra_links: list[str], insights: int = 5) -> str:
    lines = [MARK]
    lines += extra_links
    if url := page_url(res):
        lines.append(f"📄 Summary: {url}")
    if res.source_url.startswith("http"):
        lines.append(f"📑 Paper: {res.source_url}")
    lines += hierarchy_links(res)
    lines.append("")
    lines.append(res.title)
    if res.authors:
        lines.append(f"👤 {res.authors}")
    if res.one_line_summary:
        lines += ["", f"📝 {res.one_line_summary}"]
    if res.key_insights and insights:
        lines += ["", "💡 Key insights"] + [f"• {k}" for k in res.key_insights[:insights]]
    if res.tags:
        lines += ["", " ".join(_hashtag(t) for t in res.tags if _hashtag(t) != "#")]
    lines.append(MARK)
    return _clean("\n".join(lines))


def merge(old: str, block: str) -> str:
    body = _BLOCK_RE.sub("", old).strip()
    return f"{block}\n\n{body}".strip() if body else block


def plans(db: Database, yt, only: list[str] | None = None) -> list[Plan]:
    """Compute new descriptions for every video linked to a resource (plus 'previous reading' videos)."""
    targets: dict[str, tuple[Resource, list[str]]] = {}
    for res in db.list_resources():
        for link in db.videos_for(res.id):
            vid = link.video.video_id
            extra: list[str] = []
            for label, url in _MD_LINK_RE.findall(link.timestamp_note or ""):
                if m := _YT_ID_RE.search(url):
                    prev = m.group(1)
                    extra.append(f"▶ {label}: https://youtu.be/{prev}")
                    targets.setdefault(prev, (res, [f"▶ Latest reading: https://youtu.be/{vid}"]))
            targets[vid] = (res, extra)
    if only:
        targets = {k: v for k, v in targets.items() if k in only}

    out: list[Plan] = []
    ids = list(targets)
    for i in range(0, len(ids), 50):  # videos.list: 50 ids per call, 1 quota unit
        resp = yt.videos().list(part="snippet", id=",".join(ids[i:i + 50]), maxResults=50).execute()
        for item in resp.get("items", []):
            vid, sn = item["id"], item["snippet"]
            res, extra = targets[vid]
            old = sn.get("description", "")
            for n in (5, 3, 1, 0):
                new = merge(old, build_block(res, extra, insights=n))
                if len(new) <= MAX_DESC:
                    break
            else:
                log.warning("설명란이 %d자를 넘어 건너뜁니다: %s", MAX_DESC, vid)
                continue
            out.append(Plan(vid, sn.get("title", ""), old, new))
        missing = set(ids[i:i + 50]) - {it["id"] for it in resp.get("items", [])}
        for vid in missing:
            log.warning("영상을 찾을 수 없거나 권한이 없습니다: %s", vid)
    return out


def apply(yt, plan: Plan) -> None:
    """videos.update replaces the whole snippet, so re-send title/category/tags with the new description."""
    from googleapiclient.errors import HttpError

    try:
        cur = yt.videos().list(part="snippet", id=plan.video_id).execute()["items"][0]["snippet"]
        snippet = {k: cur[k] for k in ("title", "categoryId", "tags", "defaultLanguage", "defaultAudioLanguage")
                   if k in cur}
        snippet["description"] = plan.new
        yt.videos().update(part="snippet", body={"id": plan.video_id, "snippet": snippet}).execute()
    except HttpError as e:
        raise YouTubeError(f"{plan.video_id}: {e.status_code} {e.reason}") from e
    except (KeyError, IndexError) as e:
        raise YouTubeError(f"{plan.video_id}: 영상 정보를 읽지 못했습니다") from e
