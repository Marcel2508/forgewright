"""Tests for forgewright.platform — factory and GitLabPlatform."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from forgewright.platform import create_platform
from forgewright.platform.github import GitHubPlatform
from forgewright.platform.gitlab import GitLabPlatform
from forgewright.types import DiffRefs, Project


class TestCreatePlatform:
    def test_creates_gitlab(self, tmp_config):
        tmp_config.platform_type = "gitlab"
        platform = create_platform(tmp_config)
        assert isinstance(platform, GitLabPlatform)

    def test_creates_github(self, tmp_config):
        tmp_config.platform_type = "github"
        tmp_config.platform_url = "https://api.github.com"
        from forgewright.platform.github import GitHubPlatform
        platform = create_platform(tmp_config)
        assert isinstance(platform, GitHubPlatform)

    def test_unknown_type_raises(self, tmp_config):
        tmp_config.platform_type = "forgejo"
        with pytest.raises(ValueError, match="unknown platform_type"):
            create_platform(tmp_config)

    def test_passes_config(self, tmp_config):
        tmp_config.platform_type = "gitlab"
        tmp_config.platform_url = "https://git.example.com"
        tmp_config.platform_token = "test-token"
        tmp_config.request_timeout_sec = 15
        tmp_config.http_retries = 5
        platform = create_platform(tmp_config)
        assert platform._base == "https://git.example.com/api/v4"
        assert platform._timeout == 15
        assert platform._retries == 5


class TestGitLabPlatformReq:
    def test_successful_request(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(plat._session, "request", return_value=mock_resp):
            resp = plat._req("GET", "/user")
            assert resp == mock_resp

    def test_retries_on_connection_error(self):
        plat = GitLabPlatform("https://git.example.com", "token",
                              http_retries=3)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(plat._session, "request",
                          side_effect=[requests.ConnectionError("fail"),
                                       mock_resp]) as mock_req, \
             patch("forgewright.platform.gitlab.time.sleep"):
            resp = plat._req("GET", "/test")
            assert resp == mock_resp
            assert mock_req.call_count == 2

    def test_retries_on_timeout(self):
        plat = GitLabPlatform("https://git.example.com", "token",
                              http_retries=3)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(plat._session, "request",
                          side_effect=[requests.Timeout("slow"),
                                       mock_resp]) as mock_req, \
             patch("forgewright.platform.gitlab.time.sleep"):
            resp = plat._req("GET", "/test")
            assert resp == mock_resp

    def test_retries_on_500(self):
        plat = GitLabPlatform("https://git.example.com", "token",
                              http_retries=3)
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_500.text = "Internal Server Error"
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        with patch.object(plat._session, "request",
                          side_effect=[mock_resp_500, mock_resp_200]), \
             patch("forgewright.platform.gitlab.time.sleep"):
            resp = plat._req("GET", "/test")
            assert resp == mock_resp_200

    def test_raises_after_all_retries_exhausted(self):
        plat = GitLabPlatform("https://git.example.com", "token",
                              http_retries=2)
        with patch.object(plat._session, "request",
                          side_effect=requests.ConnectionError("fail")), \
             patch("forgewright.platform.gitlab.time.sleep"):
            with pytest.raises(RuntimeError, match="request failed after retries"):
                plat._req("GET", "/test")

    def test_uses_full_url_when_starts_with_http(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(plat._session, "request",
                          return_value=mock_resp) as mock_req:
            plat._req("GET", "https://other.example.com/foo")
            mock_req.assert_called_once()
            assert mock_req.call_args[0][1] == "https://other.example.com/foo"

    def test_4xx_not_retried(self):
        plat = GitLabPlatform("https://git.example.com", "token",
                              http_retries=3)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(plat._session, "request",
                          return_value=mock_resp) as mock_req:
            resp = plat._req("GET", "/test")
            assert resp.status_code == 404
            assert mock_req.call_count == 1


class TestGitLabPlatformPaginate:
    def test_single_page(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": 1}, {"id": 2}]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            items = list(plat._paginate("/projects"))
            assert len(items) == 2

    def test_multi_page(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        resp1 = MagicMock()
        resp1.json.return_value = [{"id": 1}]
        resp1.links = {"next": {"url": "https://git.example.com/api/v4/projects?page=2"}}
        resp1.raise_for_status = MagicMock()
        resp2 = MagicMock()
        resp2.json.return_value = [{"id": 2}]
        resp2.links = {}
        resp2.raise_for_status = MagicMock()
        with patch.object(plat, "_req", side_effect=[resp1, resp2]):
            items = list(plat._paginate("/projects"))
            assert len(items) == 2
            assert items[0]["id"] == 1
            assert items[1]["id"] == 2


class TestGitLabPlatformMethods:
    def _make_platform(self):
        return GitLabPlatform("https://git.example.com", "token")

    def test_current_user(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"username": "bot", "id": 1}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            user = plat.current_user()
            assert user.username == "bot"

    def test_create_mr_adds_draft_prefix(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"iid": 1, "title": "Draft: Add stuff",
                                        "source_branch": "feat",
                                        "target_branch": "main"}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr(1, source_branch="feat", target_branch="main",
                           title="Add stuff", description="desc", draft=True)
            payload = mock_req.call_args[1]["json"]
            assert payload["title"].startswith("Draft:")

    def test_create_mr_no_duplicate_draft_prefix(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"iid": 1, "title": "Draft: My MR",
                                        "source_branch": "feat",
                                        "target_branch": "main"}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr(1, source_branch="feat", target_branch="main",
                           title="Draft: My MR", description="desc", draft=True)
            payload = mock_req.call_args[1]["json"]
            assert payload["title"] == "Draft: My MR"
            assert not payload["title"].startswith("Draft: Draft:")

    def test_create_mr_no_draft(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"iid": 1, "title": "My MR",
                                        "source_branch": "feat",
                                        "target_branch": "main"}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr(1, source_branch="feat", target_branch="main",
                           title="My MR", description="desc", draft=False)
            payload = mock_req.call_args[1]["json"]
            assert payload["title"] == "My MR"

    def test_create_mr_with_labels(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"iid": 1, "title": "MR",
                                        "source_branch": "feat",
                                        "target_branch": "main"}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr(1, source_branch="feat", target_branch="main",
                           title="MR", description="d",
                           labels=["bug", "priority"])
            payload = mock_req.call_args[1]["json"]
            assert payload["labels"] == "bug,priority"

    def test_find_mr_for_branch_found(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"iid": 5, "source_branch": "feat",
                                         "target_branch": "main",
                                         "title": "MR"}]
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            mr = plat.find_mr_for_branch(1, "feat")
            assert mr.number == 5
            # Must filter to opened — otherwise the bot would update closed
            # or merged MRs that share the branch name instead of opening a
            # new PR.
            assert mock_req.call_args[1]["params"]["state"] == "opened"

    def test_find_mr_for_branch_not_found(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            mr = plat.find_mr_for_branch(1, "no-such-branch")
            assert mr is None

    def test_comment_issue_calls_api(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.comment_issue(1, 5, "hello")
            mock_req.assert_called_once()
            assert mock_req.call_args[0][0] == "POST"
            assert "issues/5/notes" in mock_req.call_args[0][1]
            assert mock_req.call_args[1]["json"]["body"] == "hello"

    def test_reply_to_discussion(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.reply_to_discussion(1, 5, "disc-123", "reply text")
            assert "discussions/disc-123/notes" in mock_req.call_args[0][1]

    def test_create_mr_discussion_without_position(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "new-disc"}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr_discussion(1, 5, "comment")
            payload = mock_req.call_args[1]["json"]
            assert payload == {"body": "comment"}

    def test_create_mr_discussion_with_position(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "new-disc"}
        mock_resp.raise_for_status = MagicMock()
        pos = {"position_type": "text", "new_line": 10, "new_path": "foo.py"}
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr_discussion(1, 5, "inline", position=pos)
            payload = mock_req.call_args[1]["json"]
            assert payload["position"] == pos

    def test_session_has_auth_header(self):
        plat = GitLabPlatform("https://git.example.com", "my-token")
        assert plat._session.headers["PRIVATE-TOKEN"] == "my-token"

    def test_job_log(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.text = "build output here"
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            log = plat.job_log(1, 42)
            assert log == "build output here"

    def test_mr_changes(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "iid": 5,
            "changes": [{"diff": "...", "old_path": "f.py",
                          "new_path": "f.py"}],
            "diff_refs": {"base_sha": "a", "head_sha": "abc",
                          "start_sha": "b"},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            result = plat.mr_changes(1, 5)
            assert result.changes[0].diff == "..."
            assert result.diff_refs.head_sha == "abc"
            assert "access_raw_diffs" in str(mock_req.call_args)


class TestGitLabCloneUrl:
    def test_inserts_oauth2(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        proj = Project(id=1, path="group/repo",
                       web_url="https://git.example.com/group/repo",
                       default_branch="main",
                       http_clone_url="https://git.example.com/group/repo.git")
        url = plat.clone_url(proj)
        assert url == "https://oauth2@git.example.com/group/repo.git"

    def test_http_url(self):
        plat = GitLabPlatform("http://localhost", "token")
        proj = Project(id=1, path="repo", web_url="http://localhost/repo",
                       default_branch="main",
                       http_clone_url="http://localhost/repo.git")
        url = plat.clone_url(proj)
        assert url == "http://oauth2@localhost/repo.git"


class TestGitLabGitToken:
    def test_returns_token(self):
        plat = GitLabPlatform("https://git.example.com", "my-secret-token")
        assert plat.git_token == "my-secret-token"


class TestGitLabBuildInlineCommentPosition:
    def test_builds_position(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        diff_refs = DiffRefs(base_sha="aaa", head_sha="bbb", start_sha="ccc")
        pos = plat.build_inline_comment_position(
            diff_refs, "foo.py", "foo.py", 10)
        assert pos == {
            "base_sha": "aaa",
            "head_sha": "bbb",
            "start_sha": "ccc",
            "position_type": "text",
            "new_path": "foo.py",
            "old_path": "foo.py",
            "new_line": 10,
        }

    def test_returns_none_without_refs(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        assert plat.build_inline_comment_position(
            DiffRefs(base_sha="", head_sha="", start_sha=""),
            "f.py", "f.py", 1) is None

    def test_returns_none_with_partial_refs(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        assert plat.build_inline_comment_position(
            DiffRefs(base_sha="a", head_sha="b", start_sha=None),
            "f.py", "f.py", 1) is None

    def test_renamed_file(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        diff_refs = DiffRefs(base_sha="aaa", head_sha="bbb", start_sha="ccc")
        pos = plat.build_inline_comment_position(
            diff_refs, "new.py", "old.py", 5)
        assert pos["new_path"] == "new.py"
        assert pos["old_path"] == "old.py"


class TestGitLabWebhookValidation:
    def test_valid_secret(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        assert plat.validate_webhook(
            {"X-Gitlab-Token": "my-secret"}, b"", "my-secret") is True

    def test_invalid_secret(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        assert plat.validate_webhook(
            {"X-Gitlab-Token": "wrong"}, b"", "my-secret") is False

    def test_missing_header(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        assert plat.validate_webhook({}, b"", "my-secret") is False

    def test_empty_secret_allows_all(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        assert plat.validate_webhook({}, b"", "") is True


class TestGitLabParseWebhookEvent:
    def test_issue_hook(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-Gitlab-Event": "Issue Hook"},
            {"object_kind": "issue", "project": {"id": 42}},
        )
        assert event == "Issue Hook"
        assert pid == 42
        assert relevant is True

    def test_irrelevant_event(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-Gitlab-Event": "Push Hook"},
            {"object_kind": "push", "project": {"id": 42}},
        )
        assert event == "Push Hook"
        assert relevant is False

    def test_fallback_to_object_kind(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        event, pid, relevant = plat.parse_webhook_event(
            {},
            {"object_kind": "note", "project": {"id": 99}},
        )
        assert event == "note"
        assert pid == 99
        assert relevant is True

    def test_missing_project(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-Gitlab-Event": "Issue Hook"},
            {"object_kind": "issue"},
        )
        assert pid is None
        assert relevant is True


class TestProjectIdWithStringId:
    def test_string_project_id_in_api_path(self):
        plat = GitLabPlatform("https://git.example.com", "token")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "owner/repo",
                                        "path_with_namespace": "owner/repo",
                                        "default_branch": "main"}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.project("owner%2Frepo")
            assert "/projects/owner%2Frepo" in mock_req.call_args[0][1]


# ---- GitHub Platform Tests ----


class TestGitHubPlatformReq:
    def test_successful_request(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(plat._session, "request", return_value=mock_resp):
            resp = plat._req("GET", "/user")
            assert resp == mock_resp

    def test_retries_on_connection_error(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test",
                              http_retries=3)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(plat._session, "request",
                          side_effect=[requests.ConnectionError("fail"),
                                       mock_resp]) as mock_req, \
             patch("forgewright.platform.github.time.sleep"):
            resp = plat._req("GET", "/test")
            assert resp == mock_resp
            assert mock_req.call_count == 2

    def test_retries_on_500(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test",
                              http_retries=3)
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_500.text = "Internal Server Error"
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        with patch.object(plat._session, "request",
                          side_effect=[mock_resp_500, mock_resp_200]), \
             patch("forgewright.platform.github.time.sleep"):
            resp = plat._req("GET", "/test")
            assert resp == mock_resp_200

    def test_raises_after_all_retries_exhausted(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test",
                              http_retries=2)
        with patch.object(plat._session, "request",
                          side_effect=requests.ConnectionError("fail")), \
             patch("forgewright.platform.github.time.sleep"):
            with pytest.raises(RuntimeError, match="request failed after retries"):
                plat._req("GET", "/test")

    def test_session_has_auth_header(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test123")
        assert plat._session.headers["Authorization"] == "Bearer ghp_test123"
        assert "github" in plat._session.headers["Accept"]


class TestGitHubPlatformPaginate:
    def test_single_page(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": 1}, {"id": 2}]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            items = list(plat._paginate("/user/repos"))
            assert len(items) == 2

    def test_multi_page(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        resp1 = MagicMock()
        resp1.json.return_value = [{"id": 1}]
        resp1.links = {"next": {"url": "https://api.github.com/user/repos?page=2"}}
        resp1.raise_for_status = MagicMock()
        resp2 = MagicMock()
        resp2.json.return_value = [{"id": 2}]
        resp2.links = {}
        resp2.raise_for_status = MagicMock()
        with patch.object(plat, "_req", side_effect=[resp1, resp2]):
            items = list(plat._paginate("/user/repos"))
            assert len(items) == 2


class TestGitHubPlatformMethods:
    def _make_platform(self):
        return GitHubPlatform("https://api.github.com", "ghp_test")

    def test_current_user(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"login": "bot-user", "id": 12345}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            user = plat.current_user()
            assert user.username == "bot-user"
            assert user.id == 12345

    def test_project(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "default_branch": "main",
            "clone_url": "https://github.com/owner/repo.git",
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            proj = plat.project("owner/repo")
            assert proj.id == "owner/repo"
            assert proj.path == "owner/repo"
            assert "/repos/owner/repo" in mock_req.call_args[0][1]

    def test_list_issues_filters_pull_requests(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"number": 1, "title": "Bug", "html_url": "...", "updated_at": ""},
            {"number": 2, "title": "PR", "html_url": "...", "updated_at": "",
             "pull_request": {"url": "..."}},
        ]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            issues = plat.list_issues("owner/repo", None)
            assert len(issues) == 1
            assert issues[0].number == 1

    def test_list_mrs(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"number": 5, "title": "My PR", "html_url": "...",
             "updated_at": "", "head": {"ref": "feat", "sha": "abc"},
             "base": {"ref": "main", "sha": "def"},
             "user": {"login": "dev"}},
        ]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            mrs = plat.list_mrs("owner/repo", None)
            assert len(mrs) == 1
            assert mrs[0].number == 5
            assert mrs[0].source_branch == "feat"
            assert mrs[0].sha == "abc"

    def test_create_mr_adds_draft_prefix(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "number": 10, "title": "Draft: Add stuff",
            "html_url": "...", "updated_at": "",
            "head": {"ref": "feat", "sha": "a"},
            "base": {"ref": "main", "sha": "b"},
            "user": {"login": "bot"},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr("owner/repo", source_branch="feat",
                           target_branch="main", title="Add stuff",
                           description="desc", draft=True)
            payload = mock_req.call_args_list[0][1]["json"]
            assert payload["title"].startswith("Draft:")
            assert payload["draft"] is True

    def test_create_mr_no_duplicate_draft_prefix(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "number": 10, "title": "Draft: My PR",
            "html_url": "...", "updated_at": "",
            "head": {"ref": "feat", "sha": "a"},
            "base": {"ref": "main", "sha": "b"},
            "user": {"login": "bot"},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr("owner/repo", source_branch="feat",
                           target_branch="main", title="Draft: My PR",
                           description="desc", draft=True)
            payload = mock_req.call_args_list[0][1]["json"]
            assert payload["title"] == "Draft: My PR"

    def test_create_mr_with_labels(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "number": 10, "title": "MR",
            "html_url": "...", "updated_at": "",
            "head": {"ref": "feat", "sha": "a"},
            "base": {"ref": "main", "sha": "b"},
            "user": {"login": "bot"},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr("owner/repo", source_branch="feat",
                           target_branch="main", title="MR",
                           description="d", labels=["bug", "priority"])
            assert mock_req.call_count == 2
            labels_call = mock_req.call_args_list[1]
            assert labels_call[1]["json"]["labels"] == ["bug", "priority"]

    def test_find_mr_for_branch_found(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"number": 5, "title": "MR", "html_url": "...", "updated_at": "",
             "head": {"ref": "feat", "sha": "a"},
             "base": {"ref": "main", "sha": "b"},
             "user": {"login": "dev"}},
        ]
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            mr = plat.find_mr_for_branch("owner/repo", "feat")
            assert mr.number == 5
            # Must filter to open — see GitLab equivalent for rationale.
            assert mock_req.call_args[1]["params"]["state"] == "open"

    def test_find_mr_for_branch_not_found(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            mr = plat.find_mr_for_branch("owner/repo", "no-such")
            assert mr is None

    def test_comment_issue(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.comment_issue("owner/repo", 5, "hello")
            assert mock_req.call_args[0][0] == "POST"
            assert "issues/5/comments" in mock_req.call_args[0][1]
            assert mock_req.call_args[1]["json"]["body"] == "hello"

    def test_comment_mr(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.comment_mr("owner/repo", 5, "comment text")
            assert "issues/5/comments" in mock_req.call_args[0][1]

    def test_reply_to_review_discussion(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.reply_to_discussion("owner/repo", 5, "review:123456", "reply")
            assert "/pulls/5/comments" in mock_req.call_args[0][1]
            payload = mock_req.call_args[1]["json"]
            assert payload["in_reply_to"] == 123456
            assert payload["body"] == "reply"

    def test_reply_to_issue_discussion_falls_back_to_top_level(self):
        """GitHub issue comments don't support threading, so we post a new
        top-level comment instead of trying (and failing with 422) to use
        the pull request review-comment endpoint."""
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.reply_to_discussion("owner/repo", 5, "issue:100", "reply")
            assert "/issues/5/comments" in mock_req.call_args[0][1]
            assert mock_req.call_args[1]["json"]["body"] == "reply"
            assert "in_reply_to" not in mock_req.call_args[1]["json"]

    def test_reply_to_discussion_unprefixed_numeric_id(self):
        """Backwards compat: unprefixed numeric ids are treated as review
        comment ids."""
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.reply_to_discussion("owner/repo", 5, "123456", "reply")
            payload = mock_req.call_args[1]["json"]
            assert payload["in_reply_to"] == 123456
            assert payload["body"] == "reply"

    def test_reply_to_discussion_non_numeric_id(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.reply_to_discussion("owner/repo", 5, "not-a-number", "reply")
            assert "issues/5/comments" in mock_req.call_args[0][1]

    def test_create_mr_discussion_without_position(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr_discussion("owner/repo", 5, "comment")
            assert "issues/5/comments" in mock_req.call_args[0][1]

    def test_create_mr_discussion_with_position(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        pos = {"head_sha": "abc", "new_path": "foo.py", "new_line": 10}
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.create_mr_discussion("owner/repo", 5, "inline", position=pos)
            payload = mock_req.call_args[1]["json"]
            assert payload["commit_id"] == "abc"
            assert payload["path"] == "foo.py"
            assert payload["line"] == 10

    def test_job_log(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.text = "build output"
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            log = plat.job_log("owner/repo", 42)
            assert log == "build output"

    def test_mr_changes(self):
        plat = self._make_platform()
        pr_resp = MagicMock()
        pr_resp.json.return_value = {
            "number": 5,
            "head": {"sha": "abc", "ref": "feat"},
            "base": {"sha": "def", "ref": "main"},
        }
        pr_resp.raise_for_status = MagicMock()
        files_resp = MagicMock()
        files_resp.json.return_value = [
            {"filename": "foo.py", "patch": "diff content",
             "status": "modified"},
        ]
        files_resp.raise_for_status = MagicMock()
        files_resp.links = {}
        with patch.object(plat, "_req",
                          side_effect=[pr_resp, files_resp]):
            result = plat.mr_changes("owner/repo", 5)
            assert result.number == 5
            assert result.diff_refs.head_sha == "abc"
            assert result.changes[0].diff == "diff content"

    def test_mr_pipelines(self):
        plat = self._make_platform()
        pr_resp = MagicMock()
        pr_resp.json.return_value = {
            "head": {"sha": "abc123"},
        }
        pr_resp.raise_for_status = MagicMock()
        runs_resp = MagicMock()
        runs_resp.json.return_value = {
            "total_count": 1,
            "workflow_runs": [
                {"id": 9876, "head_sha": "abc123", "status": "completed",
                 "conclusion": "success", "html_url": "..."},
            ],
        }
        runs_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req",
                          side_effect=[pr_resp, runs_resp]) as mock_req:
            pipelines = plat.mr_pipelines("owner/repo", 5)
            assert len(pipelines) == 1
            assert pipelines[0].id == 9876
            assert pipelines[0].status == "success"
            # Second call must hit /actions/runs with head_sha filter so the
            # returned id is a workflow_run id (compatible with pipeline_jobs).
            runs_call = mock_req.call_args_list[1]
            assert "/actions/runs" in runs_call[0][1]
            assert runs_call[1]["params"]["head_sha"] == "abc123"

    def test_mr_pipelines_no_head_sha(self):
        plat = self._make_platform()
        pr_resp = MagicMock()
        pr_resp.json.return_value = {"head": {}}
        pr_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=pr_resp):
            assert plat.mr_pipelines("owner/repo", 5) == []

    def test_mr_pipelines_empty_runs(self):
        plat = self._make_platform()
        pr_resp = MagicMock()
        pr_resp.json.return_value = {"head": {"sha": "abc"}}
        pr_resp.raise_for_status = MagicMock()
        runs_resp = MagicMock()
        runs_resp.json.return_value = {"total_count": 0, "workflow_runs": []}
        runs_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", side_effect=[pr_resp, runs_resp]):
            assert plat.mr_pipelines("owner/repo", 5) == []

    def test_pipeline_id_chain_uses_workflow_run_id(self):
        """End-to-end check: id from mr_pipelines must work in pipeline_jobs.

        Regression for the bug where mr_pipelines returned check-run ids while
        pipeline_jobs called /actions/runs/{id}/jobs (which expects
        workflow_run ids), causing 404s on every failing-pipeline trigger.
        """
        plat = self._make_platform()
        pr_resp = MagicMock()
        pr_resp.json.return_value = {"head": {"sha": "abc"}}
        pr_resp.raise_for_status = MagicMock()
        runs_resp = MagicMock()
        runs_resp.json.return_value = {
            "workflow_runs": [
                {"id": 4242, "head_sha": "abc", "status": "completed",
                 "conclusion": "failure", "html_url": "..."},
            ],
        }
        runs_resp.raise_for_status = MagicMock()
        jobs_resp = MagicMock()
        jobs_resp.json.return_value = {
            "jobs": [{"id": 9, "name": "build", "status": "completed",
                      "conclusion": "failure"}],
        }
        jobs_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req",
                          side_effect=[pr_resp, runs_resp, jobs_resp]) as mock_req:
            pipelines = plat.mr_pipelines("owner/repo", 5)
            jobs = plat.pipeline_jobs("owner/repo", pipelines[0].id)
            assert jobs[0].name == "build"
            jobs_call = mock_req.call_args_list[2]
            assert "/actions/runs/4242/jobs" in jobs_call[0][1]


class TestGitHubCloneUrl:
    def test_inserts_x_access_token(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        proj = Project(id="owner/repo", path="owner/repo",
                       web_url="https://github.com/owner/repo",
                       default_branch="main",
                       http_clone_url="https://github.com/owner/repo.git")
        url = plat.clone_url(proj)
        assert url == "https://x-access-token@github.com/owner/repo.git"


class TestGitHubGitToken:
    def test_returns_token(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_secret")
        assert plat.git_token == "ghp_secret"


class TestGitHubIssueUrl:
    def test_builds_url(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        proj = Project(id="owner/repo", path="owner/repo",
                       web_url="https://github.com/owner/repo",
                       default_branch="main",
                       http_clone_url="https://github.com/owner/repo.git")
        assert plat.issue_url(proj, 42) == \
            "https://github.com/owner/repo/issues/42"


class TestGitHubBuildInlineCommentPosition:
    def test_builds_position(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        diff_refs = DiffRefs(base_sha="aaa", head_sha="bbb")
        pos = plat.build_inline_comment_position(
            diff_refs, "foo.py", "foo.py", 10)
        assert pos == {
            "base_sha": "aaa",
            "head_sha": "bbb",
            "new_path": "foo.py",
            "old_path": "foo.py",
            "new_line": 10,
        }

    def test_returns_none_without_refs(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        assert plat.build_inline_comment_position(
            DiffRefs(base_sha="", head_sha=""), "f.py", "f.py", 1) is None


class TestGitHubWebhookValidation:
    def test_valid_signature(self):
        import hashlib
        import hmac as hmac_mod
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        body = b'{"action":"opened"}'
        secret = "webhook-secret"
        sig = "sha256=" + hmac_mod.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
        assert plat.validate_webhook(
            {"X-Hub-Signature-256": sig}, body, secret) is True

    def test_invalid_signature(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        assert plat.validate_webhook(
            {"X-Hub-Signature-256": "sha256=wrong"}, b"body", "secret") is False

    def test_missing_header(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        assert plat.validate_webhook({}, b"body", "secret") is False

    def test_empty_secret_allows_all(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        assert plat.validate_webhook({}, b"", "") is True


class TestGitHubParseWebhookEvent:
    def test_issue_event(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-GitHub-Event": "issues"},
            {"action": "opened",
             "repository": {"full_name": "owner/repo"}},
        )
        assert event == "issues"
        assert pid == "owner/repo"
        assert relevant is True

    def test_irrelevant_event(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-GitHub-Event": "push"},
            {"repository": {"full_name": "owner/repo"}},
        )
        assert event == "push"
        assert relevant is False

    def test_pull_request_event(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-GitHub-Event": "pull_request"},
            {"action": "opened",
             "repository": {"full_name": "owner/repo"}},
        )
        assert event == "pull_request"
        assert relevant is True

    def test_missing_repository(self):
        plat = GitHubPlatform("https://api.github.com", "ghp_test")
        event, pid, relevant = plat.parse_webhook_event(
            {"X-GitHub-Event": "issues"}, {"action": "opened"})
        assert pid is None
        assert relevant is True


class TestGitHubIssueNotes:
    def _make_platform(self):
        return GitHubPlatform("https://api.github.com", "ghp_test")

    def test_returns_notes(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"id": 101, "body": "first comment", "created_at": "2024-01-01",
             "user": {"login": "alice", "id": 1}},
            {"id": 102, "body": "second comment", "created_at": "2024-01-02",
             "user": {"login": "bob", "id": 2}},
        ]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            notes = plat.issue_notes("owner/repo", 5)
            assert len(notes) == 2
            assert notes[0].id == 101
            assert notes[0].body == "first comment"
            assert notes[0].author.username == "alice"
            assert notes[1].id == 102

    def test_empty_notes(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            notes = plat.issue_notes("owner/repo", 5)
            assert notes == []

    def test_note_type_is_issue_comment(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"id": 1, "body": "hi", "created_at": "", "user": {"login": "a"}},
        ]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            notes = plat.issue_notes("owner/repo", 1)
            assert notes[0].type == "issue_comment"


class TestGitHubMrDiscussions:
    def _make_platform(self):
        return GitHubPlatform("https://api.github.com", "ghp_test")

    def _mock_paginate(self, plat, issue_comments, review_comments):
        """Helper to mock _paginate for mr_discussions which calls it twice."""
        def side_effect(path, **params):
            if "/issues/" in path:
                return iter(issue_comments)
            if "/pulls/" in path:
                return iter(review_comments)
            return iter([])
        return patch.object(plat, "_paginate", side_effect=side_effect)

    def test_issue_comments_become_discussions(self):
        plat = self._make_platform()
        issue_comments = [
            {"id": 100, "body": "general comment", "created_at": "2024-01-01",
             "user": {"login": "alice"}},
        ]
        with self._mock_paginate(plat, issue_comments, []):
            discussions = plat.mr_discussions("owner/repo", 5)
            assert len(discussions) == 1
            # IDs are prefixed so reply_to_discussion can route issue
            # comments (which can't be threaded on GitHub) differently from
            # review comments.
            assert discussions[0].id == "issue:100"
            assert discussions[0].notes[0].body == "general comment"
            assert discussions[0].notes[0].type == "issue_comment"

    def test_review_comments_grouped_into_threads(self):
        plat = self._make_platform()
        review_comments = [
            {"id": 200, "body": "review root", "created_at": "2024-01-01",
             "user": {"login": "alice"}, "path": "foo.py", "line": 10},
            {"id": 201, "body": "reply to review", "created_at": "2024-01-02",
             "user": {"login": "bob"}, "in_reply_to_id": 200},
        ]
        with self._mock_paginate(plat, [], review_comments):
            discussions = plat.mr_discussions("owner/repo", 5)
            assert len(discussions) == 1
            assert discussions[0].id == "review:200"
            assert len(discussions[0].notes) == 2
            assert discussions[0].notes[0].body == "review root"
            assert discussions[0].notes[1].body == "reply to review"

    def test_mixed_issue_and_review_comments(self):
        plat = self._make_platform()
        issue_comments = [
            {"id": 300, "body": "top-level", "created_at": "2024-01-01",
             "user": {"login": "alice"}},
        ]
        review_comments = [
            {"id": 400, "body": "inline", "created_at": "2024-01-01",
             "user": {"login": "bob"}, "path": "bar.py", "line": 5},
        ]
        with self._mock_paginate(plat, issue_comments, review_comments):
            discussions = plat.mr_discussions("owner/repo", 5)
            assert len(discussions) == 2
            review_disc = [d for d in discussions
                           if any(n.type == "DiffNote" for n in d.notes)]
            issue_disc = [d for d in discussions
                          if any(n.type == "issue_comment" for n in d.notes)]
            assert len(review_disc) == 1
            assert len(issue_disc) == 1

    def test_review_comment_position(self):
        plat = self._make_platform()
        review_comments = [
            {"id": 500, "body": "inline note", "created_at": "2024-01-01",
             "user": {"login": "alice"}, "path": "src/main.py",
             "line": 42, "original_line": 40},
        ]
        with self._mock_paginate(plat, [], review_comments):
            discussions = plat.mr_discussions("owner/repo", 5)
            note = discussions[0].notes[0]
            assert note.position is not None
            assert note.position.new_path == "src/main.py"
            assert note.position.new_line == 42
            assert note.position.old_line == 40

    def test_empty_discussions(self):
        plat = self._make_platform()
        with self._mock_paginate(plat, [], []):
            discussions = plat.mr_discussions("owner/repo", 5)
            assert discussions == []


class TestGitHubPipelineJobs:
    def _make_platform(self):
        return GitHubPlatform("https://api.github.com", "ghp_test")

    def test_returns_jobs(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jobs": [
                {"id": 1, "name": "build", "status": "completed",
                 "conclusion": "success"},
                {"id": 2, "name": "test", "status": "completed",
                 "conclusion": "failure"},
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            jobs = plat.pipeline_jobs("owner/repo", 9876)
            assert len(jobs) == 2
            assert jobs[0].name == "build"
            assert jobs[0].status == "success"
            assert jobs[1].name == "test"
            assert jobs[1].status == "failed"

    def test_empty_jobs(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"jobs": []}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            jobs = plat.pipeline_jobs("owner/repo", 9876)
            assert jobs == []

    def test_in_progress_job(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jobs": [
                {"id": 3, "name": "deploy", "status": "in_progress"},
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            jobs = plat.pipeline_jobs("owner/repo", 9876)
            assert jobs[0].status == "running"


class TestGitHubUpdateMr:
    def _make_platform(self):
        return GitHubPlatform("https://api.github.com", "ghp_test")

    def test_update_title(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.update_mr("owner/repo", 5, title="New Title")
            mock_req.assert_called_once()
            assert mock_req.call_args[0][0] == "PATCH"
            assert mock_req.call_args[1]["json"]["title"] == "New Title"

    def test_update_description(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.update_mr("owner/repo", 5, description="New desc")
            assert mock_req.call_args[1]["json"]["body"] == "New desc"

    def test_update_labels(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.update_mr("owner/repo", 5, labels=["bug", "urgent"])
            assert mock_req.call_args[0][0] == "PUT"
            assert "labels" in mock_req.call_args[0][1]
            assert mock_req.call_args[1]["json"]["labels"] == ["bug", "urgent"]

    def test_update_mixed_fields(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp) as mock_req:
            plat.update_mr("owner/repo", 5, title="T",
                           labels=["bug"], description="D")
            assert mock_req.call_count == 2
            patch_call = [c for c in mock_req.call_args_list
                          if c[0][0] == "PATCH"][0]
            assert patch_call[1]["json"]["title"] == "T"
            assert patch_call[1]["json"]["body"] == "D"


class TestGitHubListMemberProjects:
    def _make_platform(self):
        return GitHubPlatform("https://api.github.com", "ghp_test")

    def test_returns_projects(self):
        plat = self._make_platform()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"full_name": "owner/repo1", "html_url": "https://github.com/owner/repo1",
             "default_branch": "main", "clone_url": "https://github.com/owner/repo1.git"},
            {"full_name": "owner/repo2", "html_url": "https://github.com/owner/repo2",
             "default_branch": "develop", "clone_url": "https://github.com/owner/repo2.git"},
        ]
        mock_resp.links = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(plat, "_req", return_value=mock_resp):
            projects = plat.list_member_projects()
            assert len(projects) == 2
            assert projects[0].id == "owner/repo1"
            assert projects[0].path == "owner/repo1"
            assert projects[1].default_branch == "develop"
