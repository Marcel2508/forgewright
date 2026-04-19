"""GitHub implementation of the Platform interface."""

from __future__ import annotations

import hashlib
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
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "check_run",
    "check_suite",
    "workflow_run",
})


def _parse_user(data: dict) -> User:
    return User(username=data.get("login", ""), id=data.get("id"))


def _parse_project(data: dict) -> Project:
    return Project(
        id=data.get("full_name", ""),
        path=data.get("full_name", ""),
        web_url=data.get("html_url", ""),
        default_branch=data.get("default_branch", "main"),
        http_clone_url=data.get("clone_url", ""),
    )


def _parse_issue(data: dict) -> Issue:
    return Issue(
        number=data["number"],
        title=data.get("title", ""),
        description=data.get("body"),
        web_url=data.get("html_url", ""),
        updated_at=data.get("updated_at", ""),
        labels=[lb["name"] for lb in (data.get("labels") or [])],
    )


def _parse_pr(data: dict) -> MergeRequest:
    head = data.get("head") or {}
    base = data.get("base") or {}
    diff_refs = None
    if head.get("sha") and base.get("sha"):
        diff_refs = DiffRefs(
            base_sha=base["sha"],
            head_sha=head["sha"],
        )
    return MergeRequest(
        number=data["number"],
        title=data.get("title", ""),
        description=data.get("body"),
        web_url=data.get("html_url", ""),
        source_branch=head.get("ref", ""),
        target_branch=base.get("ref", ""),
        updated_at=data.get("updated_at", ""),
        sha=head.get("sha", ""),
        author=_parse_user(data.get("user") or {}),
        labels=[lb["name"] for lb in (data.get("labels") or [])],
        reviewers=[_parse_user(r) for r in (data.get("requested_reviewers") or [])],
        assignees=[_parse_user(a) for a in (data.get("assignees") or [])],
        diff_refs=diff_refs,
    )


def _parse_note(data: dict, note_type: str | None = None) -> Note:
    position = None
    if data.get("path"):
        position = NotePosition(
            new_path=data.get("path"),
            old_path=data.get("path"),
            new_line=data.get("line") or data.get("original_line"),
            old_line=data.get("original_line"),
        )
    return Note(
        id=data.get("id", 0),
        body=data.get("body", ""),
        created_at=data.get("created_at", ""),
        author=_parse_user(data.get("user") or {}),
        system=False,
        type=note_type,
        position=position,
    )


def _parse_pipeline(data: dict) -> Pipeline:
    return Pipeline(
        id=data.get("id"),
        sha=data.get("head_sha", ""),
        status=_map_check_status(data.get("status", ""),
                                 data.get("conclusion")),
        web_url=data.get("html_url", ""),
    )


def _map_check_status(status: str, conclusion: str | None = None) -> str:
    if status == "completed":
        return {
            "success": "success",
            "failure": "failed",
            "cancelled": "canceled",
            "timed_out": "failed",
            "action_required": "manual",
            "neutral": "success",
            "skipped": "skipped",
        }.get(conclusion or "", conclusion or "unknown")
    return {
        "queued": "pending",
        "in_progress": "running",
        "waiting": "pending",
    }.get(status, status)


def _parse_job(data: dict) -> Job:
    return Job(
        id=data.get("id", 0),
        name=data.get("name", ""),
        stage="",
        status=_map_check_status(data.get("status", ""),
                                 data.get("conclusion")),
    )


def _owner_repo(project_id: ProjectID) -> str:
    return str(project_id)


class GitHubPlatform(Platform):
    """GitHub REST API v3 client."""

    def __init__(self, base_url: str, token: str,
                 request_timeout: int = 30, http_retries: int = 3):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = request_timeout
        self._retries = http_retries
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

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
            data = r.json()
            if isinstance(data, list):
                yield from data
            else:
                yield from data.get("items", [])
            url = r.links.get("next", {}).get("url")
            params = None

    # --- Platform interface implementation ---

    def current_user(self) -> User:
        r = self._req("GET", "/user")
        r.raise_for_status()
        return _parse_user(r.json())

    def list_member_projects(self) -> list[Project]:
        return [_parse_project(d) for d in self._paginate(
            "/user/repos",
            sort="pushed",
            direction="desc",
            affiliation="owner,collaborator,organization_member")]

    def project(self, project_id: ProjectID) -> Project:
        r = self._req("GET", f"/repos/{_owner_repo(project_id)}")
        r.raise_for_status()
        return _parse_project(r.json())

    def list_issues(self, project_id: ProjectID,
                    updated_after: str | None) -> list[Issue]:
        params: dict[str, str] = {"state": "open", "sort": "updated"}
        if updated_after:
            params["since"] = updated_after
        raw = list(self._paginate(
            f"/repos/{_owner_repo(project_id)}/issues", **params))
        return [_parse_issue(d) for d in raw if "pull_request" not in d]

    def list_mrs(self, project_id: ProjectID,
                 updated_after: str | None) -> list[MergeRequest]:
        params: dict[str, str] = {"state": "open", "sort": "updated"}
        if updated_after:
            params["since"] = updated_after
        return [_parse_pr(d) for d in self._paginate(
            f"/repos/{_owner_repo(project_id)}/pulls", **params)]

    def issue_notes(self, project_id: ProjectID,
                    issue_number: int) -> list[Note]:
        return [_parse_note(d, "issue_comment") for d in self._paginate(
            f"/repos/{_owner_repo(project_id)}/issues/{issue_number}/comments",
            sort="created", direction="asc")]

    def mr_discussions(self, project_id: ProjectID,
                       mr_number: int) -> list[Discussion]:
        owner_repo = _owner_repo(project_id)
        issue_comments = list(self._paginate(
            f"/repos/{owner_repo}/issues/{mr_number}/comments"))
        review_comments = list(self._paginate(
            f"/repos/{owner_repo}/pulls/{mr_number}/comments"))

        discussions: list[Discussion] = []
        threads: dict[int | str, list[Note]] = {}

        for c in review_comments:
            thread_id = c.get("in_reply_to_id") or c["id"]
            note = _parse_note(c, "DiffNote")
            threads.setdefault(thread_id, []).append(note)

        for thread_id, notes in threads.items():
            discussions.append(Discussion(id=str(thread_id), notes=notes))

        for c in issue_comments:
            note = _parse_note(c, "issue_comment")
            discussions.append(Discussion(
                id=str(c["id"]), notes=[note]))

        return discussions

    def mr_pipelines(self, project_id: ProjectID,
                     mr_number: int) -> list[Pipeline]:
        owner_repo = _owner_repo(project_id)
        r = self._req("GET",
                       f"/repos/{owner_repo}/pulls/{mr_number}")
        r.raise_for_status()
        pr_data = r.json()
        head_sha = (pr_data.get("head") or {}).get("sha", "")
        if not head_sha:
            return []

        check_runs = list(self._paginate(
            f"/repos/{owner_repo}/commits/{head_sha}/check-runs"))
        return [_parse_pipeline(cr) for cr in check_runs]

    def pipeline_jobs(self, project_id: ProjectID,
                      pipeline_id: int | str) -> list[Job]:
        owner_repo = _owner_repo(project_id)
        r = self._req(
            "GET",
            f"/repos/{owner_repo}/actions/runs/{pipeline_id}/jobs")
        r.raise_for_status()
        return [_parse_job(j) for j in r.json().get("jobs", [])]

    def job_log(self, project_id: ProjectID, job_id: int | str) -> str:
        owner_repo = _owner_repo(project_id)
        r = self._req(
            "GET",
            f"/repos/{owner_repo}/actions/jobs/{job_id}/logs",
            allow_redirects=True)
        r.raise_for_status()
        return r.text

    def mr_changes(self, project_id: ProjectID,
                   mr_number: int) -> MRDetail:
        owner_repo = _owner_repo(project_id)
        r = self._req("GET", f"/repos/{owner_repo}/pulls/{mr_number}")
        r.raise_for_status()
        pr_data = r.json()

        files = list(self._paginate(
            f"/repos/{owner_repo}/pulls/{mr_number}/files"))

        head = pr_data.get("head") or {}
        base = pr_data.get("base") or {}
        diff_refs = None
        if head.get("sha") and base.get("sha"):
            diff_refs = DiffRefs(
                base_sha=base["sha"],
                head_sha=head["sha"],
            )

        changes = []
        for f in files:
            changes.append(DiffChange(
                diff=f.get("patch", ""),
                old_path=f.get("previous_filename", f.get("filename", "")),
                new_path=f.get("filename", ""),
                renamed_file=f.get("status") == "renamed",
                deleted_file=f.get("status") == "removed",
                new_file=f.get("status") == "added",
            ))

        return MRDetail(
            number=pr_data.get("number", mr_number),
            diff_refs=diff_refs,
            changes=changes,
        )

    def find_mr_for_branch(self, project_id: ProjectID,
                           branch: str) -> MergeRequest | None:
        owner_repo = _owner_repo(project_id)
        r = self._req(
            "GET",
            f"/repos/{owner_repo}/pulls",
            params={"head": f"{owner_repo.split('/')[0]}:{branch}",
                    "state": "all"})
        r.raise_for_status()
        data = r.json()
        return _parse_pr(data[0]) if data else None

    def create_mr(self, project_id: ProjectID, *,
                  source_branch: str, target_branch: str,
                  title: str, description: str,
                  draft: bool = True,
                  labels: list[str] | None = None) -> MergeRequest:
        owner_repo = _owner_repo(project_id)
        if draft and not title.lower().startswith("draft:"):
            title = f"Draft: {title}"
        payload: dict[str, Any] = {
            "head": source_branch,
            "base": target_branch,
            "title": title,
            "body": description,
            "draft": draft,
        }
        r = self._req("POST", f"/repos/{owner_repo}/pulls", json=payload)
        r.raise_for_status()
        pr_data = r.json()

        if labels:
            self._req(
                "POST",
                f"/repos/{owner_repo}/issues/{pr_data['number']}/labels",
                json={"labels": labels}).raise_for_status()

        return _parse_pr(pr_data)

    def update_mr(self, project_id: ProjectID, mr_number: int,
                  **fields) -> None:
        owner_repo = _owner_repo(project_id)
        payload: dict[str, Any] = {}
        for key, val in fields.items():
            if key == "description":
                payload["body"] = val
            elif key == "labels":
                self._req(
                    "PUT",
                    f"/repos/{owner_repo}/issues/{mr_number}/labels",
                    json={"labels": val if isinstance(val, list) else [val]}).raise_for_status()
            else:
                payload[key] = val
        if payload:
            self._req("PATCH", f"/repos/{owner_repo}/pulls/{mr_number}",
                      json=payload).raise_for_status()

    def comment_issue(self, project_id: ProjectID, issue_number: int,
                      body: str) -> None:
        owner_repo = _owner_repo(project_id)
        self._req(
            "POST",
            f"/repos/{owner_repo}/issues/{issue_number}/comments",
            json={"body": body}).raise_for_status()

    def comment_mr(self, project_id: ProjectID, mr_number: int,
                   body: str) -> None:
        owner_repo = _owner_repo(project_id)
        self._req(
            "POST",
            f"/repos/{owner_repo}/issues/{mr_number}/comments",
            json={"body": body}).raise_for_status()

    def reply_to_discussion(self, project_id: ProjectID, mr_number: int,
                            discussion_id: str, body: str) -> None:
        owner_repo = _owner_repo(project_id)
        try:
            comment_id = int(discussion_id)
        except ValueError:
            self.comment_mr(project_id, mr_number, body)
            return
        self._req(
            "POST",
            f"/repos/{owner_repo}/pulls/{mr_number}/comments",
            json={"body": body, "in_reply_to": comment_id}).raise_for_status()

    def create_mr_discussion(self, project_id: ProjectID, mr_number: int,
                             body: str,
                             position: dict | None = None) -> None:
        owner_repo = _owner_repo(project_id)
        if position:
            payload: dict[str, Any] = {
                "body": body,
                "commit_id": position["head_sha"],
                "path": position["new_path"],
                "side": "RIGHT",
                "line": position["new_line"],
            }
            self._req(
                "POST",
                f"/repos/{owner_repo}/pulls/{mr_number}/comments",
                json=payload).raise_for_status()
        else:
            self.comment_mr(project_id, mr_number, body)

    # -- Git operations --------------------------------------------------------

    def clone_url(self, project: Project) -> str:
        return project.http_clone_url.replace("://", "://x-access-token@", 1)

    @property
    def git_token(self) -> str:
        return self._token

    # -- URL construction ------------------------------------------------------

    def issue_url(self, project: Project, issue_number: int) -> str:
        return f"{project.web_url}/issues/{issue_number}"

    # -- Inline review positions -----------------------------------------------

    def build_inline_comment_position(
        self,
        diff_refs: DiffRefs,
        file_path: str,
        old_path: str,
        line: int,
    ) -> dict | None:
        if not (diff_refs.base_sha and diff_refs.head_sha):
            return None
        return {
            "base_sha": diff_refs.base_sha,
            "head_sha": diff_refs.head_sha,
            "new_path": file_path,
            "old_path": old_path,
            "new_line": line,
        }

    # -- Webhook handling ------------------------------------------------------

    def validate_webhook(self, headers: dict, body: bytes,
                         secret: str) -> bool:
        if not secret:
            return True
        signature = headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[7:], expected)

    def parse_webhook_event(
        self, headers: dict, payload: dict,
    ) -> tuple[str, ProjectID | None, bool]:
        event_type = headers.get("X-GitHub-Event", "")

        is_relevant = event_type in RELEVANT_EVENTS

        repo_data = payload.get("repository") or {}
        project_id = repo_data.get("full_name")

        return event_type, project_id, is_relevant
