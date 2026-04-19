"""Tests for forgewright.webhook."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forgewright.config import Config
from forgewright.types import Project
from forgewright.webhook import create_app


@pytest.fixture
def webhook_config(tmp_path: Path) -> Config:
    return Config(
        platform_url="https://git.example.com",
        platform_token="glpat-test-token",
        bot_username="forgewright",
        workdir=tmp_path / "work",
        state_file=tmp_path / "state.json",
        lock_dir=tmp_path / "locks",
        log_file=tmp_path / "bot.log",
        webhook_enabled=True,
        webhook_host="127.0.0.1",
        webhook_port=5000,
        webhook_secret="test-secret",
        webhook_debounce_sec=0,
    )


@pytest.fixture
def webhook_config_no_secret(tmp_path: Path) -> Config:
    return Config(
        platform_url="https://git.example.com",
        platform_token="glpat-test-token",
        bot_username="forgewright",
        workdir=tmp_path / "work",
        state_file=tmp_path / "state.json",
        lock_dir=tmp_path / "locks",
        log_file=tmp_path / "bot.log",
        webhook_enabled=True,
        webhook_host="127.0.0.1",
        webhook_port=5000,
        webhook_secret="",
        webhook_debounce_sec=0,
    )


@pytest.fixture
def client(webhook_config):
    app = create_app(webhook_config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def client_no_secret(webhook_config_no_secret):
    app = create_app(webhook_config_no_secret)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _issue_payload(project_id: int = 42) -> dict:
    return {
        "object_kind": "issue",
        "project": {
            "id": project_id,
            "path_with_namespace": "test/repo",
        },
        "object_attributes": {
            "iid": 1,
            "title": "Test issue",
            "action": "open",
        },
    }


def _note_payload(project_id: int = 42, noteable_type: str = "Issue") -> dict:
    return {
        "object_kind": "note",
        "project": {
            "id": project_id,
            "path_with_namespace": "test/repo",
        },
        "object_attributes": {
            "noteable_type": noteable_type,
            "note": "@forgewright please help",
        },
    }


def _mr_payload(project_id: int = 42) -> dict:
    return {
        "object_kind": "merge_request",
        "project": {
            "id": project_id,
            "path_with_namespace": "test/repo",
        },
        "object_attributes": {
            "iid": 5,
            "title": "Test MR",
            "action": "open",
        },
    }


def _pipeline_payload(project_id: int = 42) -> dict:
    return {
        "object_kind": "pipeline",
        "project": {
            "id": project_id,
            "path_with_namespace": "test/repo",
        },
        "object_attributes": {
            "id": 100,
            "status": "failed",
        },
    }


def _make_project(pid=42, path="test/repo"):
    return Project(id=pid, path=path, web_url="", default_branch="main",
                   http_clone_url="")


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


@patch("forgewright.webhook._process_event")
class TestWebhookAuth:
    def test_valid_secret(self, _mock_process, client):
        resp = client.post(
            "/webhook",
            data=json.dumps(_issue_payload()),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Issue Hook",
            },
        )
        assert resp.status_code == 202

    def test_invalid_secret(self, _mock_process, client):
        resp = client.post(
            "/webhook",
            data=json.dumps(_issue_payload()),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "wrong-secret",
                "X-Gitlab-Event": "Issue Hook",
            },
        )
        assert resp.status_code == 401

    def test_missing_secret_header(self, _mock_process, client):
        resp = client.post(
            "/webhook",
            data=json.dumps(_issue_payload()),
            content_type="application/json",
            headers={"X-Gitlab-Event": "Issue Hook"},
        )
        assert resp.status_code == 401

    def test_no_secret_configured_allows_all(self, _mock_process,
                                             client_no_secret):
        resp = client_no_secret.post(
            "/webhook",
            data=json.dumps(_issue_payload()),
            content_type="application/json",
            headers={"X-Gitlab-Event": "Issue Hook"},
        )
        assert resp.status_code == 202


@patch("forgewright.webhook._process_event")
class TestWebhookEventAcceptance:
    """All events with a valid project id are accepted — process_project
    handles the actual filtering via fingerprints."""

    def test_push_event_accepted(self, _mock_process, client):
        resp = client.post(
            "/webhook",
            data=json.dumps({"object_kind": "push", "project": {"id": 1, "path_with_namespace": "t/r"}}),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Push Hook",
            },
        )
        assert resp.status_code == 202

    def test_system_hook_accepted(self, _mock_process, client):
        resp = client.post(
            "/webhook",
            data=json.dumps({"object_kind": "issue", "project": {"id": 1, "path_with_namespace": "t/r"}}),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "System Hook",
            },
        )
        assert resp.status_code == 202

    def test_system_hook_with_push_accepted(self, _mock_process, client):
        """System Hooks for push events should also be accepted."""
        resp = client.post(
            "/webhook",
            data=json.dumps({"object_kind": "push", "project": {"id": 1, "path_with_namespace": "t/r"}}),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "System Hook",
            },
        )
        assert resp.status_code == 202

    def test_missing_event_header_accepted(self, _mock_process, client):
        payload = _issue_payload()
        resp = client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"X-Gitlab-Token": "test-secret"},
        )
        assert resp.status_code == 202

    @pytest.mark.parametrize("event", [
        "Issue Hook", "Confidential Issue Hook",
        "Note Hook", "Confidential Note Hook",
        "Merge Request Hook", "Pipeline Hook",
    ])
    def test_standard_events_accepted(self, _mock_process, client, event):
        payload = _issue_payload()
        resp = client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": event,
            },
        )
        assert resp.status_code == 202

    def test_unknown_event_with_project_accepted(self, _mock_process, client):
        resp = client.post(
            "/webhook",
            data=json.dumps({"object_kind": "something_new", "project": {"id": 42, "path_with_namespace": "t/r"}}),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Whatever Hook",
            },
        )
        assert resp.status_code == 202


class TestWebhookPayloadValidation:
    def test_invalid_json(self, client):
        resp = client.post(
            "/webhook",
            data="not json",
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Issue Hook",
            },
        )
        assert resp.status_code == 400

    def test_missing_project_id(self, client):
        resp = client.post(
            "/webhook",
            data=json.dumps({"project": {}}),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Issue Hook",
            },
        )
        assert resp.status_code == 400

    def test_no_project_key(self, client):
        resp = client.post(
            "/webhook",
            data=json.dumps({"object_kind": "issue"}),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Issue Hook",
            },
        )
        assert resp.status_code == 400


class TestWebhookProcessing:
    @patch("forgewright.webhook._process_event")
    def test_dispatches_issue_event(self, mock_process, client):
        payload = _issue_payload(project_id=99)
        resp = client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Issue Hook",
            },
        )
        assert resp.status_code == 202
        # Give the background thread a moment
        time.sleep(0.1)
        mock_process.assert_called_once()
        args = mock_process.call_args[0]
        assert args[1] == 99
        assert args[2] == "Issue Hook"

    @patch("forgewright.webhook._process_event")
    def test_dispatches_note_event(self, mock_process, client):
        payload = _note_payload(project_id=77)
        resp = client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Note Hook",
            },
        )
        assert resp.status_code == 202
        time.sleep(0.1)
        mock_process.assert_called_once()
        args = mock_process.call_args[0]
        assert args[1] == 77
        assert args[2] == "Note Hook"

    @patch("forgewright.webhook._process_event")
    def test_dispatches_mr_event(self, mock_process, client):
        payload = _mr_payload(project_id=55)
        resp = client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Merge Request Hook",
            },
        )
        assert resp.status_code == 202
        time.sleep(0.1)
        mock_process.assert_called_once()
        args = mock_process.call_args[0]
        assert args[1] == 55

    @patch("forgewright.webhook._process_event")
    def test_dispatches_pipeline_event(self, mock_process, client):
        payload = _pipeline_payload(project_id=33)
        resp = client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={
                "X-Gitlab-Token": "test-secret",
                "X-Gitlab-Event": "Pipeline Hook",
            },
        )
        assert resp.status_code == 202
        time.sleep(0.1)
        mock_process.assert_called_once()
        args = mock_process.call_args[0]
        assert args[1] == 33


class TestProcessEvent:
    @patch("forgewright.webhook.process_project")
    @patch("forgewright.webhook.State")
    @patch("forgewright.webhook.create_agent")
    @patch("forgewright.webhook.select_projects")
    @patch("forgewright.webhook.create_platform")
    def test_processes_matching_project(
        self, mock_plat_factory, mock_select, mock_agent_factory,
        mock_state, mock_process, webhook_config,
    ):
        from forgewright.webhook import _process_event

        mock_platform = MagicMock()
        mock_plat_factory.return_value = mock_platform
        mock_select.return_value = [_make_project(42)]
        mock_platform.project.return_value = _make_project(42)

        _process_event(webhook_config, 42, "Issue Hook")

        mock_process.assert_called_once()
        call_args = mock_process.call_args[0]
        assert call_args[3]  # state
        assert call_args[4].id == 42

    @patch("forgewright.webhook.process_project")
    @patch("forgewright.webhook.State")
    @patch("forgewright.webhook.create_agent")
    @patch("forgewright.webhook.select_projects")
    @patch("forgewright.webhook.create_platform")
    def test_skips_non_matching_project(
        self, mock_plat_factory, mock_select, mock_agent_factory,
        mock_state, mock_process, webhook_config,
    ):
        from forgewright.webhook import _process_event

        mock_plat_factory.return_value = MagicMock()
        mock_select.return_value = [_make_project(99, "other/repo")]

        _process_event(webhook_config, 42, "Issue Hook")

        mock_process.assert_not_called()

    @patch("forgewright.webhook.process_project")
    @patch("forgewright.webhook.State")
    @patch("forgewright.webhook.create_agent")
    @patch("forgewright.webhook.select_projects")
    @patch("forgewright.webhook.create_platform")
    def test_handles_process_exception(
        self, mock_plat_factory, mock_select, mock_agent_factory,
        mock_state, mock_process, webhook_config,
    ):
        from forgewright.webhook import _process_event

        mock_platform = MagicMock()
        mock_plat_factory.return_value = mock_platform
        mock_select.return_value = [_make_project(42, "t/r")]
        mock_platform.project.return_value = _make_project(42, "t/r")
        mock_process.side_effect = RuntimeError("boom")

        _process_event(webhook_config, 42, "Issue Hook")
        mock_process.assert_called_once()


class TestCreateApp:
    def test_returns_flask_app(self, webhook_config):
        app = create_app(webhook_config)
        assert app.name == "forgewright-webhook"

    def test_has_health_route(self, webhook_config):
        app = create_app(webhook_config)
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert "/health" in rules

    def test_has_webhook_route(self, webhook_config):
        app = create_app(webhook_config)
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert "/webhook" in rules


# ---- GitHub Webhook Tests ----


def _github_issue_payload(owner_repo: str = "owner/repo") -> dict:
    return {
        "action": "opened",
        "issue": {"number": 1, "title": "Test issue"},
        "repository": {"full_name": owner_repo},
    }


def _github_pr_payload(owner_repo: str = "owner/repo") -> dict:
    return {
        "action": "opened",
        "pull_request": {"number": 5, "title": "Test PR"},
        "repository": {"full_name": owner_repo},
    }


def _github_comment_payload(owner_repo: str = "owner/repo") -> dict:
    return {
        "action": "created",
        "comment": {"body": "@forgewright please help"},
        "issue": {"number": 1},
        "repository": {"full_name": owner_repo},
    }


@pytest.fixture
def github_webhook_config(tmp_path: Path) -> Config:
    return Config(
        platform_url="https://api.github.com",
        platform_token="ghp_test_token",
        platform_type="github",
        bot_username="forgewright",
        workdir=tmp_path / "work",
        state_file=tmp_path / "state.json",
        lock_dir=tmp_path / "locks",
        log_file=tmp_path / "bot.log",
        webhook_enabled=True,
        webhook_host="127.0.0.1",
        webhook_port=5000,
        webhook_secret="gh-secret",
        webhook_debounce_sec=0,
    )


@pytest.fixture
def github_webhook_config_no_secret(tmp_path: Path) -> Config:
    return Config(
        platform_url="https://api.github.com",
        platform_token="ghp_test_token",
        platform_type="github",
        bot_username="forgewright",
        workdir=tmp_path / "work",
        state_file=tmp_path / "state.json",
        lock_dir=tmp_path / "locks",
        log_file=tmp_path / "bot.log",
        webhook_enabled=True,
        webhook_host="127.0.0.1",
        webhook_port=5000,
        webhook_secret="",
        webhook_debounce_sec=0,
    )


def _github_hmac_headers(body: bytes, secret: str,
                         event: str = "issues") -> dict:
    """Build GitHub webhook headers with a valid HMAC signature."""
    import hashlib
    import hmac as hmac_mod
    sig = "sha256=" + hmac_mod.new(
        secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": event,
    }


@pytest.fixture
def github_client(github_webhook_config):
    app = create_app(github_webhook_config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def github_client_no_secret(github_webhook_config_no_secret):
    app = create_app(github_webhook_config_no_secret)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@patch("forgewright.webhook._process_event")
class TestGitHubWebhookAuth:
    def test_valid_hmac(self, _mock_process, github_client,
                        github_webhook_config):
        payload = _github_issue_payload()
        body = json.dumps(payload).encode()
        headers = _github_hmac_headers(
            body, github_webhook_config.webhook_secret, "issues")
        resp = github_client.post(
            "/webhook", data=body, content_type="application/json",
            headers=headers)
        assert resp.status_code == 202

    def test_invalid_hmac(self, _mock_process, github_client):
        payload = _github_issue_payload()
        resp = github_client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={
                "X-Hub-Signature-256": "sha256=wrong",
                "X-GitHub-Event": "issues",
            })
        assert resp.status_code == 401

    def test_missing_signature_header(self, _mock_process, github_client):
        payload = _github_issue_payload()
        resp = github_client.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"X-GitHub-Event": "issues"})
        assert resp.status_code == 401

    def test_no_secret_allows_all(self, _mock_process,
                                  github_client_no_secret):
        payload = _github_issue_payload()
        resp = github_client_no_secret.post(
            "/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"X-GitHub-Event": "issues"})
        assert resp.status_code == 202


@patch("forgewright.webhook._process_event")
class TestGitHubWebhookEventAcceptance:
    def _post(self, client, config, payload, event):
        body = json.dumps(payload).encode()
        headers = _github_hmac_headers(
            body, config.webhook_secret, event)
        return client.post("/webhook", data=body,
                           content_type="application/json",
                           headers=headers)

    def test_issue_event_accepted(self, _mock_process, github_client,
                                  github_webhook_config):
        resp = self._post(github_client, github_webhook_config,
                          _github_issue_payload(), "issues")
        assert resp.status_code == 202

    def test_pr_event_accepted(self, _mock_process, github_client,
                               github_webhook_config):
        resp = self._post(github_client, github_webhook_config,
                          _github_pr_payload(), "pull_request")
        assert resp.status_code == 202

    def test_comment_event_accepted(self, _mock_process, github_client,
                                    github_webhook_config):
        resp = self._post(github_client, github_webhook_config,
                          _github_comment_payload(), "issue_comment")
        assert resp.status_code == 202

    def test_push_event_accepted(self, _mock_process, github_client,
                                 github_webhook_config):
        payload = {"repository": {"full_name": "owner/repo"}}
        resp = self._post(github_client, github_webhook_config,
                          payload, "push")
        assert resp.status_code == 202

    def test_missing_repository_returns_400(self, _mock_process,
                                            github_client,
                                            github_webhook_config):
        payload = {"action": "opened"}
        resp = self._post(github_client, github_webhook_config,
                          payload, "issues")
        assert resp.status_code == 400


class TestGitHubWebhookProcessing:
    @patch("forgewright.webhook._process_event")
    def test_dispatches_issue_event(self, mock_process,
                                    github_client, github_webhook_config):
        payload = _github_issue_payload("octocat/hello")
        body = json.dumps(payload).encode()
        headers = _github_hmac_headers(
            body, github_webhook_config.webhook_secret, "issues")
        resp = github_client.post(
            "/webhook", data=body, content_type="application/json",
            headers=headers)
        assert resp.status_code == 202
        time.sleep(0.1)
        mock_process.assert_called_once()
        args = mock_process.call_args[0]
        assert args[1] == "octocat/hello"
        assert args[2] == "issues"

    @patch("forgewright.webhook._process_event")
    def test_dispatches_pr_event(self, mock_process,
                                 github_client, github_webhook_config):
        payload = _github_pr_payload("octocat/hello")
        body = json.dumps(payload).encode()
        headers = _github_hmac_headers(
            body, github_webhook_config.webhook_secret, "pull_request")
        resp = github_client.post(
            "/webhook", data=body, content_type="application/json",
            headers=headers)
        assert resp.status_code == 202
        time.sleep(0.1)
        mock_process.assert_called_once()
        args = mock_process.call_args[0]
        assert args[1] == "octocat/hello"


class TestGitHubProcessEvent:
    @patch("forgewright.webhook.process_project")
    @patch("forgewright.webhook.State")
    @patch("forgewright.webhook.create_agent")
    @patch("forgewright.webhook.select_projects")
    @patch("forgewright.webhook.create_platform")
    def test_processes_matching_github_project(
        self, mock_plat_factory, mock_select, mock_agent_factory,
        mock_state, mock_process, github_webhook_config,
    ):
        from forgewright.webhook import _process_event

        mock_platform = MagicMock()
        mock_plat_factory.return_value = mock_platform
        proj = _make_project("octocat/hello", "octocat/hello")
        mock_select.return_value = [proj]
        mock_platform.project.return_value = proj

        _process_event(github_webhook_config, "octocat/hello", "issues")

        mock_process.assert_called_once()
        call_args = mock_process.call_args[0]
        assert call_args[4].id == "octocat/hello"

    @patch("forgewright.webhook.process_project")
    @patch("forgewright.webhook.State")
    @patch("forgewright.webhook.create_agent")
    @patch("forgewright.webhook.select_projects")
    @patch("forgewright.webhook.create_platform")
    def test_skips_non_matching_github_project(
        self, mock_plat_factory, mock_select, mock_agent_factory,
        mock_state, mock_process, github_webhook_config,
    ):
        from forgewright.webhook import _process_event

        mock_plat_factory.return_value = MagicMock()
        mock_select.return_value = [
            _make_project("other/repo", "other/repo")]

        _process_event(github_webhook_config, "octocat/hello", "issues")

        mock_process.assert_not_called()


# ---- Debounce Tests ----


class TestDebounceManager:
    def test_fires_after_delay(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        result = threading.Event()
        mgr.schedule("proj-1", 0.1, lambda: result.set())
        assert result.wait(timeout=2.0)

    def test_resets_timer_on_repeated_events(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        call_count = []

        def callback():
            call_count.append(1)

        mgr.schedule("proj-1", 0.3, callback)
        time.sleep(0.1)
        mgr.schedule("proj-1", 0.3, callback)
        time.sleep(0.1)
        mgr.schedule("proj-1", 0.3, callback)
        time.sleep(0.5)
        assert len(call_count) == 1

    def test_different_projects_independent(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        results = {"a": threading.Event(), "b": threading.Event()}
        mgr.schedule("a", 0.1, lambda: results["a"].set())
        mgr.schedule("b", 0.1, lambda: results["b"].set())
        assert results["a"].wait(timeout=2.0)
        assert results["b"].wait(timeout=2.0)

    def test_zero_delay_fires_immediately(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        result = threading.Event()
        mgr.schedule("proj-1", 0, lambda: result.set())
        assert result.wait(timeout=2.0)

    def test_cancel_all(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        call_count = []
        mgr.schedule("proj-1", 0.3, lambda: call_count.append(1))
        mgr.cancel_all()
        time.sleep(0.5)
        assert len(call_count) == 0

    def test_pending_returns_active_timers(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        mgr.schedule("proj-1", 10, lambda: None)
        assert "proj-1" in mgr.pending()
        mgr.cancel_all()

    def test_latest_event_type_used(self):
        from forgewright.webhook import _DebounceManager

        mgr = _DebounceManager()
        captured = []

        mgr.schedule("proj-1", 0.3, lambda v: captured.append(v),
                      args=("first",))
        time.sleep(0.1)
        mgr.schedule("proj-1", 0.3, lambda v: captured.append(v),
                      args=("second",))
        time.sleep(0.5)
        assert captured == ["second"]


class TestWebhookDebounceIntegration:
    def _make_debounce_config(self, tmp_path, debounce_sec):
        return Config(
            platform_url="https://git.example.com",
            platform_token="glpat-test-token",
            bot_username="forgewright",
            workdir=tmp_path / "work",
            state_file=tmp_path / "state.json",
            lock_dir=tmp_path / "locks",
            log_file=tmp_path / "bot.log",
            webhook_enabled=True,
            webhook_host="127.0.0.1",
            webhook_port=5000,
            webhook_secret="test-secret",
            webhook_debounce_sec=debounce_sec,
        )

    @patch("forgewright.webhook._process_event")
    def test_debounce_coalesces_rapid_events(self, mock_process, tmp_path):
        cfg = self._make_debounce_config(tmp_path, debounce_sec=0.3)
        app = create_app(cfg)
        app.config["TESTING"] = True
        with app.test_client() as client:
            for _ in range(5):
                client.post(
                    "/webhook",
                    data=json.dumps(_issue_payload(project_id=42)),
                    content_type="application/json",
                    headers={
                        "X-Gitlab-Token": "test-secret",
                        "X-Gitlab-Event": "Issue Hook",
                    },
                )
            time.sleep(0.6)
            assert mock_process.call_count == 1

    @patch("forgewright.webhook._process_event")
    def test_debounce_different_projects_fire_separately(
        self, mock_process, tmp_path,
    ):
        cfg = self._make_debounce_config(tmp_path, debounce_sec=0.2)
        app = create_app(cfg)
        app.config["TESTING"] = True
        with app.test_client() as client:
            client.post(
                "/webhook",
                data=json.dumps(_issue_payload(project_id=42)),
                content_type="application/json",
                headers={
                    "X-Gitlab-Token": "test-secret",
                    "X-Gitlab-Event": "Issue Hook",
                },
            )
            client.post(
                "/webhook",
                data=json.dumps(_issue_payload(project_id=99)),
                content_type="application/json",
                headers={
                    "X-Gitlab-Token": "test-secret",
                    "X-Gitlab-Event": "Issue Hook",
                },
            )
            time.sleep(0.5)
            assert mock_process.call_count == 2

    def test_health_reports_pending_debounce(self, tmp_path):
        cfg = self._make_debounce_config(tmp_path, debounce_sec=10)
        app = create_app(cfg)
        app.config["TESTING"] = True
        with app.test_client() as client:
            client.post(
                "/webhook",
                data=json.dumps(_issue_payload(project_id=42)),
                content_type="application/json",
                headers={
                    "X-Gitlab-Token": "test-secret",
                    "X-Gitlab-Event": "Issue Hook",
                },
            )
            resp = client.get("/health")
            data = resp.get_json()
            assert data["status"] == "ok"
        app.debounce.cancel_all()
