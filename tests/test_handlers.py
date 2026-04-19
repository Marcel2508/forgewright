"""Tests for forgewright.handlers -- integration-style with mocked deps."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forgewright.agent.base import AgentResult
from forgewright.handlers import _fetch_failed_job_logs, handle_issue, handle_mr, handle_mr_review
from forgewright.types import DiffRefs, Job, MergeRequest, MRDetail, Pipeline, User
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


class TestHandleIssueQAPath:
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_qa_posts_comment(self, mock_clone, mock_make_wt, mock_cleanup,
                              mock_push, mock_run,
                              tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        agent = MockAgent(ok=True, summary="The answer is 42.")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=5, title="Question",
                           description="@forgewright what is the answer?")
        notes = [make_note(author="alice", body="@forgewright what is the answer?")]

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, notes)

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_issue"]
        assert len(comment_calls) == 1
        assert "The answer is 42" in comment_calls[0][1][2]

    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_failure_includes_log(self, mock_clone, mock_make_wt, mock_cleanup,
                                  mock_push, mock_run,
                                  tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        agent = MockAgent(ok=False, output="error occurred",
                          summary="Something went wrong.")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=5, description="@forgewright fix")

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, [])

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_issue"]
        assert len(comment_calls) == 1
        body = comment_calls[0][1][2]
        assert "Something went wrong" in body
        assert "log tail" in body


class _CapturingAgent(MockAgent):
    """Agent that records the prompt it was handed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.last_prompt: str | None = None

    def run(self, prompt, cwd):
        self.last_prompt = prompt
        return super().run(prompt, cwd)


class TestPromptBotUsername:
    """Prompt templates should render with the configured bot_username,
    not the hardcoded literal 'claude'."""

    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_issue_prompt_uses_configured_mention(
            self, mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run,
            tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        tmp_config.bot_username = "jarvis"
        agent = _CapturingAgent(ok=True, summary="done")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=1, description="@jarvis hello")

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, [])

        assert agent.last_prompt is not None
        assert "@jarvis" in agent.last_prompt
        assert "tagged @forgewright" not in agent.last_prompt

    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value="")
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_review_prompt_uses_configured_mention(
            self, mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run,
            mock_sleep, tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns="")

        tmp_config.bot_username = "jarvis"
        agent = _CapturingAgent(ok=True, summary="## General\nOK.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, author="alice", source_branch="feature-x",
                     description="@jarvis please review")

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         project, mr, [], [], None, "test")

        assert agent.last_prompt is not None
        assert "@jarvis" in agent.last_prompt
        assert "mentioned by @forgewright" not in agent.last_prompt


class TestHandleIssueCodePath:
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value=FAKE_SHA)
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_creates_mr(self, mock_clone, mock_make_wt, mock_cleanup,
                        mock_push, mock_run,
                        tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns=FAKE_SHA)

        agent = MockAgent(ok=True, summary="Implemented the feature.")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=5, title="Add feature",
                           description="@forgewright add feature")

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, [])

        create_calls = [c for c in mock_platform.calls
                        if c[0] == "create_mr"]
        assert len(create_calls) == 1

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_issue"]
        assert len(comment_calls) == 1
        assert "Draft MR" in comment_calls[0][1][2]

    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value=FAKE_SHA)
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_updates_existing_mr(self, mock_clone, mock_make_wt, mock_cleanup,
                                 mock_push, mock_run,
                                 tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns=FAKE_SHA)

        mock_platform.find_mr_for_branch = lambda pid, branch: MergeRequest(
            number=99, title="", description="",
            web_url="https://example.com/mr/99",
            source_branch=branch, target_branch="main", updated_at="",
            sha="", author=User(username="forgewright"))

        agent = MockAgent(ok=True, summary="Updated the implementation.")
        state = State(tmp_path / "state.json")
        project = make_project()
        issue = make_issue(number=5, title="Fix bug",
                           description="@forgewright fix bug")

        handle_issue(tmp_config, mock_platform, agent, state, project,
                     issue, [])

        update_calls = [c for c in mock_platform.calls
                        if c[0] == "update_mr"]
        assert len(update_calls) == 1
        create_calls = [c for c in mock_platform.calls
                        if c[0] == "create_mr"]
        assert len(create_calls) == 0


class TestFetchFailedJobLogs:
    def test_returns_logs_for_failed_jobs(self, mock_platform):
        mock_platform.pipeline_jobs = lambda pid, plid: [
            Job(id=10, name="build", stage="build", status="success"),
            Job(id=11, name="test", stage="test", status="failed"),
        ]
        mock_platform.job_log = lambda pid, jid: "FAIL: test_foo expected 1 got 2"

        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=100, sha="abc", status="failed", web_url=""))
        assert "## Failed job logs" in result
        assert "### Job: test (stage: test)" in result
        assert "FAIL: test_foo expected 1 got 2" in result

    def test_empty_when_no_failed_jobs(self, mock_platform):
        mock_platform.pipeline_jobs = lambda pid, plid: [
            Job(id=10, name="build", stage="build", status="success"),
        ]
        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=100, sha="abc", status="failed", web_url=""))
        assert result == ""

    def test_empty_when_no_pipeline_id(self, mock_platform):
        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=None, sha="", status="failed", web_url=""))
        assert result == ""

    def test_truncates_long_logs(self, mock_platform):
        mock_platform.pipeline_jobs = lambda pid, plid: [
            Job(id=11, name="test", stage="test", status="failed"),
        ]
        long_log = "x" * 10000
        mock_platform.job_log = lambda pid, jid: long_log

        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=100, sha="abc", status="failed", web_url=""))
        assert "…(truncated)" in result
        assert len(result) < 10000

    def test_handles_jobs_fetch_failure(self, mock_platform):
        def raise_err(pid, plid):
            raise RuntimeError("API error")
        mock_platform.pipeline_jobs = raise_err

        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=100, sha="abc", status="failed", web_url=""))
        assert result == ""

    def test_handles_log_fetch_failure(self, mock_platform):
        mock_platform.pipeline_jobs = lambda pid, plid: [
            Job(id=11, name="test", stage="test", status="failed"),
        ]
        def raise_err(pid, jid):
            raise RuntimeError("API error")
        mock_platform.job_log = raise_err

        result = _fetch_failed_job_logs(
            mock_platform, 1,
            Pipeline(id=100, sha="abc", status="failed", web_url=""))
        assert result == ""


class TestHandleMrReview:
    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value=FAKE_SHA)
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_waits_for_push_before_posting(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns=FAKE_SHA)

        mock_platform.mr_changes = lambda pid, nr: MRDetail(
            number=nr,
            diff_refs=DiffRefs(base_sha="base000", head_sha=FAKE_SHA,
                               start_sha="start000"),
            changes=[],
        )

        agent = MockAgent(ok=True, summary="## General\nLooks good.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, author="alice", source_branch="feature-x")
        discussions = []
        pipelines = []

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         project, mr, discussions, pipelines, None, "test")

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) >= 1
        assert "pushed changes" in comment_calls[0][1][2]

    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value=FAKE_SHA)
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_polls_until_sha_matches(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns=FAKE_SHA)

        call_count = 0

        def mr_changes_side_effect(pid, nr):
            nonlocal call_count
            call_count += 1
            sha = FAKE_SHA if call_count >= 4 else "old_sha"
            return MRDetail(
                number=nr,
                diff_refs=DiffRefs(base_sha="base000", head_sha=sha,
                                   start_sha="start000"),
                changes=[],
            )

        mock_platform.mr_changes = mr_changes_side_effect

        agent = MockAgent(ok=True, summary="## General\nLooks good.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, author="alice", source_branch="feature-x")

        handle_mr_review(tmp_config, mock_platform, agent, state,
                         project, mr, [], [], None, "test")

        assert call_count >= 4
        assert mock_sleep.call_count >= 2


class TestHandleMr:
    @patch("forgewright.handlers.time.sleep")
    @patch(_HANDLER_PATCHES[4])
    @patch(_HANDLER_PATCHES[3], return_value=FAKE_SHA)
    @patch(_HANDLER_PATCHES[2])
    @patch(_HANDLER_PATCHES[1])
    @patch(_HANDLER_PATCHES[0])
    def test_waits_for_push_before_posting(
            self, mock_clone, mock_make_wt, mock_cleanup,
            mock_push, mock_run, mock_sleep,
            tmp_config, mock_platform, tmp_path):
        from forgewright.state import State

        _setup_handler_mocks(
            (mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run),
            tmp_path, push_returns=FAKE_SHA)

        mock_platform.mr_changes = lambda pid, nr: MRDetail(
            number=nr,
            diff_refs=DiffRefs(base_sha="", head_sha=FAKE_SHA, start_sha=""),
            changes=[],
        )

        agent = MockAgent(ok=True, summary="## General\nFixed it.")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=10, source_branch="forgewright/fix-bug",
                     author="forgewright")
        discussions = []
        pipelines = []

        handle_mr(tmp_config, mock_platform, agent, state,
                  project, mr, discussions, pipelines, None, "test")

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) >= 1
        assert "pushed updates" in comment_calls[0][1][2]
