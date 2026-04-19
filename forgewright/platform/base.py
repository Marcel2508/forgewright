"""Abstract base class for code hosting platforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from forgewright.types import (
    DiffChange,
    DiffRefs,
    Discussion,
    Issue,
    Job,
    MergeRequest,
    MRDetail,
    Note,
    Pipeline,
    Project,
    User,
)

ProjectID = int | str


class Platform(ABC):
    """Abstract interface for a code hosting platform (GitLab, GitHub, etc.).

    ``project_id`` is typed as ``int | str`` so that platforms using numeric
    IDs (GitLab) and platforms using ``owner/repo`` strings (GitHub) are both
    supported without casting.
    """

    # -- Identity & discovery --------------------------------------------------

    @abstractmethod
    def current_user(self) -> User:
        """Return the authenticated user."""

    @abstractmethod
    def list_member_projects(self) -> list[Project]:
        """List projects the bot is a member of."""

    @abstractmethod
    def project(self, project_id: ProjectID) -> Project:
        """Fetch a single project by ID."""

    # -- Issues ----------------------------------------------------------------

    @abstractmethod
    def list_issues(self, project_id: ProjectID,
                    updated_after: str | None) -> list[Issue]:
        """List open issues, optionally filtered by updated_after."""

    @abstractmethod
    def issue_notes(self, project_id: ProjectID,
                    issue_number: int) -> list[Note]:
        """Notes/comments on an issue, sorted ascending by created_at."""

    @abstractmethod
    def comment_issue(self, project_id: ProjectID, issue_number: int,
                      body: str) -> None:
        """Post a comment on an issue."""

    # -- Merge / Pull requests -------------------------------------------------

    @abstractmethod
    def list_mrs(self, project_id: ProjectID,
                 updated_after: str | None) -> list[MergeRequest]:
        """List open merge/pull requests."""

    @abstractmethod
    def mr_discussions(self, project_id: ProjectID,
                       mr_number: int) -> list[Discussion]:
        """Discussion threads on an MR/PR."""

    @abstractmethod
    def mr_changes(self, project_id: ProjectID,
                   mr_number: int) -> MRDetail:
        """MR diff with full diff content."""

    @abstractmethod
    def find_mr_for_branch(self, project_id: ProjectID,
                           branch: str) -> MergeRequest | None:
        """Find an existing MR/PR with the given source branch."""

    @abstractmethod
    def create_mr(self, project_id: ProjectID, *,
                  source_branch: str, target_branch: str,
                  title: str, description: str,
                  draft: bool = True,
                  labels: list[str] | None = None) -> MergeRequest:
        """Create a new merge/pull request."""

    @abstractmethod
    def update_mr(self, project_id: ProjectID, mr_number: int,
                  **fields) -> None:
        """Update fields on an existing MR/PR."""

    @abstractmethod
    def comment_mr(self, project_id: ProjectID, mr_number: int,
                   body: str) -> None:
        """Post a top-level comment on an MR/PR."""

    @abstractmethod
    def reply_to_discussion(self, project_id: ProjectID, mr_number: int,
                            discussion_id: str, body: str) -> None:
        """Reply to an existing discussion thread."""

    @abstractmethod
    def create_mr_discussion(self, project_id: ProjectID, mr_number: int,
                             body: str,
                             position: dict | None = None) -> None:
        """Create a new discussion on an MR, optionally inline on a diff line."""

    # -- CI / Pipelines --------------------------------------------------------

    @abstractmethod
    def mr_pipelines(self, project_id: ProjectID,
                     mr_number: int) -> list[Pipeline]:
        """CI pipelines for an MR, newest first."""

    @abstractmethod
    def pipeline_jobs(self, project_id: ProjectID,
                      pipeline_id: int | str) -> list[Job]:
        """Jobs in a pipeline."""

    @abstractmethod
    def job_log(self, project_id: ProjectID, job_id: int | str) -> str:
        """Raw trace/log output for a single CI job."""

    # -- Git operations --------------------------------------------------------

    @abstractmethod
    def clone_url(self, project: Project) -> str:
        """Return a clone-ready URL for the project.

        GitLab inserts ``oauth2@`` into the URL; GitHub uses
        ``x-access-token@`` for app tokens or ``{user}:{pat}@`` for PATs.
        """

    @property
    @abstractmethod
    def git_token(self) -> str:
        """The token used for git push/pull authentication via GIT_ASKPASS."""

    # -- URL construction ------------------------------------------------------

    @abstractmethod
    def issue_url(self, project: Project, issue_number: int) -> str:
        """Return the web URL for an issue by number."""

    # -- Inline review positions -----------------------------------------------

    @abstractmethod
    def build_inline_comment_position(
        self,
        diff_refs: DiffRefs,
        file_path: str,
        old_path: str,
        line: int,
    ) -> dict | None:
        """Build a platform-specific position payload for an inline MR comment.

        Returns ``None`` if inline comments are not possible.  The returned
        dict is passed directly to :meth:`create_mr_discussion` as ``position``.
        """

    # -- Webhook handling ------------------------------------------------------

    @abstractmethod
    def validate_webhook(self, headers: dict, body: bytes,
                         secret: str) -> bool:
        """Check whether an incoming webhook request is authentic."""

    @abstractmethod
    def parse_webhook_event(
        self, headers: dict, payload: dict,
    ) -> tuple[str, ProjectID | None, bool]:
        """Extract event metadata from a webhook payload.

        Returns ``(event_type, project_id, is_relevant)``.
        """
