"""Decision logic: should we process an issue/MR, and in which mode."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Callable

from forgewright.helpers import has_mention, parse_ts
from forgewright.types import (
    Discussion, Issue, MergeRequest, Note, Pipeline, Project, User,
)

if TYPE_CHECKING:
    from forgewright.platform.base import Platform
    from forgewright.config import Config

# Predicate: may this user interact with the bot? (None = no restriction)
Authorizer = Callable[["User | None"], bool]


def _allowed(user: "User | None", is_authorized: "Authorizer | None") -> bool:
    """True when no authz is configured, or the user clears the threshold."""
    return is_authorized is None or is_authorized(user)


def _desc_hash(text: str | None) -> str:
    """Short hash of description text for change detection."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def _note_key(n: Note) -> tuple:
    """Chronological sort key for notes (tie-break by id)."""
    return (parse_ts(n.created_at), str(n.id))


def _newest_note(notes: list[Note]) -> Note | None:
    """The chronologically newest note (tie-break by id), or None."""
    if not notes:
        return None
    return max(notes, key=_note_key)


def _human_notes(notes: list[Note], bot_username: str,
                 is_authorized: "Authorizer | None" = None) -> list[Note]:
    return [n for n in notes
            if not n.system and n.author.username != bot_username
            and _allowed(n.author, is_authorized)]


def _newer_than_last_run(notes: list[Note], prev_fp: dict) -> list[Note]:
    """Notes created after the newest human note recorded at the last run.

    Uses the same ordering key as :func:`_newest_note`, so a note that became
    the recorded cutoff is never "newer" than itself — no re-trigger loops.
    """
    cutoff_at = prev_fp.get("last_note_at")
    if not cutoff_at:
        # No human note existed at the last run → every human note is new.
        return list(notes)
    cutoff = (parse_ts(cutoff_at), str(prev_fp.get("last_note_id")))
    return [n for n in notes if _note_key(n) > cutoff]


def _bot_requested(mr: MergeRequest, bot_username: str) -> bool:
    return (any(r.username == bot_username for r in mr.reviewers)
            or any(a.username == bot_username for a in mr.assignees))


def is_review_mode(mr: MergeRequest, bot_username: str,
                   branch_prefix: str) -> bool:
    """Determine if this MR should be handled in review mode.

    Review mode applies when the MR was NOT created by the bot: it is not on a
    ``branch_prefix`` branch of this repository and not authored by the bot
    user.  Fork MRs are always review mode — the bot never owns a fork branch,
    even one that happens to be named ``forgewright/…``.
    """
    if mr.is_fork:
        return True
    is_bot_branch = mr.source_branch.startswith(branch_prefix)
    is_bot_author = mr.author.username == bot_username
    return not is_bot_branch and not is_bot_author


def fingerprint_mr(mr: MergeRequest, notes: list[Note],
                   pipelines: list[Pipeline],
                   bot_username: str = "") -> dict:
    human_notes = _human_notes(notes, bot_username)
    last_note = _newest_note(human_notes)
    last_pipe = pipelines[0] if pipelines else None
    return {
        "description_hash": _desc_hash(mr.description),
        "last_note_id": last_note.id if last_note else None,
        "last_note_at": last_note.created_at if last_note else None,
        "labels": sorted(mr.labels),
        "pipeline_id": last_pipe.id if last_pipe else None,
        "pipeline_sha": last_pipe.sha if last_pipe else None,
        "pipeline_status": last_pipe.status if last_pipe else None,
        "head_sha": mr.sha,
        "bot_requested": _bot_requested(mr, bot_username),
        "desc_mention": has_mention(mr.description, bot_username),
    }


def fingerprint_issue(issue: Issue, notes: list[Note],
                      bot_username: str = "") -> dict:
    human_notes = _human_notes(notes, bot_username)
    last_note = _newest_note(human_notes)
    return {
        "description_hash": _desc_hash(issue.description),
        "last_note_id": last_note.id if last_note else None,
        "last_note_at": last_note.created_at if last_note else None,
        "labels": sorted(issue.labels),
    }


def should_process_issue(issue: Issue, notes: list[Note],
                         prev: dict | None,
                         bot_username: str,
                         is_authorized: "Authorizer | None" = None
                         ) -> tuple[bool, str]:
    """Run on the first mention, then only on each NEW mention.

    Once the bot has run for an issue, plain comments ("thanks!"), label
    changes (e.g. moving the card on an issue board) and description edits do
    not start another (expensive) agent run — only a comment that mentions
    the bot again does.  Feedback on code changes belongs in the MR.
    """
    mention_in_desc = (
        has_mention(issue.description, bot_username)
        and _allowed(issue.author, is_authorized))
    mention_notes = [
        n for n in _human_notes(notes, bot_username, is_authorized)
        if has_mention(n.body, bot_username)]
    if not (mention_in_desc or mention_notes):
        return False, f"no @{bot_username} mention"
    if prev is None:
        return True, "first-seen with mention"
    prev_fp = prev.get("fingerprint") or {}
    fp = fingerprint_issue(issue, notes, bot_username)
    if fp == prev_fp:
        return False, "no change since last run"
    if _newer_than_last_run(mention_notes, prev_fp):
        return True, "new mention"
    return False, "activity without a new mention"


def ci_fix_trigger(fp: dict, prev_fp: dict) -> bool:
    """A failed pipeline the bot has not reacted to yet.

    Fires when the newest pipeline is failed and it is a *different* pipeline
    (or status) than the one recorded at the last run — so a failure of the
    pipeline that tests the bot's own fix triggers again.
    """
    if fp.get("pipeline_status") != "failed":
        return False
    if prev_fp.get("pipeline_status") != "failed":
        return True
    if "pipeline_id" in prev_fp and fp.get("pipeline_id") is not None:
        return fp["pipeline_id"] != prev_fp["pipeline_id"]
    return fp.get("pipeline_sha") != prev_fp.get("pipeline_sha")


def _should_process_review_mr(mr: MergeRequest, notes: list[Note],
                              discussions: "list[Discussion] | None",
                              prev: dict | None, fp: dict,
                              bot_username: str,
                              is_authorized: "Authorizer | None"
                              ) -> tuple[bool, str]:
    """Someone else's MR: act on explicit requests only.

    Triggers: a new mention, the bot newly added as reviewer/assignee, a
    mention newly added to the description, or a human reply in a thread the
    bot takes part in.  New pushes, labels and unrelated comments do NOT
    re-run a full review.
    """
    mention_in_desc = (has_mention(mr.description, bot_username)
                       and _allowed(mr.author, is_authorized))
    human = _human_notes(notes, bot_username, is_authorized)
    mention_notes = [n for n in human if has_mention(n.body, bot_username)]
    requested = fp["bot_requested"]
    if not (mention_in_desc or mention_notes or requested):
        return False, "not a bot-owned MR"
    if prev is None:
        return True, "first-seen with review request"

    prev_fp = prev.get("fingerprint") or {}
    if fp == prev_fp:
        return False, "no change since last run"

    reasons: list[str] = []
    if _newer_than_last_run(mention_notes, prev_fp):
        reasons.append("new mention")
    if requested and not prev_fp.get("bot_requested", requested):
        reasons.append("review requested")
    if (mention_in_desc
            and not prev_fp.get("desc_mention", fp["desc_mention"])):
        reasons.append("mentioned in description")
    if discussions:
        thread_notes = [
            n for d in discussions
            if any(x.author.username == bot_username for x in d.notes)
            for n in _human_notes(d.notes, bot_username, is_authorized)]
        if _newer_than_last_run(thread_notes, prev_fp):
            reasons.append("reply in bot thread")
    if not reasons:
        return False, "activity without a new review request"
    return True, ", ".join(reasons)


def should_process_mr(mr: MergeRequest, notes: list[Note],
                      pipelines: list[Pipeline],
                      prev: dict | None, bot_username: str,
                      branch_prefix: str,
                      is_authorized: "Authorizer | None" = None,
                      discussions: "list[Discussion] | None" = None,
                      max_ci_fix_attempts: int = 3,
                      ) -> tuple[bool, str]:
    fp = fingerprint_mr(mr, notes, pipelines, bot_username)
    if is_review_mode(mr, bot_username, branch_prefix):
        return _should_process_review_mr(
            mr, notes, discussions, prev, fp, bot_username, is_authorized)

    # The bot's own MR: every human comment is feedback for it.
    non_bot_notes = _human_notes(notes, bot_username, is_authorized)
    if prev is None:
        if fp["pipeline_status"] == "failed":
            return True, "first-seen, pipeline failed"
        if non_bot_notes:
            return True, "first-seen with human activity"
        return False, "first-seen, no human activity yet"

    prev_fp = prev.get("fingerprint") or {}
    if fp == prev_fp:
        return False, "no change since last run"

    reasons = []
    if _newer_than_last_run(non_bot_notes, prev_fp):
        reasons.append("new comment")
    if ci_fix_trigger(fp, prev_fp):
        attempts = int(prev.get("ci_fix_attempts") or 0)
        if reasons or attempts < max_ci_fix_attempts:
            reasons.append("pipeline failed")
        else:
            return False, (f"pipeline failed, but {attempts} automatic CI "
                           f"fix attempt(s) already used — waiting for a "
                           f"comment")
    # Deliberately NOT triggers: new commits (the bot's own pushes, or a human
    # pushing a fix themselves), label changes, and description edits (the
    # bot rewrites its own MR description when it pushes from the issue).
    if not reasons:
        return False, "state diff but no meaningful trigger"
    return True, ", ".join(reasons)


def next_ci_fix_attempts(prev: dict | None, reason: str) -> int:
    """Counter of consecutive automatic CI-fix runs without human input."""
    attempts = 0 if (prev is None or "new comment" in reason) \
        else int((prev or {}).get("ci_fix_attempts") or 0)
    if "pipeline failed" in reason:
        attempts += 1
    return attempts


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
                              bot_username: str,
                              is_authorized: "Authorizer | None" = None) -> str:
    """Extract the user's @<bot> instructions from the MR.

    When *is_authorized* is given, instructions from users below the role
    threshold are skipped so they cannot steer the agent.
    """
    instructions = []

    desc = mr.description or ""
    if has_mention(desc, bot_username) and _allowed(mr.author, is_authorized):
        instructions.append(f"From MR description:\n{desc}")

    for n in notes:
        if n.system:
            continue
        if n.author.username == bot_username:
            continue
        if not _allowed(n.author, is_authorized):
            continue
        body = n.body.strip()
        if has_mention(body, bot_username):
            instructions.append(f"From @{n.author.username}:\n{body}")

    return "\n\n".join(instructions) if instructions else \
        "_(no specific instructions \u2014 perform a general review)_"
