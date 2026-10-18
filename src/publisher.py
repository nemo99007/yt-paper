"""Git automation: export dump, add, commit, optional push."""
from __future__ import annotations

import logging
import shutil
import subprocess

from . import config
from .database import Database, DatabaseError

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=config.ROOT, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PublishError(f"git {' '.join(args)} 실행 실패: {e}") from e
    if check and proc.returncode != 0:
        raise PublishError(f"git {' '.join(args)} 실패:\n{(proc.stderr or proc.stdout).strip()}")
    return proc


def publish(db: Database, message: str, push: bool = False) -> bool:
    """Returns True on success (including 'nothing to commit'), False on a handled error."""
    try:
        db.export_to_json(config.DUMP_PATH)
        if shutil.which("git") is None:
            raise PublishError("git이 설치되어 있지 않습니다.")
        if _git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
            raise PublishError(f"Git 저장소가 아닙니다: {config.ROOT}\n  → `git init -b main` 후 다시 실행하세요.")

        _git("add", "docs/", "data/index_dump.json", "mkdocs.yml")
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            log.info("변경 사항이 없어 커밋을 건너뜁니다.")
        else:
            _git("commit", "-m", message)
            log.info("커밋 완료: %s", message)

        if push:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if branch != config.GIT_BRANCH:
                raise PublishError(f"현재 브랜치가 '{branch}'입니다. '{config.GIT_BRANCH}'에서만 push합니다.")
            if _git("remote", "get-url", config.GIT_REMOTE, check=False).returncode != 0:
                raise PublishError(f"원격 저장소 '{config.GIT_REMOTE}'가 없습니다.\n"
                                   f"  → git remote add {config.GIT_REMOTE} <GitHub 저장소 URL>")
            proc = _git("push", config.GIT_REMOTE, config.GIT_BRANCH, check=False)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout).strip()
                if "rejected" in err or "non-fast-forward" in err or "fetch first" in err:
                    raise PublishError("원격에 새 커밋이 있어 push가 거부되었습니다.\n"
                                       f"  → git pull --rebase {config.GIT_REMOTE} {config.GIT_BRANCH} 후 다시 실행하세요.")
                raise PublishError(f"git push 실패:\n{err}")
            log.info("push 완료: %s/%s", config.GIT_REMOTE, config.GIT_BRANCH)
        return True
    except (PublishError, DatabaseError) as e:
        log.error("%s", e)
        return False
