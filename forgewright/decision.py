"""Decision logic: should we process an issue/MR, and in which mode."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from forgewright.helpers import has_mention
from forgewright.types import Issue, MergeRequest, Note, Pipeline, Project

if TYPE_CHECKING:
    from forgewright.platform.base import Platform
    from forgewright.config import Config


def _desc_hash(text: str | None) -> str:
    """Short hash of description text for change detection."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def is_review_mode(mr: MergeRequest, bot_username: str,
                   branch_prefix: str) -> bool:
    """Determine if this MR should be handled in review mode.

    Review mode applies when:
    - The MR was NOT created by the bot (not on a ``branch_prefix`` branch
      and author is not the bot user).
    - The bot is mentioned in the MR description or notes.
    """
    is_bot_branch = mr.source_branch.startswith(branch_prefix)
    is_bot_author = mr.author.username == bot_username
    return not is_bot_branch and not is_bot_author


def fingerprint_mr(mr: MergeRequest, notes: list[Note],
                   pipelines: list[Pipeline],
                   bot_username: str = "") -> dict:
    human_notes = [n for n in notes
                   if not n.system
                   and n.author.username != bot_username]
    last_note = human_notes[-1] if human_notes else None
    last_pipe = pipelines[0] if pipelines else None
    return {
        "description_hash": _desc_hash(mr.description),
        "last_note_id": last_note.id if last_note else None,
        "last_note_at": last_note.created_at if last_note else None,
        "labels": sorted(mr.labels),
        "pipeline_sha": last_pipe.sha if last_pipe else None,
        "pipeline_status": last_pipe.status if last_pipe else None,
        "head_sha": mr.sha,
    }


def fingerprint_issue(issue: Issue, notes: list[Note],
                      bot_username: str = "") -> dict:
    human_notes = [n for n in notes
                   if not n.system
                   and n.author.username != bot_username]
    last_note = human_notes[-1] if human_notes else None
    return {
        "description_hash": _desc_hash(issue.description),
        "last_note_id": last_note.id if last_note else None,
        "last_note_at": last_note.created_at if last_note else None,
        "labels": sorted(issue.labels),
    }


def should_process_issue(issue: Issue, notes: list[Note],
                         prev: dict | None,
                         bot_username: str) -> tuple[bool, str]:
    mention_in_desc = has_mention(issue.description, bot_username)
    mention_in_notes = any(
        has_mention(n.body, bot_username)
        and n.author.username != bot_username
        and not n.system
        for n in notes
    )
    if not (mention_in_desc or mention_in_notes):
        return False, f"no @{bot_username} mention"
    fp = fingerprint_issue(issue, notes, bot_username)
    if prev is None:
        return True, "first-seen with mention"
    prev_fp = prev.get("fingerprint") or {}
    if fp == prev_fp:
        return False, "no change since last run"
    reasons: list[str] = []
    if fp["last_note_id"] != prev_fp.get("last_note_id"):
        reasons.append("new comment")
    if fp["labels"] != prev_fp.get("labels"):
        reasons.append("labels changed")
    if fp["description_hash"] != prev_fp.get("description_hash"):
        reasons.append("description edited")
    if not reasons:
        return False, "state diff but no meaningful trigger"
    return True, ", ".join(reasons)


def should_process_mr(mr: MergeRequest, notes: list[Note],
                      pipelines: list[Pipeline],
                      prev: dict | None, bot_username: str,
                      branch_prefix: str) -> tuple[bool, str]:
    is_bot_branch = mr.source_branch.startswith(branch_prefix)
    mention_in_desc = has_mention(mr.description, bot_username)
    non_bot_notes = [
        n for n in notes
        if not n.system and n.author.username != bot_username
    ]
    mention_in_notes = any(
        has_mention(n.body, bot_username) for n in non_bot_notes)
    is_reviewer = any(
        r.username == bot_username for r in mr.reviewers)
    is_assignee = any(
        a.username == bot_username for a in mr.assignees)

    if not (is_bot_branch or mention_in_desc or mention_in_notes
            or is_reviewer or is_assignee):
        return False, "not a bot-owned MR"

    fp = fingerprint_mr(mr, notes, pipelines, bot_username)

    if prev is None:
        if not (non_bot_notes or mention_in_desc or is_reviewer
                or is_assignee or fp["pipeline_status"] in ("failed",)):
            return False, "first-seen, no human activity yet"
        return True, "first-seen with human activity"

    prev_fp = prev.get("fingerprint") or {}
    if fp == prev_fp:
        return False, "no change since last run"

    reasons = []
    if fp["last_note_id"] != prev_fp.get("last_note_id"):
        reasons.append("new comment")
    if fp["labels"] != prev_fp.get("labels"):
        reasons.append("labels changed")
    if fp["pipeline_status"] != prev_fp.get("pipeline_status"):
        reasons.append(f"pipeline -> {fp['pipeline_status']}")
    if fp["head_sha"] != prev_fp.get("head_sha"):
        reasons.append("new commits on branch")
    if fp["description_hash"] != prev_fp.get("description_hash"):
        reasons.append("description edited")
    if not reasons:
        return False, "state diff but no meaningful trigger"
    return True, ", ".join(reasons)


def select_projects(platform: Platform, cfg: Config) -> list[Project]:
    """Filter projects based on include/exclude config."""
    projects = platform.list_member_projects()
    inc = set(cfg.projects_include)
    exc = set(cfg.projects_exclude)
    out = []
    for p in projects:
        if exc and p.path in exc:
            continue
        if inc and p.path not in inc:
            continue
        out.append(p)
    return out


def extract_user_instructions(mr: MergeRequest, notes: list[Note],
                              bot_username: str) -> str:
    """Extract the user's @<bot> instructions from the MR."""
    instructions = []

    desc = mr.description or ""
    if has_mention(desc, bot_username):
        instructions.append(f"From MR description:\n{desc}")

    for n in notes:
        if n.system:
            continue
        if n.author.username == bot_username:
            continue
        body = n.body.strip()
        if has_mention(body, bot_username):
            instructions.append(f"From @{n.author.username}:\n{body}")

    return "\n\n".join(instructions) if instructions else \
        "_(no specific instructions \u2014 perform a general review)_"
