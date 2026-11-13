"""The agent's scratch directory inside a worktree: ``.forgewright/``.

Everything forgewright hands to the agent or gets back from it lives here —
never inside the repository's own tree (and deliberately not in ``.claude/``,
which is Claude Code's real per-repo config dir and is often committed):

    .forgewright/last-run-summary.md   agent → forgewright: the result
    .forgewright/agent-live.log        live agent output (tail -f)
    .forgewright/mr.diff               full MR diff (review mode)
    .forgewright/ci/<job>.log          full logs of failed CI jobs

The directory is listed in the mirror's ``info/exclude``, which applies to
every worktree of that mirror, so an agent running ``git add -A`` can never
commit (and push) these files.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

WORK_DIR = ".forgewright"
SUMMARY_FILE = "last-run-summary.md"
LIVE_LOG = "agent-live.log"
MR_DIFF = "mr.diff"
CI_DIR = "ci"

# The path agents are told to write to (relative to the worktree root).
SUMMARY_REL = f"{WORK_DIR}/{SUMMARY_FILE}"

_EXCLUDE_LINE = f"/{WORK_DIR}/"


def work_dir(wt: Path) -> Path:
    return wt / WORK_DIR


def summary_path(wt: Path) -> Path:
    return wt / WORK_DIR / SUMMARY_FILE


def live_log_path(wt: Path) -> Path:
    return wt / WORK_DIR / LIVE_LOG


def ensure_work_dir(wt: Path) -> Path:
    d = work_dir(wt)
    d.mkdir(parents=True, exist_ok=True)
    return d


def exclude_work_dir(mirror: Path) -> None:
    """Make git ignore ``.forgewright/`` in every worktree of *mirror*.

    ``info/exclude`` lives in the common git dir, so one entry in the bare
    mirror covers all its linked worktrees.
    """
    exclude = mirror / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        if _EXCLUDE_LINE not in text.splitlines():
            sep = "" if not text or text.endswith("\n") else "\n"
            exclude.write_text(
                f"{text}{sep}# forgewright agent scratch dir\n"
                f"{_EXCLUDE_LINE}\n")
    except OSError as e:  # never block a run on this
        logging.warning("could not update %s: %s", exclude, e)


def safe_name(text: str, max_len: int = 80) -> str:
    """File-system-safe name for job logs / context dirs."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return name[:max_len] or "item"
