"""Regression tests for the project's identity — package name, defaults,
environment variables, and the bundled config example.

These guard against a partial rename: if someone reverts one of the new
defaults or renames the package back, at least one of these tests will fail
loudly instead of the rename silently rotting across the tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import forgewright
from forgewright.config import DEFAULT_CONFIG_PATH, Config, _mention_re
from forgewright.webhook import create_app


REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPackageIdentity:
    def test_package_importable(self):
        assert forgewright.__name__ == "forgewright"

    def test_version_present(self):
        assert isinstance(forgewright.__version__, str)
        assert forgewright.__version__

    def test_entry_point_script_exists(self):
        """forgewright.py sits at the repo root as a thin shim."""
        assert (REPO_ROOT / "forgewright.py").is_file()

    def test_no_legacy_package(self):
        """The old claude_bot package must be gone, not aliased."""
        assert not (REPO_ROOT / "claude_bot").exists()
        assert not (REPO_ROOT / "claude_bot.py").exists()


class TestConfigDefaults:
    """Defaults encode the bot's identity; changing them is a breaking change."""

    def _minimal(self, tmp_path) -> Config:
        return Config(
            platform_url="https://git.example.com",
            platform_token="tok",
            bot_username="forgewright",
            workdir=tmp_path / "work",
            state_file=tmp_path / "state.json",
            lock_dir=tmp_path / "locks",
            log_file=tmp_path / "bot.log",
        )

    def test_branch_prefix_default(self, tmp_path):
        assert self._minimal(tmp_path).branch_prefix == "forgewright/"

    def test_git_user_name_default(self, tmp_path):
        assert self._minimal(tmp_path).git_user_name == "Forgewright"

    def test_git_user_email_default(self, tmp_path):
        assert self._minimal(tmp_path).git_user_email == "forgewright@example.com"

    def test_claude_binary_default(self, tmp_path):
        # The agent CLI binary is still called `claude` — it's Claude Code,
        # not the bot. This stays put across the rename.
        assert self._minimal(tmp_path).claude_binary == "claude"

    def test_agent_type_default(self, tmp_path):
        # Agent type value is unchanged: "claude" means Claude Code.
        assert self._minimal(tmp_path).agent_type == "claude"

    def test_platform_type_default(self, tmp_path):
        assert self._minimal(tmp_path).platform_type == "gitlab"


class TestDefaultConfigPath:
    def test_path(self):
        assert DEFAULT_CONFIG_PATH == "/etc/forgewright/config.yaml"


class TestDefaultMentionRegex:
    def test_default_matches_forgewright(self):
        """_mention_re() with no argument uses 'forgewright' as the default."""
        assert _mention_re().search("Hey @forgewright fix this")

    def test_default_does_not_match_claude(self):
        """Old @claude mentions should NOT trigger with the default regex."""
        assert not _mention_re().search("Hey @claude fix this")

    def test_default_case_insensitive(self):
        assert _mention_re().search("Hey @Forgewright help")
        assert _mention_re().search("Hey @FORGEWRIGHT help")

    def test_default_not_part_of_email(self):
        assert not _mention_re().search("user@forgewright.com")


class TestGitAuthEnv:
    def test_forgewright_git_token_env_var(self, tmp_path):
        """The env var handed to GIT_ASKPASS is FORGEWRIGHT_GIT_TOKEN."""
        cfg = Config(
            platform_url="https://git.example.com",
            platform_token="glpat-xxx",
            bot_username="forgewright",
            workdir=tmp_path / "work",
            state_file=tmp_path / "state.json",
            lock_dir=tmp_path / "locks",
            log_file=tmp_path / "bot.log",
        )
        env = cfg.git_auth_env()
        assert env["FORGEWRIGHT_GIT_TOKEN"] == "glpat-xxx"
        assert "CLAUDE_BOT_GIT_TOKEN" not in env

    def test_askpass_script_references_new_env_var(self, tmp_path):
        cfg = Config(
            platform_url="https://git.example.com",
            platform_token="glpat-xxx",
            bot_username="forgewright",
            workdir=tmp_path / "work",
            state_file=tmp_path / "state.json",
            lock_dir=tmp_path / "locks",
            log_file=tmp_path / "bot.log",
        )
        env = cfg.git_auth_env()
        askpass = Path(env["GIT_ASKPASS"]).read_text()
        assert "FORGEWRIGHT_GIT_TOKEN" in askpass
        assert "CLAUDE_BOT_GIT_TOKEN" not in askpass


class TestBundledConfigExample:
    """The shipped config.example.yaml must parse and advertise the new identity."""

    @pytest.fixture
    def example_raw(self) -> dict:
        with open(REPO_ROOT / "config.example.yaml") as f:
            return yaml.safe_load(f)

    def test_bot_username(self, example_raw):
        assert example_raw["bot_username"] == "forgewright"

    def test_branch_prefix(self, example_raw):
        assert example_raw["branch_prefix"] == "forgewright/"

    def test_git_user_name(self, example_raw):
        assert example_raw["git_user_name"] == "Forgewright"

    def test_git_user_email(self, example_raw):
        assert example_raw["git_user_email"] == "forgewright@example.com"

    def test_paths_under_forgewright(self, example_raw):
        for key in ("workdir", "state_file", "lock_dir", "log_file"):
            assert "forgewright" in example_raw[key], (
                f"{key}={example_raw[key]!r} should reference forgewright")
            assert "claude-bot" not in example_raw[key]

    def test_loads_through_config(self, tmp_path, example_raw):
        """Feeding config.example.yaml into Config.load() round-trips cleanly."""
        example_raw["platform_token"] = "test-token"
        for key in ("workdir", "state_file", "lock_dir", "log_file"):
            example_raw[key] = str(tmp_path / Path(example_raw[key]).name)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(example_raw))
        cfg = Config.load(str(path))
        assert cfg.bot_username == "forgewright"
        assert cfg.branch_prefix == "forgewright/"
        assert cfg.git_user_email == "forgewright@example.com"


class TestWebhookAppName:
    def test_flask_app_name(self, tmp_path):
        cfg = Config(
            platform_url="https://git.example.com",
            platform_token="tok",
            bot_username="forgewright",
            workdir=tmp_path / "work",
            state_file=tmp_path / "state.json",
            lock_dir=tmp_path / "locks",
            log_file=tmp_path / "bot.log",
            webhook_enabled=True,
        )
        app = create_app(cfg)
        assert app.name == "forgewright-webhook"


class TestCliEntryPoints:
    """The package must run via all three advertised entry points."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            args, capture_output=True, text=True, env=env,
            cwd=REPO_ROOT, timeout=15,
        )

    def test_module_invocation(self):
        """`python -m forgewright --help` works and advertises the new name."""
        result = self._run(sys.executable, "-m", "forgewright", "--help")
        assert result.returncode == 0
        assert "forgewright" in result.stdout

    def test_shim_invocation(self):
        """`python forgewright.py --help` works."""
        result = self._run(sys.executable, "forgewright.py", "--help")
        assert result.returncode == 0
        assert "forgewright" in result.stdout

    def test_module_help_does_not_mention_claude_bot(self):
        result = self._run(sys.executable, "-m", "forgewright", "--help")
        assert "claude-bot" not in result.stdout
        assert "claude_bot" not in result.stdout


class TestConfigEnvVar:
    """main() should honour FORGEWRIGHT_CONFIG as the default config path."""

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_forgewright_config_env_respected(
        self, mock_plat_factory, mock_agent_factory,
        mock_select, mock_process, tmp_path, monkeypatch,
    ):
        from forgewright.types import User
        from forgewright.main import main

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "platform_url: https://git.example.com\n"
            "platform_token: tok\n"
            "bot_username: forgewright\n"
            f"workdir: {tmp_path / 'work'}\n"
            f"state_file: {tmp_path / 'state.json'}\n"
            f"lock_dir: {tmp_path / 'locks'}\n"
            f"log_file: {tmp_path / 'bot.log'}\n"
        )

        platform = MagicMock()
        platform.current_user.return_value = User(username="forgewright")
        mock_plat_factory.return_value = platform
        mock_agent_factory.return_value = MagicMock(name="MockAgent")
        mock_select.return_value = []

        monkeypatch.setenv("FORGEWRIGHT_CONFIG", str(config_file))

        with patch("sys.argv", ["forgewright"]):
            assert main() == 0


class TestDecisionLabelInternal:
    """The renamed internal helper variable should produce the new reason string."""

    def test_not_a_bot_owned_mr_reason(self):
        from forgewright.decision import should_process_mr
        from tests.conftest import make_mr

        mr = make_mr(source_branch="feature/foo", author="alice",
                     description="No mention")
        go, reason = should_process_mr(
            mr, [], [], None, "forgewright", "forgewright/")
        assert not go
        assert "not a bot-owned MR" in reason
        assert "claude" not in reason
