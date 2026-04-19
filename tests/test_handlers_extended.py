"""Extended handler tests — edge cases and less-covered paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forgewright.agent.base import AgentResult
from forgewright.handlers import (
    _fetch_failed_job_logs,
    _wait_for_mr_update,
    handle_issue,
    handle_mr,
    handle_mr_review,
)
from forgewright.state import State
from forgewright.types import (
    DiffRefs, Discussion, Job, MergeRequest, MRDetail, Pipeline, User,
)
from tests.conftest import (
    MockAgent, MockPlatform, make_issue, make_mr, make_note, make_project,
)

_HANDLER_PATCHES = [
    "forgewright.handlers.clone_or_update_mirror",
    "forgewright.handlers.make_worktree",
    "forgewright.handlers.cleanup_worktree",
    "forgewright.handlers.push_branch",
    "forgewright.handlers.run",
]

FAKE_SHA = "a" * 40


def _setup_handler_mocks(mocks, tmp_path, push_returns=""):
    mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run = mocks
    mock_clone.return_value = tmp_path / "mirror"
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    (wt / ".claude").mkdir(exist_ok=True)
    mock_make_wt.return_value = wt
    mock_push.return_value = push_returns
    return wt


class TestWaitForMrUpdate:
    @patch("forgewright.handlers.time.sleep")
    def test_returns_immediately_on_match(self, mock_sleep):
        platform = MockPlatform()
        platform.mr_changes = lambda pid, nr: MRDetail(
            number=nr,
            diff_refs=DiffRefs(base_sha="", head_sha="abc123", start_sha=""),
            changes=[],
        )

        result = _wait_for_mr_update(platform, 1, 5, "abc123")
        assert result is not None
        assert result.diff_refs.head_sha == "abc123"
        mock_sleep.assert_not_called()

    @patch("forgewright.handlers.time.sleep")
    def test_returns_none_on_all_failures(self, mock_sleep):
        platform = MockPlatform()
        platform.mr_changes = MagicMock(side_effect=RuntimeError("fail"))

        result = _wait_for_mr_update(platform, 1, 5, "abc123",
                                     max_attempts=3, delay=0)
        assert result is None

    @patch("forgewright.handlers.time.sleep")
    def test_returns_stale_after_max_attempts(self, mock_sleep):
        platform = MockPlatform()
        platform.mr_changes = lambda pid, nr: MRDetail(
            number=nr,
            diff_refs=DiffRefs(base_sha="", head_sha="old", start_sha=""),
            changes=[],
        )

        result = _wait_for_mr_update(platform, 1, 5, "new-sha",
                                     max_attempts=2, delay=0)
        assert result is not None
        assert result.diff_refs.head_sha == "old"


class TestFetchFailedJobLogsExtended:
    def test_truncates_after_total_limit(self, mock_platform):
        mock_platform.pipeline_jobs = lambda pid, plid: [
            Job(id=i, name=f"job{i}", stage="test", status="failed")
            for i in range(5)
        ]
        mock_platform.job_log = lambda pid, jid: "x" * 5000

        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=100, sha="abc", status="failed", web_url=""))
        assert "truncated" in result
        assert "more failed jobs not shown" in result

    def test_empty_pipeline_no_id(self, mock_platform):
        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=None, sha="", status="failed", web_url=""))
        assert result == ""


class TestHandleIssueEdgeCases:
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_existing_mr_no_push_ok_result(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        mock_platform.find_mr_for_branch = lambda pid, branch: MergeRequest(
            number=99, title="", description="",
            web_url="https://example.com/mr/99",
            source_branch=branch, target_branch="main", updated_at="",
            sha="", author=User(username="forgewright"))

        agent = MockAgent(ok=True, summary="Answered the question.")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=5, description="@forgewright what is this?")

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, [])

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_issue"]
        assert len(comment_calls) == 1
        assert "Answered the question" in comment_calls[0][1][2]

    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_existing_mr_no_push_failed_result(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        mock_platform.find_mr_for_branch = lambda pid, branch: MergeRequest(
            number=99, title="", description="",
            web_url="https://example.com/mr/99",
            source_branch=branch, target_branch="main", updated_at="",
            sha="", author=User(username="forgewright"))

        agent = MockAgent(ok=False, output="crash log", summary="failed")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=5, description="@forgewright fix")

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, [])

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_issue"]
        assert len(comment_calls) == 1
        assert "failed" in comment_calls[0][1][2].lower()


class TestHandleMrEdgeCases:
    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_no_push_ok_result(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        agent = MockAgent(ok=True, summary="## Reply to discussion d1\nDone.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, source_branch="forgewright/fix",
                     author="forgewright")

        handle_mr(tmp_config, mock_platform, agent, state, project,
                  mr, [], [], None, "test")

        reply_calls = [c for c in mock_platform.calls
                       if c[0] == "reply_to_discussion"]
        assert len(reply_calls) >= 1

    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_failed_result_posts_log(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        agent = MockAgent(ok=False, output="error output", summary="")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, source_branch="forgewright/fix", author="forgewright")

        handle_mr(tmp_config, mock_platform, agent, state, project,
                  mr, [], [], None, "test")

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert "failed" in comment_calls[0][1][2]

    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_new_discussions_filtered_by_prev(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        agent = MockAgent(ok=True, summary="handled")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, source_branch="forgewright/fix", author="forgewright")

        discussions = [
            Discussion(id="d1", notes=[
                make_note(note_id=5, author="alice", body="old comment"),
            ]),
            Discussion(id="d2", notes=[
                make_note(note_id=15, author="bob", body="new comment"),
            ]),
        ]
        prev = {"fingerprint": {"last_note_id": 10}}

        handle_mr(tmp_config, mock_platform, agent, state, project,
                  mr, discussions, [], prev, "new comment")


class TestHandleMrReviewEdgeCases:
    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_failed_review_posts_error(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        agent = MockAgent(ok=False, output="review crash", summary="")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, author="alice", source_branch="feature-x")

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         project, mr, [], [], None, "test")

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert "failed" in comment_calls[0][1][2]

    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_mr_changes_failure_continues(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        mock_platform.mr_changes = MagicMock(
            side_effect=RuntimeError("API fail"))

        agent = MockAgent(ok=True, summary="## General\nReview done.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, author="alice", source_branch="feature-x")

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         project, mr, [], [], None, "test")

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) >= 1

    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_no_push_fetches_fresh_mr(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        call_count = {"changes": 0}

        def track_mr_changes(pid, nr):
            call_count["changes"] += 1
            return MRDetail(
                number=nr,
                diff_refs=DiffRefs(base_sha="", head_sha="abc", start_sha=""),
                changes=[],
            )

        mock_platform.mr_changes = track_mr_changes

        agent = MockAgent(ok=True, summary="## General\nLGTM.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, author="alice", source_branch="feature-x")

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         project, mr, [], [], None, "test")

        assert call_count["changes"] >= 2
