"""Issue and MR handlers — the core processing logic."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

MAX_LOG_CHARS_PER_JOB = 6000
MAX_LOG_CHARS_TOTAL = 16000
# Full job logs go to .forgewright/ci/ — capped so a runaway log can't fill
# the disk.
MAX_LOG_FILE_BYTES = 10 * 1024 * 1024

from forgewright.authz import (
    filter_discussions,
    filter_notes,
    make_authorizer,
)
from forgewright.decision import (
    extract_user_instructions,
    fingerprint_issue,
    fingerprint_mr,
    is_review_mode,
    next_ci_fix_attempts,
    should_process_issue,
    should_process_mr,
)
from forgewright.formatting import (
    format_discussions,
    format_notes,
    format_review_diff,
    notes_from_discussions,
    unified_diff_file,
)
from forgewright.git import (
    cleanup_worktree,
    clone_or_update_mirror,
    make_worktree,
    push_branch,
)
from forgewright.helpers import file_lock, parse_ts, run, slugify
from forgewright.posting import post_mr_responses, post_review_comments
from forgewright.prompts import ISSUE_PROMPT, MR_REVIEW_PROMPT, MR_UPDATE_PROMPT
from forgewright.workspace import (
    CI_DIR,
    MR_DIFF,
    WORK_DIR,
    ensure_work_dir,
    safe_name,
)
from forgewright.types import (
    Discussion,
    Issue,
    MergeRequest,
    MRDetail,
    Note,
    Pipeline,
    Project,
)

if TYPE_CHECKING:
    from pathlib import Path

    from forgewright.agent.base import Agent
    from forgewright.config import Config
    from forgewright.platform.base import Platform
    from forgewright.state import State


def _fetch_failed_job_logs(platform: "Platform", pid: int | str,
                           pipeline: Pipeline,
                           wt: "Path | None" = None) -> str:
    """Fetch logs for failed jobs in a pipeline, returning a formatted block.

    The prompt gets the tail of each log; with *wt* the complete logs are
    also written to ``.forgewright/ci/<job>.log`` and referenced.
    """
    if not pipeline.id:
        return ""
    try:
        jobs = platform.pipeline_jobs(pid, pipeline.id)
    except Exception as e:
        logging.warning("failed to fetch pipeline jobs: %s", e)
        return ""

    failed_jobs = [j for j in jobs if j.status == "failed"]
    if not failed_jobs:
        return ""

    parts: list[str] = []
    total = 0
    for job in failed_jobs:
        if total >= MAX_LOG_CHARS_TOTAL:
            parts.append(f"\n_(truncated — {len(failed_jobs) - len(parts)} "
                         f"more failed jobs not shown)_")
            break
        try:
            log = platform.job_log(pid, job.id)
        except Exception as e:
            logging.warning("failed to fetch log for job %s: %s",
                            job.name, e)
            continue
        full_note = ""
        if wt is not None:
            try:
                ci_dir = ensure_work_dir(wt) / CI_DIR
                ci_dir.mkdir(exist_ok=True)
                fname = f"{safe_name(f'{job.id}-{job.name}')}.log"
                data = log.encode("utf-8", errors="replace")
                if len(data) > MAX_LOG_FILE_BYTES:
                    data = (b"...(log start cut)\n"
                            + data[-MAX_LOG_FILE_BYTES:])
                (ci_dir / fname).write_bytes(data)
                full_note = (f"\nFull log: `{WORK_DIR}/{CI_DIR}/{fname}` "
                             f"({len(log)} chars)")
            except OSError as e:
                logging.warning("could not save log of job %s: %s",
                                job.name, e)
        if len(log) > MAX_LOG_CHARS_PER_JOB:
            log = f"…(truncated)\n{log[-MAX_LOG_CHARS_PER_JOB:]}"
        parts.append(
            f"### Job: {job.name} (stage: {job.stage}){full_note}"
            f"\n```\n{log}\n```"
        )
        total += len(log)

    if not parts:
        return ""
    return "\n## Failed job logs\n" + "\n".join(parts)


def _wait_for_mr_update(platform: Platform, pid: int | str, mr_number: int,
                        expected_sha: str, max_attempts: int = 10,
                        delay: float = 1.0) -> MRDetail | None:
    """Poll until the MR's diff_refs.head_sha matches *expected_sha*."""
    for attempt in range(max_attempts):
        try:
            mr_detail = platform.mr_changes(pid, mr_number)
            if (mr_detail.diff_refs
                    and mr_detail.diff_refs.head_sha == expected_sha):
                return mr_detail
        except Exception:
            pass
        if attempt < max_attempts - 1:
            time.sleep(delay)
    logging.warning(
        "MR !%d head_sha did not converge to %s after %d attempts",
        mr_number, expected_sha[:12], max_attempts)
    try:
        return platform.mr_changes(pid, mr_number)
    except Exception:
        return None


CONTINUE_HINT = ("\n\n---\n_Mention @{bot} in a new comment to continue "
                 "here; feedback on code changes belongs in the MR._")


def _issue_branch(cfg: Config, state: State, pid: int | str,
                  issue: Issue) -> str:
    """Branch for an issue — reused from earlier runs.

    The issue title may have been edited since the last run; a fresh slug
    would start a second branch (and a second MR) for the same issue.
    """
    prev = (state.proj(pid)["issues"].get(str(issue.number)) or {})
    prev_branch = prev.get("branch") or ""
    if prev_branch.startswith(f"{cfg.branch_prefix}issue-{issue.number}-"):
        return prev_branch
    return f"{cfg.branch_prefix}issue-{issue.number}-{slugify(issue.title)}"


def _record_mr_state(state: State, pid: int | str, number: int, branch: str,
                     fp: dict, ci_fix_attempts: int = 0) -> None:
    proj = state.proj(pid)
    proj["merge_requests"][str(number)] = {
        "branch": branch,
        "fingerprint": fp,
        "ci_fix_attempts": ci_fix_attempts,
        "last_run_at": datetime.now(timezone.utc).isoformat(),
    }
    state.save()


def handle_issue(cfg: Config, platform: Platform, agent: Agent,
                 state: State, project: Project,
                 issue: Issue, notes: list[Note]) -> None:
    pid = project.id
    number = issue.number
    base_branch = cfg.default_base_branch or project.default_branch or "main"
    branch = _issue_branch(cfg, state, pid, issue)
    hint = CONTINUE_HINT.format(bot=cfg.bot_username)

    agent_ran = False
    lock_path = cfg.lock_dir / f"{pid}-{slugify(branch, 80)}.lock"
    try:
        with file_lock(lock_path) as ok:
            if not ok:
                return

            mirror = clone_or_update_mirror(cfg, platform, project)
            wt = make_worktree(cfg, mirror, pid, branch, base_branch)
            delete_branch_after = False
            try:
                prompt = ISSUE_PROMPT.format(
                    repo_path=project.path,
                    base_branch=base_branch,
                    iid=number,
                    title=issue.title,
                    web_url=issue.web_url,
                    description=issue.description or "_(empty)_",
                    notes_block=format_notes(notes),
                    branch=branch,
                    bot_username=cfg.bot_username,
                    co_author_name=cfg.git_user_name,
                    co_author_email=cfg.git_user_email,
                )
                result = agent.run(prompt, wt)
                agent_ran = True
                summary = result.summary or "_(no summary)_"

                pushed = ""
                try:
                    pushed = push_branch(cfg, platform, wt, branch,
                                         base_branch)
                except subprocess.CalledProcessError as e:
                    logging.error("push failed: %s", e)

                existing_mr = platform.find_mr_for_branch(pid, branch)
                if pushed or not existing_mr:
                    title = f"{issue.title} (#{number})"
                    description = (
                        f"_Auto-generated Draft MR by forgewright for issue "
                        f"#{number}._\n\n"
                        f"Closes #{number}\n\n---\n\n"
                        f"{summary}\n\n"
                        f"---\n<details><summary>agent run log (tail)"
                        f"</summary>\n\n```\n{result.output[-4000:]}\n```"
                        f"\n</details>"
                    )
                    if existing_mr:
                        platform.update_mr(pid, existing_mr.number,
                                           description=description)
                        platform.comment_mr(
                            pid, existing_mr.number,
                            f"\U0001f916 forgewright: pushed new commits."
                            f"\n\n{summary}")
                    elif not pushed:
                        comment = f"\U0001f916 forgewright:\n\n{summary}"
                        if not result.ok:
                            comment += (
                                f"\n\n<details><summary>log tail</summary>"
                                f"\n\n```\n{result.output[-2000:]}\n```"
                                f"\n</details>")
                        platform.comment_issue(pid, number, comment + hint)
                        delete_branch_after = True
                    else:
                        mr = platform.create_mr(
                            pid,
                            source_branch=branch,
                            target_branch=base_branch,
                            title=title, description=description,
                            draft=True,
                            labels=["forgewright"])
                        platform.comment_issue(
                            pid, number,
                            f"\U0001f916 forgewright: opened Draft MR "
                            f"!{mr.number}: {mr.web_url}")
                elif result.ok:
                    platform.comment_issue(
                        pid, number,
                        f"\U0001f916 forgewright:\n\n{summary}{hint}")
                else:
                    platform.comment_issue(
                        pid, number,
                        f"\U0001f916 forgewright: run failed.\n\n"
                        f"<details><summary>log tail</summary>\n\n"
                        f"```\n{result.output[-3000:]}\n```\n</details>"
                        f"{hint}")
            finally:
                cleanup_worktree(mirror, wt)
            if delete_branch_after:
                run(["git", "branch", "-D", branch], cwd=mirror, check=False)
    finally:
        # Record the fingerprint as soon as the agent has run — even when
        # pushing/posting crashed afterwards. Otherwise every poll would
        # re-run the (expensive) agent for the same request.
        if agent_ran:
            proj = state.proj(pid)
            proj["issues"][str(number)] = {
                "branch": branch,
                "fingerprint": fingerprint_issue(issue, notes,
                                                 cfg.bot_username),
                "last_run_at": datetime.now(timezone.utc).isoformat(),
            }
            state.save()


FORK_NOTE = (
    "NOTE: this merge request comes from a fork. You are on a detached, "
    "read-only checkout of its head commit; nothing you commit can be pushed. "
    "Do NOT make commits — put any suggested change into your review "
    "comments instead.")


def handle_mr_review(cfg: Config, platform: Platform, agent: Agent,
                     state: State, project: Project,
                     mr: MergeRequest, discussions: list[Discussion],
                     pipelines: list[Pipeline],
                     prev: dict | None, reason: str,
                     is_authorized=None) -> None:
    """Handle an MR in review mode (someone else's MR)."""
    pid = project.id
    number = mr.number
    branch = mr.source_branch
    base_branch = mr.target_branch or project.default_branch or "main"
    notes = notes_from_discussions(discussions)
    pushed = ""
    # Fork MRs: the source branch is not in this repo. Check out the MR head
    # ref detached and never push (the branch name could collide with — or
    # create — a branch in this repository).
    detach_ref = platform.mr_head_ref(number) if mr.is_fork else None
    can_push = (not mr.is_fork
                and branch not in (base_branch, project.default_branch))
    wt_name = f"mr-{number}-fork" if mr.is_fork else branch

    agent_ran = False
    lock_path = cfg.lock_dir / f"{pid}-review-{slugify(wt_name, 80)}.lock"
    try:
        with file_lock(lock_path) as ok:
            if not ok:
                return

            mirror = clone_or_update_mirror(cfg, platform, project)
            wt = make_worktree(cfg, mirror, pid, wt_name, base_branch,
                               detach_ref=detach_ref)
            try:
                mr_detail: MRDetail | None = None
                try:
                    mr_detail = platform.mr_changes(pid, number)
                except Exception as e:
                    logging.error("failed to fetch MR changes for !%d: %s",
                                  number, e)

                changes = mr_detail.changes if mr_detail else []
                diff_rel = f"{WORK_DIR}/{MR_DIFF}"
                (ensure_work_dir(wt) / MR_DIFF).write_text(
                    unified_diff_file(changes), encoding="utf-8")
                refs = (mr_detail.diff_refs if mr_detail else None) \
                    or mr.diff_refs
                diff_block = format_review_diff(
                    changes, diff_rel,
                    base_sha=refs.base_sha if refs else None,
                    head_sha=refs.head_sha if refs else None)
                discussion_block = format_discussions(
                    discussions, bot_username=cfg.bot_username)
                user_instructions = extract_user_instructions(
                    mr, notes, cfg.bot_username, is_authorized)
                if mr.is_fork:
                    user_instructions = f"{FORK_NOTE}\n\n{user_instructions}"

                prompt = MR_REVIEW_PROMPT.format(
                    repo_path=project.path,
                    base_branch=base_branch,
                    mr_iid=number,
                    title=mr.title,
                    web_url=mr.web_url,
                    mr_author=mr.author.username,
                    branch=(f"{branch} (fork, detached checkout)"
                            if mr.is_fork else branch),
                    target_branch=base_branch,
                    description=mr.description or "_(empty)_",
                    discussion_block=discussion_block,
                    user_instructions=user_instructions,
                    diff_block=diff_block,
                    bot_username=cfg.bot_username,
                )
                result = agent.run(prompt, wt)
                agent_ran = True
                summary = result.summary or "_(no review generated)_"

                if not result.ok:
                    platform.comment_mr(
                        pid, number,
                        f"\U0001f916 forgewright: review run failed.\n\n"
                        f"<details><summary>log tail</summary>\n\n"
                        f"```\n{result.output[-3000:]}\n```\n</details>")
                else:
                    if can_push:
                        try:
                            pushed = push_branch(cfg, platform, wt, branch,
                                                 base_branch)
                        except subprocess.CalledProcessError as e:
                            logging.error("push failed in review mode: %s", e)
                    else:
                        logging.info(
                            "mr %d review: not pushing (fork or default "
                            "branch)", number)

                    if pushed:
                        logging.info("mr %d review: agent pushed changes",
                                     number)
                        fresh_detail = _wait_for_mr_update(
                            platform, pid, number, pushed)
                        if not fresh_detail:
                            fresh_detail = mr_detail
                        if not fresh_detail:
                            fresh_detail = MRDetail(
                                number=number, diff_refs=mr.diff_refs,
                                changes=[])
                        post_review_comments(
                            platform, pid, fresh_detail, summary,
                            prefix="\U0001f916 forgewright: pushed changes."
                                   "\n\n")
                    else:
                        try:
                            fresh_detail = platform.mr_changes(pid, number)
                        except Exception as e:
                            logging.warning(
                                "re-fetch of MR !%d changes failed: %s — "
                                "using earlier diff_refs", number, e)
                            fresh_detail = mr_detail
                        if not fresh_detail:
                            fresh_detail = MRDetail(
                                number=number, diff_refs=mr.diff_refs,
                                changes=[])
                        post_review_comments(
                            platform, pid, fresh_detail, summary)
            finally:
                cleanup_worktree(mirror, wt)
    finally:
        if agent_ran:
            fp = fingerprint_mr(mr, notes, pipelines, cfg.bot_username)
            if pushed:
                # The agent's own push moves head_sha; record the pushed SHA.
                fp["head_sha"] = pushed
            _record_mr_state(state, pid, number, branch, fp)


def handle_mr(cfg: Config, platform: Platform, agent: Agent,
              state: State, project: Project,
              mr: MergeRequest, discussions: list[Discussion],
              pipelines: list[Pipeline],
              prev: dict | None, reason: str) -> None:
    """Handle an MR update (bot's own draft MR with new reviewer feedback)."""
    pid = project.id
    number = mr.number
    branch = mr.source_branch
    base_branch = mr.target_branch or project.default_branch or "main"
    notes = notes_from_discussions(discussions)
    pushed = ""
    ci_attempts = next_ci_fix_attempts(prev, reason)
    max_attempts = getattr(cfg, "max_ci_fix_attempts", 3)

    agent_ran = False
    lock_path = cfg.lock_dir / f"{pid}-{slugify(branch, 80)}.lock"
    try:
        with file_lock(lock_path) as ok:
            if not ok:
                return

            mirror = clone_or_update_mirror(cfg, platform, project)
            wt = make_worktree(cfg, mirror, pid, branch, base_branch)
            try:
                prev_fp = (prev or {}).get("fingerprint") or {}
                prev_note_at = prev_fp.get("last_note_at")
                cutoff = parse_ts(prev_note_at) if prev_note_at else None
                new_discussions = []
                for d in discussions:
                    new_notes_in_d = [
                        n for n in d.notes
                        if not n.system
                        and n.author.username != cfg.bot_username
                        and (cutoff is None
                             or parse_ts(n.created_at) > cutoff)
                    ]
                    if new_notes_in_d:
                        new_discussions.append(d)

                activity_lines = [f"_Trigger: {reason}_", ""]
                if new_discussions:
                    activity_lines.append("## New comments")
                    activity_lines.append(
                        format_discussions(new_discussions,
                                           bot_username=cfg.bot_username))
                if pipelines:
                    p = pipelines[0]
                    activity_lines.append(
                        f"\n## Latest pipeline\n- status: **{p.status}**\n"
                        f"- sha: {p.sha}\n- url: {p.web_url}"
                    )
                    if p.status == "failed" and "pipeline" in reason:
                        log_block = _fetch_failed_job_logs(
                            platform, pid, p, wt)
                        if log_block:
                            activity_lines.append(log_block)
                activity_block = "\n".join(activity_lines)

                issue_block = "_(no linked issue detected)_"
                m = re.search(r"(?:Closes|Fixes|Resolves)\s+#(\d+)",
                              mr.description or "", re.IGNORECASE)
                if m:
                    issue_url = platform.issue_url(project, int(m.group(1)))
                    issue_block = f"Issue #{m.group(1)} ({issue_url})"

                prompt = MR_UPDATE_PROMPT.format(
                    repo_path=project.path,
                    base_branch=base_branch,
                    mr_iid=number,
                    title=mr.title,
                    web_url=mr.web_url,
                    branch=branch,
                    issue_block=issue_block,
                    activity_block=activity_block,
                    co_author_name=cfg.git_user_name,
                    co_author_email=cfg.git_user_email,
                )
                result = agent.run(prompt, wt)
                agent_ran = True
                summary = result.summary or "_(no summary written)_"

                try:
                    pushed = push_branch(cfg, platform, wt, branch,
                                         base_branch)
                except subprocess.CalledProcessError as e:
                    logging.error("push failed: %s", e)

                if pushed:
                    _wait_for_mr_update(platform, pid, number, pushed)
                    post_mr_responses(
                        platform, pid, number, summary,
                        prefix="\U0001f916 forgewright: pushed updates.\n\n")
                elif result.ok:
                    post_mr_responses(platform, pid, number, summary)
                else:
                    platform.comment_mr(
                        pid, number,
                        f"\U0001f916 forgewright: run failed.\n\n"
                        f"<details><summary>log tail</summary>\n\n"
                        f"```\n{result.output[-3000:]}\n```\n</details>")

                if "pipeline failed" in reason and ci_attempts >= max_attempts:
                    platform.comment_mr(
                        pid, number,
                        f"\U0001f916 forgewright: that was automatic CI fix "
                        f"attempt {ci_attempts}/{max_attempts}. If the "
                        f"pipeline fails again I will not retry on my own "
                        f"— leave a comment here to continue.")
            finally:
                cleanup_worktree(mirror, wt)
    finally:
        if agent_ran:
            fp = fingerprint_mr(mr, notes, pipelines, cfg.bot_username)
            if pushed:
                fp["head_sha"] = pushed
            _record_mr_state(state, pid, number, branch, fp, ci_attempts)


def process_project(cfg: Config, platform: Platform, agent: Agent,
                    state: State, project: Project) -> None:
    """Process all issues and MRs in a single project."""
    from forgewright.helpers import shortdt

    pid = project.id
    proj_state = state.proj(pid)
    last_checked = proj_state.get("last_checked_at")
    updated_after = last_checked
    # Capture the cutoff for the NEXT poll BEFORE doing any work, so updates that
    # land while this run is in progress are still picked up next time.
    run_started_at = datetime.now(timezone.utc).isoformat()

    logging.info("PROJECT %s (since %s)", project.path,
                 shortdt(last_checked))

    # Per-project agent state (e.g. Hermes' learning profile) keys off this.
    set_scope = getattr(agent, "set_scope", None)
    if callable(set_scope):
        set_scope(project.path)

    authorized = make_authorizer(platform, project, cfg)
    if authorized is not None:
        logging.info("PROJECT %s: authorization enabled (min role %s)",
                     project.path, cfg.authorization_min_role)

    earliest_crash_ts: str | None = None

    def _record_crash(updated_at: str | None) -> None:
        nonlocal earliest_crash_ts
        if updated_at and (not earliest_crash_ts
                           or updated_at < earliest_crash_ts):
            earliest_crash_ts = updated_at

    for issue in platform.list_issues(pid, updated_after):
        number = issue.number
        try:
            notes = platform.issue_notes(pid, number)
        except Exception as e:
            logging.warning("issue %d notes fetch failed: %s", number, e)
            continue
        if authorized is not None:
            notes = filter_notes(notes, authorized)
        prev = proj_state["issues"].get(str(number))
        go, reason = should_process_issue(issue, notes, prev,
                                          cfg.bot_username, authorized)
        if not go:
            logging.debug("issue %d skip: %s", number, reason)
            continue
        logging.info("issue %d TRIGGER: %s", number, reason)
        try:
            handle_issue(cfg, platform, agent, state, project, issue, notes)
        except Exception as e:
            logging.exception("issue %d handler crashed: %s", number, e)
            _record_crash(issue.updated_at)

    for mr in platform.list_mrs(pid, updated_after):
        number = mr.number
        try:
            discussions = platform.mr_discussions(pid, number)
            pipelines = platform.mr_pipelines(pid, number)
        except Exception as e:
            logging.warning("mr %d fetch failed: %s", number, e)
            continue
        if authorized is not None:
            discussions = filter_discussions(discussions, authorized)
        notes = notes_from_discussions(discussions)
        prev = proj_state["merge_requests"].get(str(number))
        go, reason = should_process_mr(
            mr, notes, pipelines, prev, cfg.bot_username, cfg.branch_prefix,
            authorized, discussions=discussions,
            max_ci_fix_attempts=getattr(cfg, "max_ci_fix_attempts", 3))
        if not go:
            logging.debug("mr %d skip: %s", number, reason)
            continue
        logging.info("mr %d TRIGGER: %s", number, reason)
        try:
            if is_review_mode(mr, cfg.bot_username, cfg.branch_prefix):
                logging.info("mr %d REVIEW MODE (not authored by bot)",
                             number)
                handle_mr_review(cfg, platform, agent, state, project, mr,
                                 discussions, pipelines, prev, reason,
                                 authorized)
            else:
                handle_mr(cfg, platform, agent, state, project, mr,
                          discussions, pipelines, prev, reason)
        except Exception as e:
            logging.exception("mr %d handler crashed: %s", number, e)
            _record_crash(mr.updated_at)

    if earliest_crash_ts:
        logging.warning(
            "handler crash(es) detected \u2014 rewinding last_checked_at to %s",
            shortdt(earliest_crash_ts))
        proj_state["last_checked_at"] = earliest_crash_ts
    else:
        # Use the run-start time (not "now"): anything updated mid-run is then
        # re-evaluated next poll (fingerprints dedupe already-handled items).
        proj_state["last_checked_at"] = run_started_at
    state.save()
