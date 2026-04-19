"""Tests for forgewright.config."""

import os
from pathlib import Path

import pytest
import yaml

from forgewright.config import Config


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    """Write a minimal valid config file."""
    cfg = {
        "gitlab_url": "https://git.example.com",
        "gitlab_token": "glpat-test",
        "bot_username": "forgewright",
        "workdir": str(tmp_path / "work"),
        "state_file": str(tmp_path / "state.json"),
        "lock_dir": str(tmp_path / "locks"),
        "log_file": str(tmp_path / "bot.log"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    return path


@pytest.fixture
def new_style_config(tmp_path: Path) -> Path:
    """Write a config using the new platform_url/platform_token keys."""
    cfg = {
        "platform_url": "https://git.example.com",
        "platform_token": "glpat-test",
        "bot_username": "forgewright",
        "workdir": str(tmp_path / "work"),
        "state_file": str(tmp_path / "state.json"),
        "lock_dir": str(tmp_path / "locks"),
        "log_file": str(tmp_path / "bot.log"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    return path


class TestConfigLoad:
    def test_load_minimal(self, minimal_config):
        cfg = Config.load(str(minimal_config))
        assert cfg.platform_url == "https://git.example.com"
        assert cfg.bot_username == "forgewright"
        assert cfg.agent_type == "claude"
        assert cfg.platform_type == "gitlab"

    def test_gitlab_url_alias(self, minimal_config):
        cfg = Config.load(str(minimal_config))
        assert cfg.gitlab_url == cfg.platform_url
        assert cfg.gitlab_token == cfg.platform_token

    def test_new_style_config(self, new_style_config):
        cfg = Config.load(str(new_style_config))
        assert cfg.platform_url == "https://git.example.com"
        assert cfg.platform_token == "glpat-test"

    def test_missing_required_key(self, tmp_path):
        cfg = {"gitlab_url": "https://git.example.com",
               "gitlab_token": "tok"}
        # Missing bot_username and others
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        with pytest.raises(SystemExit, match="required config key missing"):
            Config.load(str(path))

    def test_missing_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("PLATFORM_TOKEN", raising=False)
        cfg = {
            "gitlab_url": "https://git.example.com",
            "gitlab_token": "",
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        with pytest.raises(SystemExit, match="platform_token missing"):
            Config.load(str(path))

    def test_token_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "env-token")
        cfg = {
            "gitlab_url": "https://git.example.com",
            "gitlab_token": "",  # empty in config
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        loaded = Config.load(str(path))
        assert loaded.platform_token == "env-token"

    def test_token_from_platform_token_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.setenv("PLATFORM_TOKEN", "platform-env-token")
        cfg = {
            "platform_url": "https://git.example.com",
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        loaded = Config.load(str(path))
        assert loaded.platform_token == "platform-env-token"

    def test_defaults_for_new_fields(self, minimal_config):
        cfg = Config.load(str(minimal_config))
        assert cfg.agent_type == "claude"
        assert cfg.platform_type == "gitlab"
        assert cfg.opencode_binary == "opencode"
        assert cfg.opencode_model is None
        assert cfg.claude_timeout_sec == 3600

    def test_custom_agent_type(self, tmp_path):
        cfg = {
            "gitlab_url": "https://git.example.com",
            "gitlab_token": "tok",
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
            "agent_type": "opencode",
            "opencode_binary": "/usr/bin/opencode",
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        loaded = Config.load(str(path))
        assert loaded.agent_type == "opencode"
        assert loaded.opencode_binary == "/usr/bin/opencode"

    def test_url_trailing_slash_stripped(self, minimal_config):
        cfg = Config.load(str(minimal_config))
        assert not cfg.platform_url.endswith("/")

    def test_webhook_defaults(self, minimal_config):
        cfg = Config.load(str(minimal_config))
        assert cfg.webhook_enabled is False
        assert cfg.webhook_host == "127.0.0.1"
        assert cfg.webhook_port == 5000
        assert cfg.webhook_secret == ""

    def test_webhook_custom_values(self, tmp_path):
        cfg = {
            "gitlab_url": "https://git.example.com",
            "gitlab_token": "tok",
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
            "webhook_enabled": True,
            "webhook_host": "0.0.0.0",
            "webhook_port": 8080,
            "webhook_secret": "my-secret",
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        loaded = Config.load(str(path))
        assert loaded.webhook_enabled is True
        assert loaded.webhook_host == "0.0.0.0"
        assert loaded.webhook_port == 8080
        assert loaded.webhook_secret == "my-secret"

    def test_webhook_secret_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WEBHOOK_SECRET", "env-secret")
        cfg = {
            "gitlab_url": "https://git.example.com",
            "gitlab_token": "tok",
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
            "webhook_secret": "",
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        loaded = Config.load(str(path))
        assert loaded.webhook_secret == "env-secret"


class TestGitAuthEnv:
    def test_default_token(self, tmp_config):
        tmp_config.workdir.mkdir(parents=True, exist_ok=True)
        env = tmp_config.git_auth_env()
        assert env["FORGEWRIGHT_GIT_TOKEN"] == "glpat-test-token"
        assert "GIT_ASKPASS" in env

    def test_explicit_token(self, tmp_config):
        tmp_config.workdir.mkdir(parents=True, exist_ok=True)
        env = tmp_config.git_auth_env("custom-token")
        assert env["FORGEWRIGHT_GIT_TOKEN"] == "custom-token"

    def test_askpass_script_created(self, tmp_config):
        tmp_config.workdir.mkdir(parents=True, exist_ok=True)
        env = tmp_config.git_auth_env()
        askpass = Path(env["GIT_ASKPASS"])
        assert askpass.exists()
        assert askpass.stat().st_mode & 0o700 == 0o700
