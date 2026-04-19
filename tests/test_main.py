"""Tests for forgewright.main — CLI entry point and logging setup."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forgewright.main import main, setup_logging
from forgewright.types import Project, User


class TestSetupLogging:
    def test_creates_log_file(self, tmp_path):
        log_path = tmp_path / "logs" / "bot.log"
        setup_logging(log_path, verbose=False)
        assert log_path.parent.exists()
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def test_verbose_sets_debug(self, tmp_path):
        log_path = tmp_path / "bot.log"
        root = logging.getLogger()
        root.handlers.clear()
        setup_logging(log_path, verbose=True)
        assert root.level == logging.DEBUG
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def test_non_verbose_sets_info(self, tmp_path):
        log_path = tmp_path / "bot.log"
        root = logging.getLogger()
        root.handlers.clear()
        setup_logging(log_path, verbose=False)
        assert root.level == logging.INFO
        root.handlers.clear()
        root.setLevel(logging.WARNING)


def _make_project(pid=1, path="test/proj"):
    return Project(id=pid, path=path, web_url="", default_branch="main",
                   http_clone_url="")


class TestMain:
    def _write_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"gitlab_url: https://git.example.com\n"
            f"gitlab_token: test-token\n"
            f"bot_username: forgewright\n"
            f"workdir: {tmp_path / 'work'}\n"
            f"state_file: {tmp_path / 'state.json'}\n"
            f"lock_dir: {tmp_path / 'locks'}\n"
            f"log_file: {tmp_path / 'bot.log'}\n"
        )
        return str(config_file)

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_dry_run(self, mock_plat_factory, mock_agent_factory,
                     mock_select, mock_process, tmp_path):
        config_file = self._write_config(tmp_path)
        mock_platform = MagicMock()
        mock_platform.current_user.return_value = User(username="forgewright")
        mock_plat_factory.return_value = mock_platform
        mock_agent_factory.return_value = MagicMock(name="MockAgent")
        mock_select.return_value = [_make_project()]

        with patch("sys.argv", ["forgewright", "--config", config_file,
                                 "--dry-run"]):
            result = main()

        assert result == 0
        mock_process.assert_not_called()

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_normal_run(self, mock_plat_factory, mock_agent_factory,
                        mock_select, mock_process, tmp_path):
        config_file = self._write_config(tmp_path)
        mock_platform = MagicMock()
        mock_platform.current_user.return_value = User(username="forgewright")
        mock_plat_factory.return_value = mock_platform
        mock_agent_factory.return_value = MagicMock(name="MockAgent")
        mock_select.return_value = [_make_project()]

        with patch("sys.argv", ["forgewright", "--config", config_file]):
            result = main()

        assert result == 0
        mock_process.assert_called_once()

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_auth_failure(self, mock_plat_factory, mock_agent_factory,
                          mock_select, mock_process, tmp_path):
        config_file = self._write_config(tmp_path)
        mock_platform = MagicMock()
        mock_platform.current_user.side_effect = RuntimeError("auth failed")
        mock_plat_factory.return_value = mock_platform

        with patch("sys.argv", ["forgewright", "--config", config_file]):
            result = main()

        assert result == 2
        mock_process.assert_not_called()

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_project_filter(self, mock_plat_factory, mock_agent_factory,
                            mock_select, mock_process, tmp_path):
        config_file = self._write_config(tmp_path)
        mock_platform = MagicMock()
        mock_platform.current_user.return_value = User(username="forgewright")
        mock_plat_factory.return_value = mock_platform
        mock_agent_factory.return_value = MagicMock(name="MockAgent")
        mock_select.return_value = [
            _make_project(pid=1, path="a/b"),
            _make_project(pid=2, path="c/d"),
        ]

        with patch("sys.argv", ["forgewright", "--config", config_file,
                                 "--project", "a/b"]):
            result = main()

        assert result == 0
        assert mock_process.call_count == 1
        project_arg = mock_process.call_args[0][4]
        assert project_arg.path == "a/b"

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_username_mismatch_updates_config(
            self, mock_plat_factory, mock_agent_factory,
            mock_select, mock_process, tmp_path):
        config_file = self._write_config(tmp_path)
        mock_platform = MagicMock()
        mock_platform.current_user.return_value = User(username="real-bot")
        mock_plat_factory.return_value = mock_platform
        mock_agent_factory.return_value = MagicMock(name="MockAgent")
        mock_select.return_value = []

        with patch("sys.argv", ["forgewright", "--config", config_file]):
            result = main()

        assert result == 0

    @patch("forgewright.main.process_project")
    @patch("forgewright.main.select_projects")
    @patch("forgewright.main.create_agent")
    @patch("forgewright.main.create_platform")
    def test_project_crash_continues(self, mock_plat_factory, mock_agent_factory,
                                     mock_select, mock_process, tmp_path):
        config_file = self._write_config(tmp_path)
        mock_platform = MagicMock()
        mock_platform.current_user.return_value = User(username="forgewright")
        mock_plat_factory.return_value = mock_platform
        mock_agent_factory.return_value = MagicMock(name="MockAgent")
        mock_select.return_value = [
            _make_project(pid=1, path="a/b"),
            _make_project(pid=2, path="c/d"),
        ]
        mock_process.side_effect = [RuntimeError("boom"), None]

        with patch("sys.argv", ["forgewright", "--config", config_file]):
            result = main()

        assert result == 0
        assert mock_process.call_count == 2

    @patch("forgewright.webhook.run_server")
    def test_serve_mode(self, mock_run_server, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"gitlab_url: https://git.example.com\n"
            f"gitlab_token: test-token\n"
            f"bot_username: forgewright\n"
            f"workdir: {tmp_path / 'work'}\n"
            f"state_file: {tmp_path / 'state.json'}\n"
            f"lock_dir: {tmp_path / 'locks'}\n"
            f"log_file: {tmp_path / 'bot.log'}\n"
            f"webhook_enabled: true\n"
        )

        with patch("sys.argv", ["forgewright", "--config", str(config_file),
                                 "--serve"]):
            result = main()

        assert result == 0
        mock_run_server.assert_called_once()

    def test_serve_mode_disabled(self, tmp_path):
        config_file = self._write_config(tmp_path)

        with patch("sys.argv", ["forgewright", "--config", config_file,
                                 "--serve"]):
            result = main()

        assert result == 1
