"""Extended tests for forgewright.config — edge cases and git_auth_env."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from forgewright.config import Config, _mention_re


@contextlib.contextmanager
def patch_env(**kwargs):
    old = {}
    for k, v in kwargs.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestGitAuthEnv:
    def test_creates_askpass_script(self, tmp_config):
        env = tmp_config.git_auth_env()
        askpass = Path(env["GIT_ASKPASS"])
        assert askpass.exists()
        content = askpass.read_text()
        assert "FORGEWRIGHT_GIT_TOKEN" in content

    def test_askpass_is_executable(self, tmp_config):
        env = tmp_config.git_auth_env()
        askpass = Path(env["GIT_ASKPASS"])
        assert os.access(askpass, os.X_OK)

    def test_token_in_env(self, tmp_config):
        env = tmp_config.git_auth_env()
        assert env["FORGEWRIGHT_GIT_TOKEN"] == tmp_config.platform_token

    def test_explicit_token_overrides_default(self, tmp_config):
        env = tmp_config.git_auth_env("custom-tok")
        assert env["FORGEWRIGHT_GIT_TOKEN"] == "custom-tok"

    def test_terminal_prompt_disabled(self, tmp_config):
        env = tmp_config.git_auth_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_askpass_reused_on_second_call(self, tmp_config):
        env1 = tmp_config.git_auth_env()
        env2 = tmp_config.git_auth_env()
        assert env1["GIT_ASKPASS"] == env2["GIT_ASKPASS"]

    def test_creates_workdir_if_missing(self, tmp_path):
        cfg = Config(
            platform_url="https://example.com",
            platform_token="token",
            bot_username="forgewright",
            workdir=tmp_path / "nonexistent" / "work",
            state_file=tmp_path / "state.json",
            lock_dir=tmp_path / "locks",
            log_file=tmp_path / "bot.log",
        )
        env = cfg.git_auth_env()
        assert (tmp_path / "nonexistent" / "work").exists()


class TestConfigLoad:
    def _write_config(self, tmp_path, **overrides):
        defaults = {
            "gitlab_url": "https://git.example.com",
            "gitlab_token": "test-token",
            "bot_username": "forgewright",
            "workdir": str(tmp_path / "work"),
            "state_file": str(tmp_path / "state.json"),
            "lock_dir": str(tmp_path / "locks"),
            "log_file": str(tmp_path / "bot.log"),
        }
        defaults.update(overrides)
        lines = [f"{k}: {v}" for k, v in defaults.items()]
        config_file = tmp_path / "config.yaml"
        config_file.write_text("\n".join(lines))
        return str(config_file)

    def test_loads_basic_config(self, tmp_path):
        path = self._write_config(tmp_path)
        cfg = Config.load(path)
        assert cfg.platform_url == "https://git.example.com"
        assert cfg.gitlab_url == "https://git.example.com"
        assert cfg.bot_username == "forgewright"

    def test_strips_trailing_slash(self, tmp_path):
        path = self._write_config(tmp_path, gitlab_url="https://git.example.com/")
        cfg = Config.load(path)
        assert not cfg.platform_url.endswith("/")

    def test_missing_token_raises(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "gitlab_url: https://example.com\n"
            "bot_username: forgewright\n"
            "workdir: /tmp\n"
            "state_file: /tmp/s.json\n"
            "lock_dir: /tmp/l\n"
            "log_file: /tmp/b.log\n"
        )
        with pytest.raises(SystemExit, match="platform_token"):
            with patch_env(GITLAB_TOKEN="", PLATFORM_TOKEN=""):
                Config.load(str(config_file))

    def test_missing_required_key_raises(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "gitlab_url: https://example.com\n"
            "gitlab_token: tok\n"
        )
        with pytest.raises(SystemExit, match="required config key"):
            Config.load(str(config_file))

    def test_env_fallback_for_token(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "gitlab_url: https://example.com\n"
            "bot_username: forgewright\n"
            "workdir: /tmp\n"
            "state_file: /tmp/s.json\n"
            "lock_dir: /tmp/l\n"
            "log_file: /tmp/b.log\n"
        )
        with patch_env(GITLAB_TOKEN="env-token"):
            cfg = Config.load(str(config_file))
        assert cfg.platform_token == "env-token"
        assert cfg.gitlab_token == "env-token"

    def test_defaults(self, tmp_path):
        path = self._write_config(tmp_path)
        cfg = Config.load(path)
        assert cfg.branch_prefix == "forgewright/"
        assert cfg.platform_type == "gitlab"
        assert cfg.agent_type == "claude"
        assert cfg.claude_timeout_sec == 3600
        assert cfg.webhook_enabled is False

    def test_custom_values(self, tmp_path):
        path = self._write_config(
            tmp_path,
            branch_prefix="bot/",
            agent_type="opencode",
            claude_timeout_sec="120",
            webhook_enabled="true",
            webhook_port="8080",
        )
        cfg = Config.load(path)
        assert cfg.branch_prefix == "bot/"
        assert cfg.agent_type == "opencode"
        assert cfg.claude_timeout_sec == 120
        assert cfg.webhook_enabled is True
        assert cfg.webhook_port == 8080


class TestMentionRe:
    def test_basic_match(self):
        assert _mention_re("forgewright").search("@forgewright help")

    def test_no_match_in_email(self):
        assert not _mention_re("forgewright").search("user@forgewright.com")

    def test_case_insensitive(self):
        assert _mention_re("forgewright").search("@FORGEWRIGHT help")

    def test_caching(self):
        r1 = _mention_re("testbot")
        r2 = _mention_re("testbot")
        assert r1 is r2
