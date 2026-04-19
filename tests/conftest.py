"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from forgewright.agent.base import Agent, AgentResult
from forgewright.config import Config
from forgewright.platform.base import Platform, ProjectID
from forgewright.types import (
    DiffChange,
    DiffRefs,
    Discussion,
    Issue,
    Job,
    MergeRequest,
    MRDetail,
    Note,
    NotePosition,
    Pipeline,
    Project,
    User,
)


@pytest.fixture
def tmp_config(tmp_path: Path) -> Config:
    """Config with all paths pointing at tmp_path."""
    return Config(
        platform_url="https://git.example.com",
        platform_token="glpat-test-token",
        bot_username="forgewright",
        workdir=tmp_path / "work",
        state_file=tmp_path / "state.json",
        lock_dir=tmp_path / "locks",
        log_file=tmp_path / "bot.log",
        branch_prefix="forgewright/",
        git_user_name="Test Bot",
        git_user_email="test@example.com",
    )


class MockPlatform(Platform):
    """Platform with all methods returning canned data or doing nothing."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def current_user(self) -> User:
        return User(username="forgewright", id=1)

    def list_member_projects(self) -> list[Project]:
        return []

    def project(self, project_id: ProjectID) -> Project:
        return Project(id=project_id, path="test/proj",
                       web_url="https://git.example.com/test/proj",
                       default_branch="main", http_clone_url="")

    def list_issues(self, project_id: ProjectID,
                    updated_after: str | None) -> list[Issue]:
        return []

    def list_mrs(self, project_id: ProjectID,
                 updated_after: str | None) -> list[MergeRequest]:
        return []

    def issue_notes(self, project_id: ProjectID,
                    issue_number: int) -> list[Note]:
        return []

    def mr_discussions(self, project_id: ProjectID,
                       mr_number: int) -> list[Discussion]:
        return []

    def mr_pipelines(self, project_id: ProjectID,
                     mr_number: int) -> list[Pipeline]:
        return []

    def pipeline_jobs(self, project_id: ProjectID,
                      pipeline_id: int | str) -> list[Job]:
        return []

    def job_log(self, project_id: ProjectID, job_id: int | str) -> str:
        return ""

    def mr_changes(self, project_id: ProjectID,
                   mr_number: int) -> MRDetail:
        return MRDetail(number=mr_number, diff_refs=None, changes=[])

    def find_mr_for_branch(self, project_id: ProjectID,
                           branch: str) -> MergeRequest | None:
        return None

    def create_mr(self, project_id: ProjectID, **kwargs) -> MergeRequest:
        self._record("create_mr", project_id, **kwargs)
        return MergeRequest(
            number=1, title=kwargs.get("title", ""),
            description=kwargs.get("description", ""),
            web_url="https://git.example.com/mr/1",
            source_branch=kwargs.get("source_branch", ""),
            target_branch=kwargs.get("target_branch", ""),
            updated_at="", sha="", author=User(username="forgewright"))

    def update_mr(self, project_id: ProjectID, mr_number: int,
                  **fields) -> None:
        self._record("update_mr", project_id, mr_number, **fields)

    def comment_issue(self, project_id: ProjectID, issue_number: int,
                      body: str) -> None:
        self._record("comment_issue", project_id, issue_number, body)

    def comment_mr(self, project_id: ProjectID, mr_number: int,
                   body: str) -> None:
        self._record("comment_mr", project_id, mr_number, body)

    def reply_to_discussion(self, project_id: ProjectID, mr_number: int,
                            discussion_id: str, body: str) -> None:
        self._record("reply_to_discussion", project_id, mr_number,
                     discussion_id, body)

    def create_mr_discussion(self, project_id: ProjectID, mr_number: int,
                             body: str,
                             position: dict | None = None) -> None:
        self._record("create_mr_discussion", project_id, mr_number,
                     body, position)

    # -- Git operations --------------------------------------------------------

    def clone_url(self, project: Project) -> str:
        return project.http_clone_url.replace("://", "://oauth2@", 1)

    @property
    def git_token(self) -> str:
        return "mock-token"

    # -- URL construction ------------------------------------------------------

    def issue_url(self, project: Project, issue_number: int) -> str:
        return f"{project.web_url}/-/issues/{issue_number}"

    # -- Inline review positions -----------------------------------------------

    def build_inline_comment_position(
        self,
        diff_refs: DiffRefs,
        file_path: str,
        old_path: str,
        line: int,
    ) -> dict | None:
        if not (diff_refs.base_sha and diff_refs.head_sha
                and diff_refs.start_sha):
            return None
        return {
            "base_sha": diff_refs.base_sha,
            "head_sha": diff_refs.head_sha,
            "start_sha": diff_refs.start_sha,
            "position_type": "text",
            "new_path": file_path,
            "old_path": old_path,
            "new_line": line,
        }

    # -- Webhook handling ------------------------------------------------------

    def validate_webhook(self, headers: dict, body: bytes,
                         secret: str) -> bool:
        if not secret:
            return True
        import hmac
        token = headers.get("X-Gitlab-Token", "")
        return hmac.compare_digest(token, secret)

    def parse_webhook_event(
        self, headers: dict, payload: dict,
    ) -> tuple[str, ProjectID | None, bool]:
        event_type = headers.get("X-Gitlab-Event", "")
        object_kind = payload.get("object_kind", "")
        is_relevant = event_type != "" or object_kind in {
            "issue", "note", "merge_request", "pipeline"}
        project_data = payload.get("project") or {}
        project_id = project_data.get("id")
        return event_type or object_kind, project_id, is_relevant


@pytest.fixture
def mock_platform() -> MockPlatform:
    return MockPlatform()


class MockAgent(Agent):
    """Agent that returns a fixed result without running a subprocess."""

    def __init__(self, ok: bool = True, output: str = "",
                 summary: str = "Test summary"):
        self._ok = ok
        self._output = output
        self._summary = summary

    @property
    def name(self) -> str:
        return "MockAgent"

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        return AgentResult(
            ok=self._ok,
            output=self._output,
            summary=self._summary,
        )


@pytest.fixture
def mock_agent() -> MockAgent:
    return MockAgent()


def make_note(note_id: int = 1, author: str = "alice", body: str = "hello",
              system: bool = False, created_at: str = "2024-01-01T00:00:00Z",
              note_type: str | None = None,
              position: NotePosition | None = None) -> Note:
    """Helper to create a Note for tests."""
    return Note(
        id=note_id,
        body=body,
        created_at=created_at,
        author=User(username=author),
        system=system,
        type=note_type,
        position=position,
    )


def make_issue(number: int = 1, title: str = "Test issue",
               description: str = "A test", updated_at: str = "2024-01-01",
               labels: list[str] | None = None,
               web_url: str = "https://git.example.com/issues/1") -> Issue:
    return Issue(
        number=number,
        title=title,
        description=description,
        updated_at=updated_at,
        labels=labels or [],
        web_url=web_url,
    )


def make_mr(number: int = 1, title: str = "Test MR",
            description: str = "", source_branch: str = "feature",
            target_branch: str = "main",
            author: str = "alice", updated_at: str = "2024-01-01",
            labels: list[str] | None = None,
            sha: str = "abc123",
            reviewers: list[User] | None = None,
            assignees: list[User] | None = None,
            diff_refs: DiffRefs | None = None,
            web_url: str = "https://git.example.com/mr/1") -> MergeRequest:
    return MergeRequest(
        number=number,
        title=title,
        description=description,
        source_branch=source_branch,
        target_branch=target_branch,
        author=User(username=author),
        updated_at=updated_at,
        labels=labels or [],
        sha=sha,
        reviewers=reviewers or [],
        assignees=assignees or [],
        diff_refs=diff_refs,
        web_url=web_url,
    )


def make_project(pid: int | str = 42, path: str = "test/proj",
                 web_url: str = "https://git.example.com/test/proj",
                 default_branch: str = "main",
                 http_clone_url: str = "https://git.example.com/test/proj.git"
                 ) -> Project:
    return Project(
        id=pid, path=path, web_url=web_url,
        default_branch=default_branch, http_clone_url=http_clone_url,
    )
