"""Tests for forgewright.agent — factory, ClaudeCodeAgent, OpenCodeAgent."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forgewright.agent import create_agent
from forgewright.agent.base import Agent, AgentResult
from forgewright.agent.claude_code import ClaudeCodeAgent
from forgewright.agent.opencode import OpenCodeAgent


class TestAgentResult:
    def test_fields(self):
        r = AgentResult(ok=True, output="done", summary="all good")
        assert r.ok is True
        assert r.output == "done"
        assert r.summary == "all good"

    def test_falsy_result(self):
        r = AgentResult(ok=False, output="", summary="")
        assert r.ok is False


class TestCreateAgent:
    def test_creates_claude_agent(self, tmp_config):
        tmp_config.agent_type = "claude"
        agent = create_agent(tmp_config)
        assert isinstance(agent, ClaudeCodeAgent)
        assert agent.name == "Claude Code"

    def test_creates_opencode_agent(self, tmp_config):
        tmp_config.agent_type = "opencode"
        agent = create_agent(tmp_config)
        assert isinstance(agent, OpenCodeAgent)
        assert agent.name == "OpenCode"

    def test_unknown_agent_type_raises(self, tmp_config):
        tmp_config.agent_type = "unknown"
        with pytest.raises(ValueError, match="unknown agent_type"):
            create_agent(tmp_config)

    def test_passes_config_to_claude(self, tmp_config):
        tmp_config.agent_type = "claude"
        tmp_config.claude_binary = "/usr/bin/claude"
        tmp_config.claude_model = "opus"
        tmp_config.claude_timeout_sec = 999
        agent = create_agent(tmp_config)
        assert agent._binary == "/usr/bin/claude"
        assert agent._model == "opus"
        assert agent._timeout == 999

    def test_passes_config_to_opencode(self, tmp_config):
        tmp_config.agent_type = "opencode"
        tmp_config.opencode_binary = "/usr/bin/opencode"
        tmp_config.opencode_model = "gpt-4"
        tmp_config.claude_timeout_sec = 500
        agent = create_agent(tmp_config)
        assert agent._binary == "/usr/bin/opencode"
        assert agent._model == "gpt-4"
        assert agent._timeout == 500


class TestClaudeCodeAgent:
    def test_name(self):
        agent = ClaudeCodeAgent()
        assert agent.name == "Claude Code"

    def test_default_params(self):
        agent = ClaudeCodeAgent()
        assert agent._binary == "claude"
        assert agent._model is None
        assert agent._timeout == 3600

    def test_custom_params(self):
        agent = ClaudeCodeAgent(binary="/bin/c", model="sonnet", timeout_sec=60)
        assert agent._binary == "/bin/c"
        assert agent._model == "sonnet"
        assert agent._timeout == 60

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_run_success(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter(["line1\n", "line2\n"])
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        summary_dir = tmp_path / ".claude"
        summary_dir.mkdir()
        (summary_dir / "last-run-summary.md").write_text("test summary")

        agent = ClaudeCodeAgent(timeout_sec=300)
        result = agent.run("do stuff", tmp_path)

        assert result.ok is True
        assert "line1" in result.output
        assert "line2" in result.output
        assert result.summary == "test summary"

        cmd = mock_popen.call_args[0][0]
        assert cmd[:3] == ["claude", "-p", "do stuff"]
        assert "--dangerously-skip-permissions" in cmd

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_run_with_model(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        (tmp_path / ".claude").mkdir()

        agent = ClaudeCodeAgent(model="opus")
        agent.run("prompt", tmp_path)

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert "opus" in cmd

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_run_failure(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter(["error\n"])
        proc.wait.return_value = 1
        proc.returncode = 1
        mock_popen.return_value = proc

        (tmp_path / ".claude").mkdir()

        agent = ClaudeCodeAgent()
        result = agent.run("fail", tmp_path)

        assert result.ok is False
        assert "error" in result.output

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_run_creates_live_log(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter(["output\n"])
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        agent = ClaudeCodeAgent()
        agent.run("prompt", tmp_path)

        live_log = tmp_path / ".claude" / "claude-live.log"
        assert live_log.exists()
        assert "output" in live_log.read_text()

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_run_sets_ci_env(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        (tmp_path / ".claude").mkdir()

        agent = ClaudeCodeAgent()
        agent.run("prompt", tmp_path)

        env = mock_popen.call_args[1]["env"]
        assert env.get("CI"), "CI env var should be set"

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_timeout_returns_failure(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.returncode = -9

        def slow_stdout():
            import time
            yield "partial output\n"
            time.sleep(5)

        proc.stdout = slow_stdout()

        def kill_side_effect():
            pass

        proc.kill = MagicMock(side_effect=kill_side_effect)
        proc.wait = MagicMock(return_value=-9)
        mock_popen.return_value = proc

        (tmp_path / ".claude").mkdir()

        agent = ClaudeCodeAgent(timeout_sec=0)

        import threading
        original_timer = threading.Timer

        class InstantTimer(threading.Timer):
            def __init__(self, interval, function, *args, **kwargs):
                super().__init__(0.01, function, *args, **kwargs)

        with patch("forgewright.agent.claude_code.threading.Timer", InstantTimer):
            import time
            time.sleep(0.05)
            result = agent.run("prompt", tmp_path)

        assert result.ok is False
        assert "TIMEOUT" in result.output

    @patch("forgewright.agent.claude_code.subprocess.Popen")
    def test_popen_exception_reraises(self, mock_popen, tmp_path):
        mock_popen.side_effect = FileNotFoundError("claude not found")

        (tmp_path / ".claude").mkdir()

        agent = ClaudeCodeAgent()
        with pytest.raises(FileNotFoundError, match="claude not found"):
            agent.run("prompt", tmp_path)


class TestOpenCodeAgent:
    def test_name(self):
        agent = OpenCodeAgent()
        assert agent.name == "OpenCode"

    def test_default_params(self):
        agent = OpenCodeAgent()
        assert agent._binary == "opencode"
        assert agent._model is None
        assert agent._timeout == 3600

    @patch("forgewright.agent.opencode.subprocess.Popen")
    def test_run_success(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter(["result\n"])
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        summary_dir = tmp_path / ".claude"
        summary_dir.mkdir()
        (summary_dir / "last-run-summary.md").write_text("oc summary")

        agent = OpenCodeAgent()
        result = agent.run("do stuff", tmp_path)

        assert result.ok is True
        assert result.summary == "oc summary"

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "opencode"
        assert "--non-interactive" in cmd
        assert "--prompt" in cmd

    @patch("forgewright.agent.opencode.subprocess.Popen")
    def test_run_with_model(self, mock_popen, tmp_path):
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        (tmp_path / ".claude").mkdir()

        agent = OpenCodeAgent(model="gpt-4")
        agent.run("prompt", tmp_path)

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert "gpt-4" in cmd
