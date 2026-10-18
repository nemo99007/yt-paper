"""Paths and environment settings."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from rich.logging import RichHandler

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional at runtime
    load_dotenv = None

ROOT: Path = Path(__file__).resolve().parent.parent
if load_dotenv is not None:
    load_dotenv(ROOT / ".env")

DATA_DIR: Path = ROOT / "data"
DB_PATH: Path = DATA_DIR / "archive.db"
DUMP_PATH: Path = DATA_DIR / "index_dump.json"
DOCS_DIR: Path = ROOT / "docs"
REFERENCES_DIR: Path = DOCS_DIR / "references"
VIDEOS_DIR: Path = DOCS_DIR / "videos"

ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5-5"
ANTHROPIC_FALLBACKS: str = (os.getenv("ANTHROPIC_FALLBACKS") or "default").strip().lower()
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS") or 16000)
LLM_BACKEND: str = (os.getenv("LLM_BACKEND") or "auto").strip().lower()  # auto | api | cli
CLI_MODEL: str | None = (os.getenv("CLI_MODEL") or "").strip() or None
CLI_TIMEOUT: int = int(os.getenv("CLI_TIMEOUT") or 600)
LLM_EFFORT: str | None = (os.getenv("LLM_EFFORT") or "").strip().lower() or None
MAX_TEXT_CHARS: int = int(os.getenv("MAX_TEXT_CHARS") or 40000)

GIT_REMOTE: str = os.getenv("GIT_REMOTE") or "origin"
GIT_BRANCH: str = os.getenv("GIT_BRANCH") or "main"

HTTP_TIMEOUT: float = 60.0
USER_AGENT: str = "yt-paper/1.0 (personal knowledge archive)"


def setup_logging() -> None:
    level = (os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    for noisy in ("httpx", "httpx2", "trafilatura", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
