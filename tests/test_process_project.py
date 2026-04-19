"""Tests for process_project orchestration in forgewright.handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forgewright.agent.base import AgentResult
from forgewright.handlers import process_project
from forgewright.state import State
from forgewright.types import DiffRefs, Discussion, MRDetail
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


def _setup_handler_mocks(mocks, tmp_path, push_returns=""):
    mock_clone, mock_make_wt, mock_cleanup, mock_push, mock_run = mocks
    mock_clone.return_value = tmp_path / "mirror"
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    (wt / ".claude").mkdir(exist_ok=True)
    mock_make_wt.return_value = wt
    mock_push.return_value = push_returns
    return wt


class TestProcessProject:
    def test_processes_issue_with_mention(self, tmp_config, tmp_path):
        platform = MockPlatform()
        platform.list_issues = lambda pid, after: [
            make_issue(number=1, description="@forgewright fix this")
        ]
        platform.issue_notes = lambda pid, nr: [
            make_note(author="alice", body="@forgewright help")
        ]

        agent = MockAgent(ok=True, summary="Fixed it.")
        state = State(tmp_path / "state.json")
        project = make_project()

        with patch(_HANDLER_PATCHES[0]) as mc, \
             patch(_HANDLER_PATCHES[1]) as mw, \
             patch(_HANDLER_PATCHES[2]) as mcl, \
             patch(_HANDLER_PATCHES[3]) as mp, \
             patch(_HANDLER_PATCHES[4]) as mr:
            _setup_handler_mocks((mc, mw, mcl, mp, mr), tmp_path)
            process_project(tmp_config, platform, agent, state, project)

        assert "1" in state.proj(42)["issues"]

    def test_skips_issue_without_mention(self, tmp_config, tmp_path):
        platform = MockPlatform()
        platform.list_issues = lambda pid, after: [
            make_issue(number=1, description="No mention here")
        ]
        platform.issue_notes = lambda pid, nr: []

        agent = MockAgent()
        state = State(tmp_path / "state.json")
        project = make_project()

        process_project(tmp_config, platform, agent, state, project)

        assert "1" not in state.proj(42)["issues"]

    def test_processes_mr_in_review_mode(self, tmp_config, tmp_path):
        platform = MockPlatform()
        platform.list_mrs = lambda pid, after: [
            make_mr(number=5, source_branch="feature/x", author="alice",
                    description="@forgewright review this")
        ]
        platform.mr_discussions = lambda pid, nr: []
        platform.mr_pipelines = lambda pid, nr: []
        platform.mr_changes = lambda pid, nr: MRDetail(
            number=nr,
            diff_refs=DiffRefs(base_sha="", head_sha="abc", start_sha=""),
            changes=[],
        )

        agent = MockAgent(ok=True, summary="## General\nLooks good.")
        state = State(tmp_path / "state.json")
        project = make_project()

        with patch(_HANDLER_PATCHES[0]) as mc, \
             patch(_HANDLER_PATCHES[1]) as mw, \
             patch(_HANDLER_PATCHES[2]) as mcl, \
             patch(_HANDLER_PATCHES[3]) as mp, \
             patch(_HANDLER_PATCHES[4]) as mr:
            _setup_handler_mocks((mc, mw, mcl, mp, mr), tmp_path)
            process_project(tmp_config, platform, agent, state, project)

        assert "5" in state.proj(42)["merge_requests"]

    def test_processes_mr_in_update_mode(self, tmp_config, tmp_path):
        platform = MockPlatform()
        platform.list_mrs = lambda pid, after: [
            make_mr(number=3, source_branch="forgewright/issue-1", author="forgewright")
        ]
        platform.mr_discussions = lambda pid, nr: [
            Discussion(id="d1", notes=[
                make_note(note_id=1, author="alice", body="please fix X")
            ])
        ]
        platform.mr_pipelines = lambda pid, nr: []
        platform.mr_changes = lambda pid, nr: MRDetail(
            number=nr,
            diff_refs=DiffRefs(base_sha="", head_sha="abc", start_sha=""),
            changes=[],
        )

        agent = MockAgent(ok=True, summary="Fixed X.")
        state = State(tmp_path / "state.json")
        project = make_project()

        with patch(_HANDLER_PATCHES[0]) as mc, \
             patch(_HANDLER_PATCHES[1]) as mw, \
             patch(_HANDLER_PATCHES[2]) as mcl, \
             patch(_HANDLER_PATCHES[3]) as mp, \
             patch(_HANDLER_PATCHES[4]) as mr:
            _setup_handler_mocks((mc, mw, mcl, mp, mr), tmp_path)
            process_project(tmp_config, platform, agent, state, project)

        assert "3" in state.proj(42)["merge_requests"]

    def test_handler_crash_rewinds_timestamp(self, tmp_config, tmp_path):
        platform = MockPlatform()
        platform.list_issues = lambda pid, after: [
            make_issue(number=1, description="@forgewright fix",
                       updated_at="2024-06-01T00:00:00Z")
        ]
        platform.issue_notes = lambda pid, nr: [
            make_note(author="alice", body="@forgewright help")
        ]

        agent = MockAgent()
        state = State(tmp_path / "state.json")
        project = make_project()

        with patch("forgewright.handlers.handle_issue",
                   side_effect=RuntimeError("boom")):
            process_project(tmp_config, platform, agent, state, project)

        proj_state = state.proj(42)
        assert proj_state["last_checked_at"] == "2024-06-01T00:00:00Z"

    def test_notes_fetch_failure_skips_issue(self, tmp_config, tmp_path):
        platform = MockPlatform()
        platform.list_issues = lambda pid, after: [
            make_issue(number=1, description="@forgewright fix")
        ]
        platform.issue_notes = MagicMock(side_effect=RuntimeError("API error"))

        agent = MockAgent()
        state = State(tmp_path / "state.json")
        project = make_project()

        process_project(tmp_config, platform, agent, state, project)
        assert "1" not in state.proj(42)["issues"]

    def test_updates_last_checked_at(self, tmp_config, tmp_path):
        platform = MockPlatform()
        agent = MockAgent()
        state = State(tmp_path / "state.json")
        project = make_project()

        process_project(tmp_config, platform, agent, state, project)

        assert state.proj(42).get("last_checked_at") is not None
