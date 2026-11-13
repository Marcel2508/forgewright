"""Tests for the Hermes Agent integration (agent wrapper + config)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from forgewright.agent import create_agent
from forgewright.agent.base import FORGE_SECRET_ENV_VARS
from forgewright.agent.hermes import (
    HERMES_KEY_ENV,
    HermesAgent,
    HermesSettings,
    build_hermes_config,
    parse_stream,
    project_profile_name,
    valid_profile_name,
)
from forgewright.config import Config


@pytest.fixture(autouse=True)
def hermes_root(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.hermes."""
    root = tmp_path / "hermes-root"
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_BASE_URL", raising=False)
    monkeypatch.delenv(HERMES_KEY_ENV, raising=False)
    return root


def _settings(**kw) -> HermesSettings:
    base = dict(model="qwen3-coder", base_url="https://llm.example.com/v1",
                api_key="sk-secret-123", reasoning_effort="high",
                context_length=131072, max_turns=40, timeout_sec=60)
    base.update(kw)
    return HermesSettings(**base)


def _ev(**kw) -> str:
    return json.dumps(kw) + "\n"


def _fake_proc(lines=(), returncode=0):
    proc = MagicMock()
    proc.pid = None  # never a real pid → process-group helpers no-op
    proc.stdout = iter(lines)
    proc.wait.return_value = returncode
    proc.returncode = returncode
    return proc


def _ok_stream(text="done"):
    return [_ev(type="system", subtype="init"),
            "Session: local-abc\n",
            _ev(type="text", text=text),
            _ev(type="result", exit_code=0, text=text),
            "\n", "session_id: local-abc\n"]


# --------------------------------------------------------------------------
# managed profile
# --------------------------------------------------------------------------

class TestManagedProfile:
    def test_byok_endpoint_model_and_tuning(self):
        cfg = build_hermes_config(_settings())
        assert cfg["model"] == {
            "default": "qwen3-coder",
            "provider": "custom",
            "base_url": "https://llm.example.com/v1",
            "key_env": HERMES_KEY_ENV,
            "context_length": 131072,
        }
        assert cfg["agent"] == {"reasoning_effort": "high", "max_turns": 40}

    def test_no_persistent_learning_between_runs(self):
        cfg = build_hermes_config(_settings())
        assert cfg["memory"]["memory_enabled"] is False
        assert cfg["memory"]["user_profile_enabled"] is False
        assert cfg["skills"]["creation_nudge_interval"] == 0
        assert cfg["auxiliary"]["background_review"]["enabled"] is False

    def test_optional_tuning_omitted(self):
        cfg = build_hermes_config(_settings(
            reasoning_effort=None, context_length=None, max_turns=None))
        assert "agent" not in cfg
        assert "context_length" not in cfg["model"]

    def test_keyless_local_server(self, hermes_root):
        HermesAgent(_settings(api_key=None)).prepare_env()
        pdir = hermes_root / "profiles" / "forgewright"
        cfg = yaml.safe_load((pdir / "config.yaml").read_text())
        assert cfg["model"]["api_key"] == ""
        assert "key_env" not in cfg["model"]
        assert HERMES_KEY_ENV not in (pdir / ".env").read_text()

    def test_profile_files(self, hermes_root):
        HermesAgent(_settings()).prepare_env()
        pdir = hermes_root / "profiles" / "forgewright"
        cfg_text = (pdir / "config.yaml").read_text()
        assert "sk-secret-123" not in cfg_text          # key not in config
        assert yaml.safe_load(cfg_text)["model"]["key_env"] == HERMES_KEY_ENV
        env_file = pdir / ".env"
        assert f"{HERMES_KEY_ENV}=sk-secret-123" in env_file.read_text()
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert pdir.stat().st_mode & 0o777 == 0o700

    def test_key_with_special_chars_is_quoted(self, hermes_root):
        HermesAgent(_settings(api_key='a b"c\\d')).prepare_env()
        text = (hermes_root / "profiles" / "forgewright" / ".env").read_text()
        assert f'{HERMES_KEY_ENV}="a b\\"c\\\\d"' in text

    def test_key_rotation_rewrites_env(self, hermes_root):
        HermesAgent(_settings(api_key="old")).prepare_env()
        HermesAgent(_settings(api_key="new")).prepare_env()
        text = (hermes_root / "profiles" / "forgewright" / ".env").read_text()
        assert "new" in text and "old" not in text

    def test_custom_profile_name(self, hermes_root):
        HermesAgent(_settings(profile="bot-a")).prepare_env()
        assert (hermes_root / "profiles" / "bot-a" / "config.yaml").exists()

    def test_unmanaged_writes_nothing(self, hermes_root):
        agent = HermesAgent(HermesSettings())
        agent.prepare_env()
        assert not hermes_root.exists()
        assert "-p" not in agent.build_command(Path("p"))

    def test_unmanaged_existing_profile(self, hermes_root):
        agent = HermesAgent(HermesSettings(profile="mine"))
        agent.prepare_env()
        assert not hermes_root.exists()          # user's profile untouched
        cmd = agent.build_command(Path("p"))
        assert cmd[cmd.index("-p") + 1] == "mine"


# --------------------------------------------------------------------------
# command line + environment
# --------------------------------------------------------------------------

class TestCommandAndEnv:
    def test_headless_flags(self, tmp_path):
        cmd = HermesAgent(_settings()).build_command(tmp_path / "p.md")
        assert cmd[:3] == ["hermes", "-p", "forgewright"]
        assert cmd[3] == "chat"
        for flag in ("--oneshot", "--yolo"):
            assert flag in cmd
        assert cmd[cmd.index("--format") + 1] == "stream-json"
        assert cmd[cmd.index("--query-file") + 1] == str(tmp_path / "p.md")
        assert cmd[cmd.index("--model") + 1] == "qwen3-coder"
        assert cmd[cmd.index("--max-turns") + 1] == "40"

    def test_api_key_not_on_command_line(self, tmp_path):
        cmd = HermesAgent(_settings()).build_command(tmp_path / "p.md")
        assert not any("sk-secret-123" in c for c in cmd)
        assert "--api-key" not in cmd

    def test_secrets_stripped_from_env(self, monkeypatch):
        for var in FORGE_SECRET_ENV_VARS + ("HERMES_API_KEY",):
            monkeypatch.setenv(var, "secret")
        env = HermesAgent(_settings()).prepare_env()
        for var in FORGE_SECRET_ENV_VARS + ("HERMES_API_KEY", HERMES_KEY_ENV):
            assert var not in env


# --------------------------------------------------------------------------
# stream parsing
# --------------------------------------------------------------------------

class TestParseStream:
    def test_result_and_transcript(self):
        lines = [
            _ev(type="system", subtype="init"),
            "Session: local-1\n",
            _ev(type="tool_use", name="terminal", input={"command": "ls"}),
            _ev(type="tool_result", name="terminal", output="x",
                is_error=False),
            _ev(type="tool_result", name="patch", output="no match",
                is_error=True),
            _ev(type="text", text="All done."),
            _ev(type="result", exit_code=0, text="All done."),
            "session_id: local-1\n",
        ]
        result, transcript = parse_stream(lines)
        assert result["text"] == "All done."
        assert '→ terminal {"command": "ls"}' in transcript
        assert "✗ patch: no match" in transcript
        assert "All done." in transcript
        assert "session_id" not in transcript
        assert "Session:" not in transcript

    def test_no_result_event(self):
        result, transcript = parse_stream(["Traceback: boom\n"])
        assert result is None
        assert "boom" in transcript

    def test_error_result(self):
        result, transcript = parse_stream(
            [_ev(type="result", exit_code=1, error="No API key")])
        assert result["exit_code"] == 1
        assert "ERROR: No API key" in transcript


# --------------------------------------------------------------------------
# run()
# --------------------------------------------------------------------------

class TestRun:
    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_prompt_via_file_and_cleaned_up(self, mock_popen, tmp_path):
        seen = {}

        def popen(cmd, **kw):
            path = Path(cmd[cmd.index("--query-file") + 1])
            seen["path"] = path
            seen["content"] = path.read_text()
            seen["mode"] = path.stat().st_mode & 0o777
            seen["cwd"] = kw["cwd"]
            seen["new_session"] = kw.get("start_new_session")
            return _fake_proc(_ok_stream())

        mock_popen.side_effect = popen
        big = "review this\n" + "x" * 300_000   # > 128 KiB argv limit
        res = HermesAgent(_settings()).run(big, tmp_path)

        assert res.ok
        assert seen["content"] == big
        assert seen["mode"] == 0o600
        assert seen["cwd"] == tmp_path
        assert seen["new_session"] is True
        assert not seen["path"].exists()   # temp prompt file removed
        cmd = mock_popen.call_args[0][0]
        assert all(len(c.encode()) < 128 * 1024 for c in cmd)

    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_summary_file_wins(self, mock_popen, tmp_path):
        mock_popen.return_value = _fake_proc(_ok_stream("reply text"))
        (tmp_path / ".forgewright").mkdir()
        (tmp_path / ".forgewright" / "last-run-summary.md").write_text(
            "## General\nok")
        res = HermesAgent(_settings()).run("p", tmp_path)
        assert res.summary == "## General\nok"

    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_falls_back_to_final_reply(self, mock_popen, tmp_path):
        mock_popen.return_value = _fake_proc(_ok_stream("The answer is 42."))
        res = HermesAgent(_settings()).run("p", tmp_path)
        assert res.summary == "The answer is 42."
        assert "session_id" not in res.output

    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_result_exit_code_overrides_process_exit(self, mock_popen,
                                                     tmp_path):
        # Hermes printed an error result (e.g. missing key) — not a success,
        # and the error text must not be posted as the bot's answer.
        mock_popen.return_value = _fake_proc([
            _ev(type="result", exit_code=1, text="⚠️ No API key",
                error="No API key for the custom endpoint")], returncode=0)
        res = HermesAgent(_settings()).run("p", tmp_path)
        assert not res.ok
        assert res.summary == ""
        assert "No API key" in res.output

    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_nonzero_exit_is_failure(self, mock_popen, tmp_path):
        mock_popen.return_value = _fake_proc(["boom\n"], returncode=2)
        res = HermesAgent(_settings()).run("p", tmp_path)
        assert not res.ok
        assert res.summary == ""
        assert "boom" in res.output

    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_live_log_written(self, mock_popen, tmp_path):
        mock_popen.return_value = _fake_proc(["a\n", "b\n"])
        HermesAgent(_settings()).run("p", tmp_path)
        assert (tmp_path / ".forgewright" / "agent-live.log").read_text() == \
            "a\nb\n"

    @patch("forgewright.agent.hermes.subprocess.Popen")
    def test_timeout(self, mock_popen, tmp_path):
        proc = _fake_proc([_ev(type="text", text="partial")])
        mock_popen.return_value = proc

        class InstantTimer:
            def __init__(self, _t, fn):
                self.fn = fn

            def start(self):
                self.fn()

            def cancel(self):
                pass

        with patch("forgewright.agent.hermes.threading.Timer", InstantTimer):
            res = HermesAgent(_settings()).run("p", tmp_path)
        assert not res.ok
        assert res.output.startswith("TIMEOUT")
        assert "partial" in res.output
        proc.kill.assert_called_once()   # fallback when no process group

    @patch("forgewright.agent.hermes.subprocess.Popen",
           side_effect=FileNotFoundError("hermes"))
    def test_missing_binary_raises_and_cleans_up(self, _p, tmp_path):
        before = set(os.listdir(tempfile.gettempdir()))
        with pytest.raises(FileNotFoundError):
            HermesAgent(_settings()).run("p", tmp_path)
        after = set(os.listdir(tempfile.gettempdir()))
        assert not [f for f in after - before
                    if f.startswith("forgewright-prompt-")]


# --------------------------------------------------------------------------
# config loading + factory
# --------------------------------------------------------------------------

def _cfg_file(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(
        "platform_url: https://git.example.com\n"
        "platform_token: tok\n"
        "bot_username: forgewright\n"
        f"workdir: {tmp_path / 'w'}\n"
        f"state_file: {tmp_path / 's.json'}\n"
        f"lock_dir: {tmp_path / 'l'}\n"
        f"log_file: {tmp_path / 'b.log'}\n" + extra)
    return str(p)


class TestConfig:
    def test_defaults(self, tmp_path):
        c = Config.load(_cfg_file(tmp_path))
        assert c.hermes_binary == "hermes"
        assert c.hermes_base_url is None
        s = c.hermes_settings()
        assert s.managed is False
        assert s.effective_profile is None

    def test_full_byok(self, tmp_path):
        c = Config.load(_cfg_file(tmp_path, (
            "agent_type: hermes\n"
            "hermes_base_url: https://llm.example.com/v1/\n"
            "hermes_model: qwen3-coder\n"
            "hermes_api_key: sk-x\n"
            "hermes_reasoning_effort: HIGH\n"
            "hermes_context_length: 131072\n"
            "hermes_max_turns: 50\n"
            "hermes_profile: team-bot\n")))
        s = c.hermes_settings()
        assert s.managed
        assert s.base_url == "https://llm.example.com/v1"  # trailing / cut
        assert s.reasoning_effort == "high"
        assert (s.context_length, s.max_turns) == (131072, 50)
        assert s.effective_profile == "team-bot"
        assert s.timeout_sec == c.claude_timeout_sec

    def test_managed_default_profile(self, tmp_path):
        c = Config.load(_cfg_file(tmp_path, (
            "hermes_base_url: https://x/v1\nhermes_model: m\n")))
        assert c.hermes_settings().effective_profile == "forgewright"

    def test_key_and_url_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_API_KEY", "sk-env")
        monkeypatch.setenv("HERMES_BASE_URL", "http://vllm:8000/v1")
        c = Config.load(_cfg_file(tmp_path, "hermes_model: m\n"))
        assert c.hermes_api_key == "sk-env"
        assert c.hermes_base_url == "http://vllm:8000/v1"

    @pytest.mark.parametrize("extra, msg", [
        ("hermes_reasoning_effort: turbo\n", "hermes_reasoning_effort"),
        ("hermes_base_url: https://x/v1\n", "hermes_model"),
        ("hermes_base_url: ftp://x\nhermes_model: m\n", "http"),
        ("hermes_context_length: lots\n", "integer"),
        ("hermes_max_turns: 0\n", "positive"),
        ("hermes_profile: ../etc\n", "hermes_profile"),
        ("hermes_profile: Bad Name\n", "hermes_profile"),
    ])
    def test_validation(self, tmp_path, extra, msg):
        with pytest.raises(SystemExit) as e:
            Config.load(_cfg_file(tmp_path, extra))
        assert msg in str(e.value)

    def test_factory_builds_hermes(self, tmp_path):
        c = Config.load(_cfg_file(tmp_path, (
            "agent_type: hermes\nhermes_base_url: https://x/v1\n"
            "hermes_model: m\n")))
        agent = create_agent(c)
        assert isinstance(agent, HermesAgent)
        assert agent.name == "Hermes"
        assert agent.s.model == "m"

    def test_opencode_logs_deprecation(self, tmp_path, caplog):
        c = Config.load(_cfg_file(tmp_path, "agent_type: opencode\n"))
        with caplog.at_level("WARNING"):
            create_agent(c)
        assert "deprecated" in caplog.text

    def test_unknown_agent_type(self, tmp_path):
        c = Config.load(_cfg_file(tmp_path, "agent_type: gpt-cli\n"))
        with pytest.raises(ValueError, match="hermes"):
            create_agent(c)


# --------------------------------------------------------------------------
# optional learning (per-project profiles)
# --------------------------------------------------------------------------


class TestLearning:
    def test_off_by_default_is_stateless(self):
        cfg = build_hermes_config(_settings())
        assert cfg["memory"]["memory_enabled"] is False
        assert cfg["auxiliary"]["background_review"]["enabled"] is False

    def test_on_enables_memory_skills_and_review(self):
        cfg = build_hermes_config(_settings(learning=True))
        assert cfg["memory"]["memory_enabled"] is True
        assert cfg["auxiliary"]["background_review"]["enabled"] is True
        # Hermes' own nudge defaults apply — we don't zero them out
        assert "nudge_interval" not in cfg["memory"]
        assert "skills" not in cfg
        # one blended "user" persona would be wrong for a multi-user bot
        assert cfg["memory"]["user_profile_enabled"] is False

    def test_one_profile_per_project(self):
        agent = HermesAgent(_settings(learning=True))
        agent.set_scope("group/app")
        a = agent.s.effective_profile
        agent.set_scope("group/other")
        b = agent.s.effective_profile
        assert a != b
        assert a.startswith("forgewright-group-app-")
        assert valid_profile_name(a) and valid_profile_name(b)

    def test_profile_name_is_stable_and_collision_safe(self):
        assert project_profile_name("fw", "a/b") == \
            project_profile_name("fw", "a/b")
        # same slug, different path → different profile
        assert project_profile_name("fw", "a/b-c") != \
            project_profile_name("fw", "a-b/c")

    def test_long_paths_stay_valid(self):
        name = project_profile_name(
            "forgewright", "very/" * 30 + "deep/project_name")
        assert len(name) <= 64
        assert valid_profile_name(name)

    def test_learning_off_ignores_scope(self):
        agent = HermesAgent(_settings())
        agent.set_scope("group/app")
        assert agent.s.effective_profile == "forgewright"

    def test_learning_profile_written_per_project(self, hermes_root):
        agent = HermesAgent(_settings(learning=True))
        for project in ("group/app", "group/other"):
            agent.set_scope(project)
            agent.prepare_env()
        dirs = sorted(p.name for p in (hermes_root / "profiles").iterdir())
        assert len(dirs) == 2
        for d in dirs:
            cfg = yaml.safe_load(
                (hermes_root / "profiles" / d / "config.yaml").read_text())
            assert cfg["memory"]["memory_enabled"] is True

    def test_learning_without_scope_warns(self, caplog):
        with caplog.at_level("WARNING"):
            HermesAgent(_settings(learning=True)).prepare_env()
        assert "no project scope" in caplog.text

    def test_command_uses_project_profile(self, tmp_path):
        agent = HermesAgent(_settings(learning=True))
        agent.set_scope("group/app")
        cmd = agent.build_command(tmp_path / "p.md")
        assert cmd[cmd.index("-p") + 1].startswith("forgewright-group-app-")

    def test_config_flag(self, tmp_path):
        c = Config.load(_cfg_file(tmp_path, (
            "hermes_base_url: https://x/v1\nhermes_model: m\n"
            "hermes_learning: true\n")))
        assert c.hermes_settings().learning is True

    @pytest.mark.parametrize("extra, msg", [
        ("hermes_learning: true\n", "hermes_base_url"),
        ("hermes_base_url: https://x/v1\nhermes_model: m\n"
         "hermes_learning: yes please\n", "true or false"),
    ])
    def test_config_validation(self, tmp_path, extra, msg):
        with pytest.raises(SystemExit) as e:
            Config.load(_cfg_file(tmp_path, extra))
        assert msg in str(e.value)
