"""GitLab implementation of the Platform interface."""

from __future__ import annotations

import hmac
import time
from typing import Any, Iterable

import requests

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

RELEVANT_EVENTS = frozenset({
    "Issue Hook",
    "Confidential Issue Hook",
    "Note Hook",
    "Confidential Note Hook",
    "Merge Request Hook",
    "Pipeline Hook",
})

RELEVANT_KINDS = frozenset({
    "issue",
    "note",
    "merge_request",
    "pipeline",
})


def _parse_user(data: dict) -> User:
    return User(username=data.get("username", ""), id=data.get("id"))


def _parse_diff_refs(data: dict | None) -> DiffRefs | None:
    if not data:
        return None
    base = data.get("base_sha", "")
    head = data.get("head_sha", "")
    if not (base and head):
        return None
    return DiffRefs(
        base_sha=base,
        head_sha=head,
        start_sha=data.get("start_sha"),
    )


def _parse_note_position(data: dict | None) -> NotePosition | None:
    if not data:
        return None
    return NotePosition(
        new_path=data.get("new_path"),
        old_path=data.get("old_path"),
        new_line=data.get("new_line"),
        old_line=data.get("old_line"),
    )


def _parse_note(data: dict) -> Note:
    return Note(
        id=data.get("id", 0),
        body=data.get("body", ""),
        created_at=data.get("created_at", ""),
        author=_parse_user(data.get("author") or {}),
        system=bool(data.get("system")),
        type=data.get("type"),
        position=_parse_note_position(data.get("position")),
    )


def _parse_project(data: dict) -> Project:
    return Project(
        id=data["id"],
        path=data.get("path_with_namespace", ""),
        web_url=data.get("web_url", ""),
        default_branch=data.get("default_branch", "main"),
        http_clone_url=data.get("http_url_to_repo", ""),
    )


def _parse_issue(data: dict) -> Issue:
    return Issue(
        number=data["iid"],
        title=data.get("title", ""),
        description=data.get("description"),
        web_url=data.get("web_url", ""),
        updated_at=data.get("updated_at", ""),
        labels=data.get("labels") or [],
    )


def _parse_mr(data: dict) -> MergeRequest:
    return MergeRequest(
        number=data["iid"],
        title=data.get("title", ""),
        description=data.get("description"),
        web_url=data.get("web_url", ""),
        source_branch=data.get("source_branch", ""),
        target_branch=data.get("target_branch", ""),
        updated_at=data.get("updated_at", ""),
        sha=data.get("sha", ""),
        author=_parse_user(data.get("author") or {}),
        labels=data.get("labels") or [],
        reviewers=[_parse_user(r) for r in (data.get("reviewers") or [])],
        assignees=[_parse_user(a) for a in (data.get("assignees") or [])],
        diff_refs=_parse_diff_refs(data.get("diff_refs")),
    )


def _parse_pipeline(data: dict) -> Pipeline:
    return Pipeline(
        id=data.get("id"),
        sha=data.get("sha", ""),
        status=data.get("status", ""),
        web_url=data.get("web_url", ""),
    )


def _parse_job(data: dict) -> Job:
    return Job(
        id=data.get("id", 0),
        name=data.get("name", ""),
        stage=data.get("stage", ""),
        status=data.get("status", ""),
    )


def _parse_diff_change(data: dict) -> DiffChange:
    return DiffChange(
        diff=data.get("diff", ""),
        old_path=data.get("old_path", ""),
        new_path=data.get("new_path", ""),
        renamed_file=bool(data.get("renamed_file")),
        deleted_file=bool(data.get("deleted_file")),
        new_file=bool(data.get("new_file")),
    )


class GitLabPlatform(Platform):
    """GitLab v4 API client."""

    def __init__(self, base_url: str, token: str,
                 request_timeout: int = 30, http_retries: int = 3):
        self._base = f"{base_url}/api/v4"
        self._token = token
        self._timeout = request_timeout
        self._retries = http_retries
        self._session = requests.Session()
        self._session.headers.update({"PRIVATE-TOKEN": token})

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self._base}{path}"
        last_exc: Exception | None = None
        for attempt in range(self._retries):
            try:
                r = self._session.request(method, url,
                                          timeout=self._timeout,
                                          **kwargs)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
                return r
            except (requests.ConnectionError, requests.Timeout,
                    requests.HTTPError) as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"request failed after retries: {last_exc}")

    def _paginate(self, path: str, **params) -> Iterable[dict]:
        params = dict(params)
        params.setdefault("per_page", 100)
        url: str | None = f"{self._base}{path}"
        while url:
            r = self._req("GET", url, params=params)
            r.raise_for_status()
            for item in r.json():
                yield item
            url = r.links.get("next", {}).get("url")
            params = None  # next-page URL already includes params

    # --- Platform interface implementation ---

    def current_user(self) -> User:
        r = self._req("GET", "/user")
        r.raise_for_status()
        return _parse_user(r.json())

    def list_member_projects(self) -> list[Project]:
        return [_parse_project(d) for d in self._paginate(
            "/projects",
            membership="true",
            simple="true",
            archived="false",
            order_by="last_activity_at")]

    def project(self, project_id: ProjectID) -> Project:
        r = self._req("GET", f"/projects/{project_id}")
        r.raise_for_status()
        return _parse_project(r.json())

    def list_issues(self, project_id: ProjectID,
                    updated_after: str | None) -> list[Issue]:
        params: dict[str, str] = {"state": "opened", "scope": "all"}
        if updated_after:
            params["updated_after"] = updated_after
        return [_parse_issue(d) for d in
                self._paginate(f"/projects/{project_id}/issues", **params)]

    def list_mrs(self, project_id: ProjectID,
                 updated_after: str | None) -> list[MergeRequest]:
        params: dict[str, str] = {"state": "opened", "scope": "all"}
        if updated_after:
            params["updated_after"] = updated_after
        return [_parse_mr(d) for d in self._paginate(
            f"/projects/{project_id}/merge_requests", **params)]

    def issue_notes(self, project_id: ProjectID,
                    issue_number: int) -> list[Note]:
        return [_parse_note(d) for d in self._paginate(
            f"/projects/{project_id}/issues/{issue_number}/notes",
            sort="asc", order_by="created_at")]

    def mr_discussions(self, project_id: ProjectID,
                       mr_number: int) -> list[Discussion]:
        raw = list(self._paginate(
            f"/projects/{project_id}/merge_requests/{mr_number}/discussions"))
        return [
            Discussion(
                id=d.get("id", ""),
                notes=[_parse_note(n) for n in d.get("notes", [])],
            )
            for d in raw
        ]

    def mr_pipelines(self, project_id: ProjectID,
                     mr_number: int) -> list[Pipeline]:
        return [_parse_pipeline(d) for d in self._paginate(
            f"/projects/{project_id}/merge_requests/{mr_number}/pipelines")]

    def pipeline_jobs(self, project_id: ProjectID,
                      pipeline_id: int | str) -> list[Job]:
        return [_parse_job(d) for d in self._paginate(
            f"/projects/{project_id}/pipelines/{pipeline_id}/jobs")]

    def job_log(self, project_id: ProjectID, job_id: int | str) -> str:
        r = self._req("GET", f"/projects/{project_id}/jobs/{job_id}/trace")
        r.raise_for_status()
        return r.text

    def mr_changes(self, project_id: ProjectID,
                   mr_number: int) -> MRDetail:
        r = self._req(
            "GET",
            f"/projects/{project_id}/merge_requests/{mr_number}/changes",
            params={"access_raw_diffs": "true"})
        r.raise_for_status()
        data = r.json()
        return MRDetail(
            number=data.get("iid", mr_number),
            diff_refs=_parse_diff_refs(data.get("diff_refs")),
            changes=[_parse_diff_change(c) for c in data.get("changes", [])],
        )

    def find_mr_for_branch(self, project_id: ProjectID,
                           branch: str) -> MergeRequest | None:
        r = self._req(
            "GET",
            f"/projects/{project_id}/merge_requests",
            params={"source_branch": branch, "state": "opened"})
        r.raise_for_status()
        data = r.json()
        return _parse_mr(data[0]) if data else None

    def create_mr(self, project_id: ProjectID, *,
                  source_branch: str, target_branch: str,
                  title: str, description: str,
                  draft: bool = True,
                  labels: list[str] | None = None) -> MergeRequest:
        if draft and not title.lower().startswith("draft:"):
            title = f"Draft: {title}"
        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
            "squash": False,
        }
        if labels:
            payload["labels"] = ",".join(labels)
        r = self._req("POST",
                       f"/projects/{project_id}/merge_requests",
                       json=payload)
        r.raise_for_status()
        return _parse_mr(r.json())

    def update_mr(self, project_id: ProjectID, mr_number: int,
                  **fields) -> None:
        r = self._req("PUT",
                       f"/projects/{project_id}/merge_requests/{mr_number}",
                       json=fields)
        r.raise_for_status()

    def comment_issue(self, project_id: ProjectID, issue_number: int,
                      body: str) -> None:
        self._req("POST",
                  f"/projects/{project_id}/issues/{issue_number}/notes",
                  json={"body": body}).raise_for_status()

    def comment_mr(self, project_id: ProjectID, mr_number: int,
                   body: str) -> None:
        self._req("POST",
                  f"/projects/{project_id}/merge_requests/{mr_number}/notes",
                  json={"body": body}).raise_for_status()

    def reply_to_discussion(self, project_id: ProjectID, mr_number: int,
                            discussion_id: str, body: str) -> None:
        self._req(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_number}"
            f"/discussions/{discussion_id}/notes",
            json={"body": body}).raise_for_status()

    def create_mr_discussion(self, project_id: ProjectID, mr_number: int,
                             body: str,
                             position: dict | None = None) -> None:
        payload: dict[str, Any] = {"body": body}
        if position:
            payload["position"] = position
        r = self._req(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_number}/discussions",
            json=payload)
        r.raise_for_status()

    # -- Git operations --------------------------------------------------------

    def clone_url(self, project: Project) -> str:
        return project.http_clone_url.replace("://", "://oauth2@", 1)

    @property
    def git_token(self) -> str:
        return self._token

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
        token = headers.get("X-Gitlab-Token", "")
        return hmac.compare_digest(token, secret)

    def parse_webhook_event(
        self, headers: dict, payload: dict,
    ) -> tuple[str, ProjectID | None, bool]:
        event_type = headers.get("X-Gitlab-Event", "")
        object_kind = payload.get("object_kind", "")

        is_relevant = (
            event_type in RELEVANT_EVENTS or object_kind in RELEVANT_KINDS
        )

        project_data = payload.get("project") or {}
        project_id = project_data.get("id")

        effective_event = event_type or object_kind
        return effective_event, project_id, is_relevant
