"""SQLite storage with an automatic JSON dump after every change.

`data/archive.db` may be left out of Git; when it is missing, the database is
rebuilt from `data/index_dump.json` (this is how GitHub Actions runs work).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config
from .schemas import Resource, ResourceLink, VideoLink, YouTubeVideo

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT UNIQUE,
    authors TEXT,
    category TEXT NOT NULL,
    tags TEXT,
    one_line_summary TEXT,
    key_insights TEXT,
    detailed_summary TEXT,
    markdown_path TEXT,
    created_at TIMESTAMP,
    venue TEXT,
    region TEXT,
    year INTEGER,
    one_line_summary_ko TEXT,
    key_insights_ko TEXT,
    detailed_summary_ko TEXT,
    figure_path TEXT,
    figure_caption TEXT
);
CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id TEXT PRIMARY KEY,
    video_url TEXT NOT NULL,
    video_title TEXT,
    published_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resource_video_map (
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
    timestamp_note TEXT,
    PRIMARY KEY (resource_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_resources_category ON resources(category);
"""

RESOURCE_COLS = ("id", "title", "source_type", "source_url", "authors", "category", "tags",
                 "one_line_summary", "key_insights", "detailed_summary", "markdown_path", "created_at",
                 "venue", "region", "year", "one_line_summary_ko", "key_insights_ko",
                 "detailed_summary_ko", "figure_path", "figure_caption")
JSON_COLS = ("tags", "key_insights", "key_insights_ko")
# Columns added after the first release; ALTER TABLE adds them to older databases.
_MIGRATIONS = {"venue": "TEXT", "region": "TEXT", "year": "INTEGER", "one_line_summary_ko": "TEXT",
               "key_insights_ko": "TEXT", "detailed_summary_ko": "TEXT", "figure_path": "TEXT",
               "figure_caption": "TEXT"}


class DatabaseError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _row_to_resource(row: sqlite3.Row) -> Resource:
    d = dict(row)
    for k in JSON_COLS:
        d[k] = json.loads(d.get(k) or "[]")
    d["authors"] = d.get("authors") or ""
    for k in ("one_line_summary", "detailed_summary", "created_at", "venue", "region",
              "one_line_summary_ko", "detailed_summary_ko", "figure_caption"):
        d[k] = d.get(k) or ""
    return Resource(**d)


class Database:
    def __init__(self, path: Path = config.DB_PATH, dump_path: Path = config.DUMP_PATH) -> None:
        self.path = path
        self.dump_path = dump_path
        fresh = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(path)
        except sqlite3.Error as e:
            raise DatabaseError(f"DB를 열 수 없습니다: {path} ({e})") from e
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(resources)")}
        for col, typ in _MIGRATIONS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE resources ADD COLUMN {col} {typ}")
        if fresh and dump_path.exists():
            self.import_from_json(dump_path)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error, then refresh the JSON dump."""
        try:
            with self.conn:
                yield self.conn
        except sqlite3.Error as e:
            raise DatabaseError(f"DB 쓰기 실패: {e}") from e
        self.export_to_json(self.dump_path)

    # --- resources -----------------------------------------------------------

    def _new_id(self, source_url: str) -> str:
        digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()
        day = datetime.now().astimezone().strftime("%Y%m%d")
        for n in range(4, len(digest) + 1):
            rid = f"ref-{day}-{digest[:n]}"
            if self.get_resource(rid) is None:
                return rid
        raise DatabaseError("ID 생성 실패")

    def get_resource(self, resource_id: str) -> Resource | None:
        row = self.conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
        return _row_to_resource(row) if row else None

    def get_by_source(self, source_url: str) -> Resource | None:
        row = self.conn.execute("SELECT * FROM resources WHERE source_url = ?", (source_url,)).fetchone()
        return _row_to_resource(row) if row else None

    def upsert_resource(self, data: dict[str, Any]) -> tuple[Resource, bool]:
        """Insert or update by source_url. Returns (resource, created)."""
        existing = self.get_by_source(data["source_url"])
        rec = existing.model_dump() if existing else {"id": self._new_id(data["source_url"]), "created_at": _now()}
        rec.update({k: v for k, v in data.items() if k in RESOURCE_COLS and k not in ("id", "created_at")})
        res = Resource(**rec)
        row = res.model_dump()
        for k in JSON_COLS:
            row[k] = json.dumps(row[k], ensure_ascii=False)
        with self.transaction() as c:
            c.execute(
                f"INSERT INTO resources ({','.join(RESOURCE_COLS)}) VALUES ({','.join('?' * len(RESOURCE_COLS))}) "
                "ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in RESOURCE_COLS if k != "id"),
                [row[k] for k in RESOURCE_COLS],
            )
        return res, existing is None

    def set_meta(self, resource_id: str, venue: str, region: str, year: int | None) -> None:
        with self.transaction() as c:
            c.execute("UPDATE resources SET venue = ?, region = ?, year = ? WHERE id = ?",
                      (venue, region, year, resource_id))

    def set_figure(self, resource_id: str, path: str | None, caption: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE resources SET figure_path = ?, figure_caption = ? WHERE id = ?",
                      (path, caption, resource_id))

    def set_markdown_path(self, resource_id: str, rel_path: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE resources SET markdown_path = ? WHERE id = ?", (rel_path, resource_id))

    def list_resources(self, category: str | None = None, tag: str | None = None,
                       source_type: str | None = None) -> list[Resource]:
        sql, args = "SELECT * FROM resources WHERE 1=1", []
        if category:
            sql += " AND lower(category) = lower(?)"
            args.append(category)
        if source_type:
            sql += " AND source_type = ?"
            args.append(source_type.upper())
        rows = [_row_to_resource(r) for r in self.conn.execute(sql + " ORDER BY created_at DESC, id", args)]
        if tag:
            t = tag.lower()
            rows = [r for r in rows if any(t == x.lower() for x in r.tags)]
        return rows

    # --- videos --------------------------------------------------------------

    def upsert_video(self, video: YouTubeVideo) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO youtube_videos (video_id, video_url, video_title, published_at) VALUES (?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET video_url=excluded.video_url, "
                "video_title=COALESCE(excluded.video_title, youtube_videos.video_title), "
                "published_at=COALESCE(excluded.published_at, youtube_videos.published_at)",
                (video.video_id, video.video_url, video.video_title, video.published_at),
            )

    def get_video(self, video_id: str) -> YouTubeVideo | None:
        row = self.conn.execute("SELECT * FROM youtube_videos WHERE video_id = ?", (video_id,)).fetchone()
        return YouTubeVideo(**dict(row)) if row else None

    def link(self, resource_id: str, video_id: str, timestamp_note: str | None = None) -> bool:
        """Map a video to a resource. Returns True if the mapping is new."""
        if self.get_resource(resource_id) is None:
            raise DatabaseError(f"자료를 찾을 수 없습니다: {resource_id}")
        new = self.conn.execute("SELECT 1 FROM resource_video_map WHERE resource_id=? AND video_id=?",
                                (resource_id, video_id)).fetchone() is None
        with self.transaction() as c:
            c.execute(
                "INSERT INTO resource_video_map (resource_id, video_id, timestamp_note) VALUES (?,?,?) "
                "ON CONFLICT(resource_id, video_id) DO UPDATE SET "
                "timestamp_note=COALESCE(excluded.timestamp_note, resource_video_map.timestamp_note)",
                (resource_id, video_id, timestamp_note),
            )
        return new

    def videos_for(self, resource_id: str) -> list[VideoLink]:
        rows = self.conn.execute(
            "SELECT v.*, m.timestamp_note FROM resource_video_map m JOIN youtube_videos v USING(video_id) "
            "WHERE m.resource_id = ? ORDER BY v.video_id", (resource_id,))
        out = []
        for r in rows:
            d = dict(r)
            note = d.pop("timestamp_note")
            out.append(VideoLink(video=YouTubeVideo(**d), timestamp_note=note))
        return out

    def resources_for(self, video_id: str) -> list[ResourceLink]:
        rows = self.conn.execute(
            "SELECT r.*, m.timestamp_note AS _note FROM resource_video_map m JOIN resources r ON r.id = m.resource_id "
            "WHERE m.video_id = ? ORDER BY r.created_at DESC", (video_id,))
        out = []
        for r in rows:
            d = dict(r)
            note = d.pop("_note")
            out.append(ResourceLink(resource=_row_to_resource(d), timestamp_note=note))
        return out

    def list_videos(self) -> list[YouTubeVideo]:
        return [YouTubeVideo(**dict(r)) for r in self.conn.execute("SELECT * FROM youtube_videos ORDER BY video_id")]

    # --- JSON dump -----------------------------------------------------------

    def export_to_json(self, path: Path | str = config.DUMP_PATH) -> Path:
        path = Path(path)
        data = {
            "resources": [r.model_dump() for r in self.list_resources()],
            "youtube_videos": [v.model_dump() for v in self.list_videos()],
            "resource_video_map": [dict(r) for r in self.conn.execute(
                "SELECT resource_id, video_id, timestamp_note FROM resource_video_map ORDER BY resource_id, video_id")],
        }
        data["resources"].sort(key=lambda r: r["id"])
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            raise DatabaseError(f"JSON 덤프 저장 실패: {path} ({e})") from e
        return path

    def import_from_json(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DatabaseError(f"JSON 덤프를 읽을 수 없습니다: {path} ({e})") from e
        with self.conn:
            for r in data.get("resources", []):
                r = Resource(**r).model_dump()
                for k in JSON_COLS:
                    r[k] = json.dumps(r[k], ensure_ascii=False)
                self.conn.execute(f"INSERT OR REPLACE INTO resources ({','.join(RESOURCE_COLS)}) "
                                  f"VALUES ({','.join('?' * len(RESOURCE_COLS))})", [r[k] for k in RESOURCE_COLS])
            for v in data.get("youtube_videos", []):
                v = YouTubeVideo(**v)
                self.conn.execute("INSERT OR REPLACE INTO youtube_videos VALUES (?,?,?,?)",
                                  (v.video_id, v.video_url, v.video_title, v.published_at))
            for m in data.get("resource_video_map", []):
                self.conn.execute("INSERT OR REPLACE INTO resource_video_map VALUES (?,?,?)",
                                  (m["resource_id"], m["video_id"], m.get("timestamp_note")))
        log.info("JSON 덤프에서 DB 복원: 자료 %d건", len(data.get("resources", [])))
