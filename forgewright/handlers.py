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

from forgewright.decision import (
    extract_user_instructions,
    fingerprint_issue,
    fingerprint_mr,
    is_review_mode,
    should_process_issue,
    should_process_mr,
)
from forgewright.formatting import (
    format_diff_for_review,
    format_discussions,
    format_notes,
    notes_from_discussions,
)
from forgewright.git import (
    cleanup_worktree,
    clone_or_update_mirror,
    make_worktree,
    push_branch,
)
from forgewright.helpers import file_lock, run, slugify
from forgewright.posting import post_mr_responses, post_review_comments
from forgewright.prompts import ISSUE_PROMPT, MR_REVIEW_PROMPT, MR_UPDATE_PROMPT
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
    from forgewright.agent.base import Agent
    from forgewright.config import Config
    from forgewright.platform.base import Platform
    from forgewright.state import State


def _fetch_failed_job_logs(platform: "Platform", pid: int | str,
                           pipeline: Pipeline) -> str:
    """Fetch logs for failed jobs in a pipeline, returning a formatted block."""
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
        if len(log) > MAX_LOG_CHARS_PER_JOB:
            log = f"…(truncated)\n{log[-MAX_LOG_CHARS_PER_JOB:]}"
        parts.append(
            f"### Job: {job.name} (stage: {job.stage})"
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


def handle_issue(cfg: Config, platform: Platform, agent: Agent,
                 state: State, project: Project,
                 issue: Issue, notes: list[Note]) -> None:
    pid = project.id
    number = issue.number
    base_branch = cfg.default_base_branch or project.default_branch or "main"
    branch = f"{cfg.branch_prefix}issue-{number}-{slugify(issue.title)}"

    lock_path = cfg.lock_dir / f"{pid}-{slugify(branch, 80)}.lock"
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
            summary = result.summary or "_(no summary)_"

            pushed = ""
            try:
                pushed = push_branch(cfg, platform, wt, branch, base_branch)
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
                    f"---\n<details><summary>agent run log (tail)</summary>"
                    f"\n\n```\n{result.output[-4000:]}\n```\n</details>"
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
                    platform.comment_issue(pid, number, comment)
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
                    f"\U0001f916 forgewright:\n\n{summary}")
            else:
                platform.comment_issue(
                    pid, number,
                    f"\U0001f916 forgewright: run failed.\n\n"
                    f"<details><summary>log tail</summary>\n\n"
                    f"```\n{result.output[-3000:]}\n```\n</details>")
        finally:
            cleanup_worktree(mirror, wt)
        if delete_branch_after:
            run(["git", "branch", "-D", branch], cwd=mirror, check=False)

    proj = state.proj(pid)
    proj["issues"][str(number)] = {
        "branch": branch,
        "fingerprint": fingerprint_issue(issue, notes, cfg.bot_username),
        "last_run_at": datetime.now(timezone.utc).isoformat(),
    }
    state.save()


def handle_mr_review(cfg: Config, platform: Platform, agent: Agent,
                     state: State, project: Project,
                     mr: MergeRequest, discussions: list[Discussion],
                     pipelines: list[Pipeline],
                     prev: dict | None, reason: str) -> None:
    """Handle an MR in review mode."""
    pid = project.id
    number = mr.number
    branch = mr.source_branch
    base_branch = mr.target_branch or project.default_branch or "main"
    notes = notes_from_discussions(discussions)

    lock_path = cfg.lock_dir / f"{pid}-review-{slugify(branch, 80)}.lock"
    with file_lock(lock_path) as ok:
        if not ok:
            return

        mirror = clone_or_update_mirror(cfg, platform, project)
        wt = make_worktree(cfg, mirror, pid, branch, base_branch)
        try:
            mr_detail: MRDetail | None = None
            try:
                mr_detail = platform.mr_changes(pid, number)
            except Exception as e:
                logging.error("failed to fetch MR changes for !%d: %s",
                              number, e)

            changes = mr_detail.changes if mr_detail else []
            diff_block = format_diff_for_review(changes)
            discussion_block = format_discussions(
                discussions, bot_username=cfg.bot_username)
            user_instructions = extract_user_instructions(
                mr, notes, cfg.bot_username)

            prompt = MR_REVIEW_PROMPT.format(
                repo_path=project.path,
                base_branch=base_branch,
                mr_iid=number,
                title=mr.title,
                web_url=mr.web_url,
                mr_author=mr.author.username,
                branch=branch,
                target_branch=base_branch,
                description=mr.description or "_(empty)_",
                discussion_block=discussion_block,
                user_instructions=user_instructions,
                diff_block=diff_block,
                bot_username=cfg.bot_username,
            )
            result = agent.run(prompt, wt)
            summary = result.summary or "_(no review generated)_"

            if not result.ok:
                platform.comment_mr(
                    pid, number,
                    f"\U0001f916 forgewright: review run failed.\n\n"
                    f"<details><summary>log tail</summary>\n\n"
                    f"```\n{result.output[-3000:]}\n```\n</details>")
            else:
                pushed = ""
                try:
                    pushed = push_branch(cfg, platform, wt, branch,
                                         base_branch)
                except subprocess.CalledProcessError as e:
                    logging.error("push failed in review mode: %s", e)

                if pushed:
                    logging.info("mr %d review: agent pushed changes", number)
                    fresh_detail = _wait_for_mr_update(
                        platform, pid, number, pushed)
                    if not fresh_detail:
                        fresh_detail = mr_detail
                    if not fresh_detail:
                        fresh_detail = MRDetail(
                            number=number, diff_refs=mr.diff_refs, changes=[])
                    post_review_comments(
                        platform, pid, fresh_detail, summary,
                        prefix="\U0001f916 forgewright: pushed changes.\n\n")
                else:
                    try:
                        fresh_detail = platform.mr_changes(pid, number)
                    except Exception as e:
                        logging.warning(
                            "re-fetch of MR !%d changes failed: %s \u2014 "
                            "using earlier diff_refs", number, e)
                        fresh_detail = mr_detail
                    if not fresh_detail:
                        fresh_detail = MRDetail(
                            number=number, diff_refs=mr.diff_refs, changes=[])
                    post_review_comments(
                        platform, pid, fresh_detail, summary)
        finally:
            cleanup_worktree(mirror, wt)

    proj = state.proj(pid)
    proj["merge_requests"][str(number)] = {
        "branch": branch,
        "fingerprint": fingerprint_mr(mr, notes, pipelines, cfg.bot_username),
        "last_run_at": datetime.now(timezone.utc).isoformat(),
    }
    state.save()


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

    lock_path = cfg.lock_dir / f"{pid}-{slugify(branch, 80)}.lock"
    with file_lock(lock_path) as ok:
        if not ok:
            return

        mirror = clone_or_update_mirror(cfg, platform, project)
        wt = make_worktree(cfg, mirror, pid, branch, base_branch)
        try:
            prev_fp = (prev or {}).get("fingerprint") or {}
            prev_note_id = prev_fp.get("last_note_id") or 0
            new_discussions = []
            for d in discussions:
                new_notes_in_d = [
                    n for n in d.notes
                    if not n.system
                    and n.author.username != cfg.bot_username
                    and (not prev_note_id or n.id > prev_note_id)
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
                    log_block = _fetch_failed_job_logs(platform, pid, p)
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
            summary = result.summary or "_(no summary written)_"

            pushed = ""
            try:
                pushed = push_branch(cfg, platform, wt, branch, base_branch)
            except subprocess.CalledProcessError as e:
                logging.error("push failed: %s", e)

            if pushed:
                _wait_for_mr_update(platform, pid, number, pushed)
                post_mr_responses(platform, pid, number, summary,
                                  prefix="\U0001f916 forgewright: pushed updates.\n\n")
            elif result.ok:
                post_mr_responses(platform, pid, number, summary)
            else:
                platform.comment_mr(
                    pid, number,
                    f"\U0001f916 forgewright: run failed.\n\n"
                    f"<details><summary>log tail</summary>\n\n"
                    f"```\n{result.output[-3000:]}\n```\n</details>")
        finally:
            cleanup_worktree(mirror, wt)

    proj = state.proj(pid)
    proj["merge_requests"][str(number)] = {
        "branch": branch,
        "fingerprint": fingerprint_mr(mr, notes, pipelines, cfg.bot_username),
        "last_run_at": datetime.now(timezone.utc).isoformat(),
    }
    state.save()


def process_project(cfg: Config, platform: Platform, agent: Agent,
                    state: State, project: Project) -> None:
    """Process all issues and MRs in a single project."""
    from forgewright.helpers import shortdt

    pid = project.id
    proj_state = state.proj(pid)
    last_checked = proj_state.get("last_checked_at")
    updated_after = last_checked

    logging.info("PROJECT %s (since %s)", project.path,
                 shortdt(last_checked))

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
        prev = proj_state["issues"].get(str(number))
        go, reason = should_process_issue(issue, notes, prev,
                                          cfg.bot_username)
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
        notes = notes_from_discussions(discussions)
        prev = proj_state["merge_requests"].get(str(number))
        go, reason = should_process_mr(
            mr, notes, pipelines, prev, cfg.bot_username, cfg.branch_prefix)
        if not go:
            logging.debug("mr %d skip: %s", number, reason)
            continue
        logging.info("mr %d TRIGGER: %s", number, reason)
        try:
            if is_review_mode(mr, cfg.bot_username, cfg.branch_prefix):
                logging.info("mr %d REVIEW MODE (not authored by bot)",
                             number)
                handle_mr_review(cfg, platform, agent, state, project, mr,
                                 discussions, pipelines, prev, reason)
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
        proj_state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    state.save()
