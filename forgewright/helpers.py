"""Small utility functions used across the bot."""

from __future__ import annotations

import fcntl
import logging
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from forgewright.config import _mention_re


def slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "task"


def parse_ts(s: str | None) -> datetime:
    """Parse an ISO-8601 timestamp into a tz-aware datetime (UTC fallback).

    Handles the trailing ``Z`` (GitHub) and explicit offsets (GitLab), as well
    as date-only and naive values.  Anything unparseable sorts as the minimum
    time, so it never raises in sort keys / comparisons.
    """
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    txt = s.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
