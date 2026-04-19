"""Small utility functions used across the bot."""

from __future__ import annotations

import fcntl
import logging
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

from forgewright.config import _mention_re


def slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "task"


def has_mention(text: str | None, bot_username: str = "claude") -> bool:
    return bool(text and _mention_re(bot_username).search(text))


def shortdt(s: str | None) -> str:
    if not s:
        return "\u2014"
    return s.replace("T", " ").split(".")[0]


def run(cmd: list[str], *, cwd: Path | None = None,
        env: dict | None = None, check: bool = True,
        capture: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess:
    logging.debug("RUN %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=check, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True)


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("LOCK busy: %s \u2014 skipping", path.name)
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()
