"""Regression tests for the workflow fixes (triggers, CI retries, forks,
state safety, webhook lock handling, agent process groups)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from forgewright.agent.base import _pgid, kill_process_group
from forgewright.decision import (
    ci_fix_trigger,
    fingerprint_mr,
    is_review_mode,
    next_ci_fix_attempts,
    should_process_mr,
)
from forgewright.git import make_worktree, push_branch
from forgewright.handlers import handle_issue, handle_mr, handle_mr_review
from forgewright.state import State
from forgewright.types import Discussion, Pipeline, User
from tests.conftest import (
    MockAgent, make_issue, make_mr, make_note, make_project,
)

BOT = "forgewright"
PREFIX = "forgewright/"


class PromptAgent(MockAgent):
    """MockAgent that remembers the last prompt."""
    last_prompt = ""

    def run(self, prompt, cwd):
        self.last_prompt = prompt
        return super().run(prompt, cwd)


def _pipe(pid, sha, status):
    return Pipeline(id=pid, sha=sha, status=status, web_url="")


# --------------------------------------------------------------------------
# CI re-trigger + attempt cap
# --------------------------------------------------------------------------

class TestCiRetrigger:
    def _bot_mr(self, sha="BBB"):
        return make_mr(source_branch="forgewright/issue-1-x", author=BOT,
                       sha=sha)

    def test_second_failure_after_bot_fix_triggers(self):
        # P1 failed → bot fixed and pushed BBB → P2 (for BBB) fails again.
        mr = self._bot_mr()
        fp = fingerprint_mr(mr, [], [_pipe(1, "AAA", "failed")], BOT)
        fp["head_sha"] = "BBB"
        prev = {"fingerprint": fp, "ci_fix_attempts": 1}
        go, reason = should_process_mr(
            mr, [], [_pipe(2, "BBB", "failed")], prev, BOT, PREFIX)
        assert go
        assert "pipeline failed" in reason

    def test_running_pipeline_does_not_trigger(self):
        mr = self._bot_mr()
        fp = fingerprint_mr(mr, [], [_pipe(1, "AAA", "failed")], BOT)
        prev = {"fingerprint": fp}
        go, _ = should_process_mr(
            mr, [], [_pipe(2, "BBB", "running")], prev, BOT, PREFIX)
        assert not go

    def test_same_failed_pipeline_does_not_retrigger(self):
        mr = self._bot_mr()
        pipes = [_pipe(1, "AAA", "failed")]
        fp = fingerprint_mr(mr, [], pipes, BOT)
        fp["labels"] = ["changed"]  # force a fingerprint diff
        prev = {"fingerprint": fp}
        go, _ = should_process_mr(mr, [], pipes, prev, BOT, PREFIX)
        assert not go

    def test_retried_pipeline_same_sha_triggers(self):
        mr = self._bot_mr()
        fp = fingerprint_mr(mr, [], [_pipe(1, "BBB", "failed")], BOT)
        assert ci_fix_trigger(
            fingerprint_mr(mr, [], [_pipe(2, "BBB", "failed")], BOT), fp)

    def test_cap_stops_automatic_retries(self):
        mr = self._bot_mr()
        fp = fingerprint_mr(mr, [], [_pipe(1, "AAA", "failed")], BOT)
        prev = {"fingerprint": fp, "ci_fix_attempts": 3}
        go, reason = should_process_mr(
            mr, [], [_pipe(2, "BBB", "failed")], prev, BOT, PREFIX,
            max_ci_fix_attempts=3)
        assert not go
        assert "attempt" in reason

    def test_human_comment_overrides_cap(self):
        mr = self._bot_mr()
        fp = fingerprint_mr(mr, [], [_pipe(1, "AAA", "failed")], BOT)
        prev = {"fingerprint": fp, "ci_fix_attempts": 3}
        notes = [make_note(note_id=9, author="alice", body="try again")]
        go, reason = should_process_mr(
            mr, notes, [_pipe(2, "BBB", "failed")], prev, BOT, PREFIX,
            max_ci_fix_attempts=3)
        assert go
        assert "new comment" in reason

    def test_attempt_counter(self):
        assert next_ci_fix_attempts(None, "first-seen, pipeline failed") == 1
        assert next_ci_fix_attempts({"ci_fix_attempts": 2},
                                    "pipeline failed") == 3
        assert next_ci_fix_attempts({"ci_fix_attempts": 2},
                                    "new comment, pipeline failed") == 1
        assert next_ci_fix_attempts({"ci_fix_attempts": 2},
                                    "new comment") == 0


# --------------------------------------------------------------------------
# Bot MR: no self-trigger from the bot's own push / description rewrite
# --------------------------------------------------------------------------

class TestBotMrNoSelfTrigger:
    def test_push_and_description_rewrite_from_issue_run_do_not_trigger(self):
        # handle_issue pushed to the existing MR and rewrote its description.
        old = make_mr(source_branch="forgewright/issue-1-x", author=BOT,
                      sha="AAA", description="old summary")
        prev = {"fingerprint": fingerprint_mr(old, [], [], BOT)}
        new = make_mr(source_branch="forgewright/issue-1-x", author=BOT,
                      sha="BBB", description="new summary")
        go, _ = should_process_mr(new, [], [], prev, BOT, PREFIX)
        assert not go


# --------------------------------------------------------------------------
# Review mode: explicit requests only
# --------------------------------------------------------------------------

class TestReviewModeTriggers:
    def _prev(self, mr, notes):
        return {"fingerprint": fingerprint_mr(mr, notes, [], BOT)}

    def test_new_push_does_not_rereview(self):
        mention = make_note(note_id=1, author="alice",
                            body="@forgewright review please")
        mr = make_mr(source_branch="feature", sha="AAA")
        prev = self._prev(mr, [mention])
        mr2 = make_mr(source_branch="feature", sha="BBB")
        go, _ = should_process_mr(mr2, [mention], [], prev, BOT, PREFIX)
        assert not go

    def test_unrelated_comment_does_not_rereview(self):
        mention = make_note(note_id=1, author="alice",
                            body="@forgewright review please",
                            created_at="2024-01-01T00:00:00Z")
        mr = make_mr(source_branch="feature")
        prev = self._prev(mr, [mention])
        lgtm = make_note(note_id=2, author="bob", body="LGTM",
                         created_at="2024-01-02T00:00:00Z")
        go, _ = should_process_mr(mr, [mention, lgtm], [], prev, BOT, PREFIX)
        assert not go

    def test_new_mention_rereviews(self):
        mention = make_note(note_id=1, author="alice",
                            body="@forgewright review please",
                            created_at="2024-01-01T00:00:00Z")
        mr = make_mr(source_branch="feature")
        prev = self._prev(mr, [mention])
        again = make_note(note_id=2, author="alice",
                          body="@forgewright again please",
                          created_at="2024-01-02T00:00:00Z")
        go, reason = should_process_mr(mr, [mention, again], [], prev, BOT,
                                       PREFIX)
        assert go
        assert "new mention" in reason

    def test_reply_in_bot_thread_triggers(self):
        mention = make_note(note_id=1, author="alice",
                            body="@forgewright review",
                            created_at="2024-01-01T00:00:00Z")
        bot_note = make_note(note_id=2, author=BOT, body="issue here",
                             created_at="2024-01-01T01:00:00Z")
        reply = make_note(note_id=3, author="alice", body="why?",
                          created_at="2024-01-02T00:00:00Z")
        mr = make_mr(source_branch="feature")
        prev = self._prev(mr, [mention, bot_note])
        discussions = [Discussion(id="d1", notes=[mention]),
                       Discussion(id="d2", notes=[bot_note, reply])]
        go, reason = should_process_mr(
            mr, [mention, bot_note, reply], [], prev, BOT, PREFIX,
            discussions=discussions)
        assert go
        assert "reply in bot thread" in reason

    def test_newly_requested_reviewer_triggers(self):
        mention = make_note(note_id=1, author="alice", body="@forgewright")
        mr = make_mr(source_branch="feature")
        prev = self._prev(mr, [mention])
        mr2 = make_mr(source_branch="feature",
                      reviewers=[User(username=BOT)])
        go, reason = should_process_mr(mr2, [mention], [], prev, BOT, PREFIX)
        assert go
        assert "review requested" in reason


# --------------------------------------------------------------------------
# Fork MRs
# --------------------------------------------------------------------------

class TestForks:
    def test_fork_with_bot_prefix_is_review_mode(self):
        mr = make_mr(source_branch="forgewright/evil", author="mallory")
        mr.is_fork = True
        assert is_review_mode(mr, BOT, PREFIX)

    def test_gitlab_parses_fork(self):
        from forgewright.platform.gitlab import _parse_mr
        base = {"iid": 3, "source_branch": "main", "target_branch": "main",
                "author": {"username": "x"}}
        assert _parse_mr({**base, "source_project_id": 2,
                          "target_project_id": 1}).is_fork
        assert not _parse_mr({**base, "source_project_id": 1,
                              "target_project_id": 1}).is_fork

    def test_github_parses_fork(self):
        from forgewright.platform.github import _parse_pr
        def pr(head_repo):
            return {"number": 4, "user": {"login": "x"},
                    "head": {"ref": "main", "sha": "h",
                             "repo": head_repo},
                    "base": {"ref": "main", "sha": "b",
                             "repo": {"full_name": "o/r"}}}
        assert _parse_pr(pr({"full_name": "fork/r"})).is_fork
        assert _parse_pr(pr(None)).is_fork  # deleted fork
        assert not _parse_pr(pr({"full_name": "o/r"})).is_fork

    @patch("forgewright.handlers.run")
    @patch("forgewright.handlers.push_branch")
    @patch("forgewright.handlers.cleanup_worktree")
    @patch("forgewright.handlers.make_worktree")
    @patch("forgewright.handlers.clone_or_update_mirror")
    def test_fork_review_detached_and_never_pushes(
            self, mock_clone, mock_make_wt, mock_cleanup, mock_push,
            mock_run, tmp_config, mock_platform, tmp_path):
        wt = tmp_path / "wt"
        (wt / ".forgewright").mkdir(parents=True)
        mock_make_wt.return_value = wt
        mock_clone.return_value = tmp_path / "mirror"
        mr = make_mr(number=7, source_branch="develop", author="mallory",
                     description="@forgewright review")
        mr.is_fork = True
        agent = PromptAgent(ok=True, summary="## General\nok")
        state = State(tmp_path / "state.json")

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         make_project(), mr, [], [], None, "first-seen")

        _, kwargs = mock_make_wt.call_args
        assert kwargs["detach_ref"] == "refs/merge-requests/7/head"
        mock_push.assert_not_called()
        assert "fork" in agent.last_prompt.lower()


# --------------------------------------------------------------------------
# Handler state safety + stable branch
# --------------------------------------------------------------------------

def _patch_git(tmp_path, push_returns=""):
    wt = tmp_path / "wt"
    (wt / ".forgewright").mkdir(parents=True, exist_ok=True)
    patches = [
        patch("forgewright.handlers.clone_or_update_mirror",
              return_value=tmp_path / "mirror"),
        patch("forgewright.handlers.make_worktree", return_value=wt),
        patch("forgewright.handlers.cleanup_worktree"),
        patch("forgewright.handlers.push_branch", return_value=push_returns),
        patch("forgewright.handlers.run"),
    ]
    return patches


class TestHandlerStateSafety:
    def test_issue_state_saved_when_posting_crashes(
            self, tmp_config, mock_platform, tmp_path):
        mock_platform.create_mr = MagicMock(side_effect=RuntimeError("422"))
        state = State(tmp_path / "state.json")
        issue = make_issue(number=5, description="@forgewright fix it")
        patches = _patch_git(tmp_path, push_returns="a" * 40)
        for p in patches:
            p.start()
        try:
            with pytest.raises(RuntimeError):
                handle_issue(tmp_config, mock_platform, MockAgent(), state,
                             make_project(), issue, [])
        finally:
            for p in patches:
                p.stop()
        saved = State(tmp_path / "state.json").proj(42)["issues"]
        assert "5" in saved
        assert saved["5"]["fingerprint"]

    def test_no_state_when_agent_never_ran(
            self, tmp_config, mock_platform, tmp_path):
        state = State(tmp_path / "state.json")
        issue = make_issue(number=5, description="@forgewright fix it")
        with patch("forgewright.handlers.clone_or_update_mirror",
                   side_effect=RuntimeError("git down")):
            with pytest.raises(RuntimeError):
                handle_issue(tmp_config, mock_platform, MockAgent(), state,
                             make_project(), issue, [])
        assert "5" not in state.proj(42)["issues"]

    def test_branch_reused_after_title_edit(
            self, tmp_config, mock_platform, tmp_path):
        state = State(tmp_path / "state.json")
        state.proj(42)["issues"]["5"] = {
            "branch": "forgewright/issue-5-old-title", "fingerprint": {}}
        issue = make_issue(number=5, title="Completely new title",
                           description="@forgewright fix")
        patches = _patch_git(tmp_path)
        mocks = [p.start() for p in patches]
        try:
            handle_issue(tmp_config, mock_platform, MockAgent(), state,
                         make_project(), issue, [])
        finally:
            for p in patches:
                p.stop()
        branch_arg = mocks[1].call_args[0][3]
        assert branch_arg == "forgewright/issue-5-old-title"

    def test_mr_ci_attempts_recorded_and_cap_note(
            self, tmp_config, mock_platform, tmp_path):
        tmp_config.max_ci_fix_attempts = 2
        state = State(tmp_path / "state.json")
        mr = make_mr(number=3, source_branch="forgewright/issue-1-x",
                     author=BOT)
        prev = {"fingerprint": {}, "ci_fix_attempts": 1}
        patches = _patch_git(tmp_path)
        for p in patches:
            p.start()
        try:
            handle_mr(tmp_config, mock_platform,
                      MockAgent(summary="## General\ntried"), state,
                      make_project(), mr, [], [_pipe(2, "abc123", "failed")],
                      prev, "pipeline failed")
        finally:
            for p in patches:
                p.stop()
        entry = state.proj(42)["merge_requests"]["3"]
        assert entry["ci_fix_attempts"] == 2
        bodies = [c[1][2] for c in mock_platform.calls
                  if c[0] == "comment_mr"]
        assert any("2/2" in b for b in bodies)


# --------------------------------------------------------------------------
# git: detached fork checkout + push guard
# --------------------------------------------------------------------------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class TestGitFixes:
    def test_detached_worktree_for_mr_ref(self, tmp_config, tmp_path):
        up = tmp_path / "up"
        up.mkdir()
        _git("init", "-q", "-b", "main", cwd=up)
        _git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
             "--allow-empty", "-m", "init", cwd=up)
        mirror = tmp_path / "mirror.git"
        _git("clone", "-q", "--mirror", str(up), str(mirror), cwd=tmp_path)
        sha = _git("rev-parse", "HEAD", cwd=up)
        _git("update-ref", "refs/merge-requests/7/head", sha, cwd=mirror)

        wt = make_worktree(tmp_config, mirror, 1, "mr-7-fork", "main",
                           detach_ref="refs/merge-requests/7/head")
        assert _git("rev-parse", "HEAD", cwd=wt) == sha
        # detached: no branch created for it
        assert _git("branch", "--list", "mr-7-fork", cwd=mirror) == ""

        with pytest.raises(RuntimeError):
            make_worktree(tmp_config, mirror, 1, "mr-8-fork", "main",
                          detach_ref="refs/merge-requests/8/head")

    def test_push_refuses_base_branch(self, tmp_config, mock_platform,
                                      tmp_path):
        with patch("forgewright.git.run") as mock_run:
            assert push_branch(tmp_config, mock_platform, tmp_path,
                               "main", "main") == ""
            mock_run.assert_not_called()


# --------------------------------------------------------------------------
# webhook: wait for the lock instead of dropping; bot username sync
# --------------------------------------------------------------------------

class TestWebhookLock:
    def test_event_waits_for_lock(self, tmp_config):
        from forgewright import webhook
        from forgewright.helpers import file_lock

        ran = threading.Event()
        lock_path = tmp_config.lock_dir / "poller.lock"
        with patch.object(webhook, "_process_event_locked",
                          side_effect=lambda *a: ran.set()):
            with file_lock(lock_path) as got:
                assert got
                t = threading.Thread(
                    target=webhook._process_event,
                    args=(tmp_config, 42, "Note Hook"),
                    kwargs={"retry_sec": 0.05, "max_wait_sec": 5})
                t.start()
                time.sleep(0.2)
                assert not ran.is_set()
                # a second event for the same project does not add a waiter
                webhook._process_event(tmp_config, 42, "Note Hook",
                                       retry_sec=0.05, max_wait_sec=5)
            t.join(timeout=5)
        assert ran.is_set()
        assert 42 not in webhook._waiting

    def test_run_server_syncs_bot_username(self, tmp_config):
        from forgewright import webhook
        tmp_config.bot_username = "wrong-name"
        fake = MagicMock()
        fake.current_user.return_value = User(username="real-bot")
        with patch.object(webhook, "create_platform", return_value=fake), \
                patch.object(webhook, "create_app") as mock_app:
            webhook.run_server(tmp_config)
        assert tmp_config.bot_username == "real-bot"
        mock_app.return_value.run.assert_called_once()


# --------------------------------------------------------------------------
# agent: process-group kill
# --------------------------------------------------------------------------

class TestAgentProcessGroup:
    def test_bogus_pid_is_never_signalled(self):
        proc = MagicMock()  # MagicMock().pid would int() to 1 — never use it
        assert _pgid(proc) is None
        kill_process_group(proc)
        proc.kill.assert_called_once()

    def test_kills_children_too(self, tmp_path):
        pidfile = tmp_path / "child.pid"
        proc = subprocess.Popen(
            ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"],
            start_new_session=True)
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        child = int(pidfile.read_text())
        kill_process_group(proc)
        proc.wait(timeout=5)
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(child, signal.SIGKILL)
            pytest.fail("child process survived the group kill")
