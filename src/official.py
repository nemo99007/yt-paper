"""Find each DVCon paper's official presentation on the Accellera YouTube channel.

Accellera uploads talks either one per video or as session videos whose description lists each
talk's title followed by its start time ("1) Title" / "29:53"). We match paper titles against both.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

from .database import Database

log = logging.getLogger(__name__)

CHANNEL_HANDLE = "Accellera"
MIN_RATIO = 0.8  # the best wrong candidate scored 0.74; true matches with reworded titles reach 0.85+
_TS = re.compile(r"^\(?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\)?$")
_INLINE = re.compile(r"^(?:\d+\)\s*)?(.{15,}?)\s*[-–]?\s*\(?(\d{1,2}:\d{2}(?::\d{2})?)\)?$")


@dataclass
class Talk:
    video_id: str
    video_title: str
    playlist: str
    title: str
    start: int  # seconds

    @property
    def url(self) -> str:
        return f"https://youtu.be/{self.video_id}" + (f"?t={self.start}" if self.start else "")

    @property
    def label(self) -> str:
        where = self.playlist or "Accellera"
        return f"{where} · {self.video_title} · {hms(self.start)}" if self.start else where


@dataclass
class Match:
    resource_id: str
    paper_title: str
    talk: Talk
    ratio: float


def hms(sec: int) -> str:
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _seconds(ts: str) -> int:
    p = [int(x) for x in ts.split(":")]
    return p[-1] + 60 * p[-2] + (3600 * p[-3] if len(p) == 3 else 0)


def _norm(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t.lower()).split())


def _items(yt, playlist_id: str) -> list[dict]:
    out, tok = [], None
    while True:
        r = yt.playlistItems().list(part="snippet", playlistId=playlist_id, maxResults=50, pageToken=tok).execute()
        out += [i["snippet"] for i in r["items"]]
        tok = r.get("nextPageToken")
        if not tok:
            return out


def fetch_talks(yt) -> list[Talk]:
    """All talks on the channel: DVCon playlists (for the event name) plus every upload."""
    ch = yt.channels().list(part="contentDetails", forHandle=CHANNEL_HANDLE).execute()["items"][0]
    channel_id = yt.channels().list(part="id", forHandle=CHANNEL_HANDLE).execute()["items"][0]["id"]
    videos: dict[str, dict] = {}
    tok = None
    while True:
        r = yt.playlists().list(part="snippet", channelId=channel_id, maxResults=50, pageToken=tok).execute()
        for pl in r["items"]:
            name = pl["snippet"]["title"]
            if "DVCon" in name and "Promotion" not in name:
                for sn in _items(yt, pl["id"]):
                    videos.setdefault(sn["resourceId"]["videoId"], {**sn, "playlist": name})
        tok = r.get("nextPageToken")
        if not tok:
            break
    for sn in _items(yt, ch["contentDetails"]["relatedPlaylists"]["uploads"]):
        videos.setdefault(sn["resourceId"]["videoId"], {**sn, "playlist": ""})
    talks = []
    for vid, sn in videos.items():
        base = dict(video_id=vid, video_title=sn["title"], playlist=sn["playlist"])
        talks.append(Talk(**base, title=sn["title"], start=0))
        lines = [l.strip() for l in sn.get("description", "").splitlines()]
        for i, line in enumerate(lines):
            if m := _TS.match(line):  # "1) Title" on an earlier line, "By ..." lines in between
                j = i - 1
                while j >= 0 and (not lines[j] or lines[j].lower().startswith("by ")):
                    j -= 1
                if j >= 0:
                    talks.append(Talk(**base, title=re.sub(r"^\d+\)\s*", "", lines[j]), start=_seconds(m.group(1))))
            elif m := _INLINE.match(line):  # "Title 12:34" on one line
                talks.append(Talk(**base, title=m.group(1), start=_seconds(m.group(2))))
    log.info("Accellera: 영상 %d개, 발표 후보 %d개", len(videos), len(talks))
    return talks


def match(db: Database, talks: list[Talk]) -> list[Match]:
    normed = [(_norm(t.title), t) for t in talks]
    out = []
    for res in db.list_resources():
        if res.venue != "DVCon":
            continue
        nt = _norm(res.title)
        ratio, talk = max(((difflib.SequenceMatcher(None, nt, n).ratio(), t) for n, t in normed),
                          key=lambda x: x[0], default=(0.0, None))
        if talk and ratio >= MIN_RATIO:
            out.append(Match(res.id, res.title, talk, ratio))
    return out
