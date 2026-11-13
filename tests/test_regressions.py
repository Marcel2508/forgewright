"""Cross-cutting regression tests.

These guard invariants that broke (or nearly broke) before and are easy to
break again: prompt rendering, the argv size limit, secret hygiene for every
agent, multi-poll trigger lifecycles, and drift between the code and the
deployment files / example config.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from forgewright.agent.base import FORGE_SECRET_ENV_VARS
from forgewright.agent.claude_code import ClaudeCodeAgent
from forgewright.agent.hermes import HermesAgent, HermesSettings
from forgewright.agent.opencode import OpenCodeAgent
from forgewright.config import Config
from forgewright.handlers import (
    handle_issue,
    handle_mr,
    handle_mr_review,
    process_project,
)
from forgewright.platform.github import GitHubPlatform
from forgewright.platform.gitlab import GitLabPlatform
from forgewright.state import State
from forgewright.types import Discussion, Pipeline
from tests.conftest import (
    MockAgent, MockPlatform, make_issue, make_mr, make_note, make_project,
)

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ("claude", "hermes", "opencode")
ARGV_LIMIT = 128 * 1024
BIG_PROMPT = "please review\n" + "y" * 300_000
UNFILLED = re.compile(r"(?<!\{)\{[a-z_]+\}(?!\})")


@pytest.fixture(autouse=True)
def _isolated_hermes_root(tmp_path, monkeypatch):
    """The managed Hermes profile must never land in the real ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-root"))


class RecordingAgent(MockAgent):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts: list[str] = []

    def run(self, prompt, cwd):
        self.prompts.append(prompt)
        return super().run(prompt, cwd)


def _git_patches(tmp_path, push_returns=""):
    wt = tmp_path / "wt"
    (wt / ".forgewright").mkdir(parents=True, exist_ok=True)
    return [
        patch("forgewright.handlers.clone_or_update_mirror",
              return_value=tmp_path / "mirror"),
        patch("forgewright.handlers.make_worktree", return_value=wt),
        patch("forgewright.handlers.cleanup_worktree"),
        patch("forgewright.handlers.push_branch", return_value=push_returns),
        patch("forgewright.handlers.run"),
    ]


class _Patched:
    def __init__(self, tmp_path, push_returns=""):
        self.patches = _git_patches(tmp_path, push_returns)

    def __enter__(self):
        self.mocks = [p.start() for p in self.patches]
        return self.mocks

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


# --------------------------------------------------------------------------
# Prompts actually rendered by the handlers
# --------------------------------------------------------------------------

class TestRenderedPrompts:
    def _check(self, prompt: str):
        assert not UNFILLED.findall(prompt), UNFILLED.findall(prompt)
        # The mirror worktree has no origin/* refs and no fetch credentials.
        assert "origin/" not in prompt.replace("origin/*", "")
        assert "git fetch origin" not in prompt
        assert ".forgewright/last-run-summary.md" in prompt

    def test_issue_prompt(self, tmp_config, mock_platform, tmp_path):
        agent = RecordingAgent()
        with _Patched(tmp_path):
            handle_issue(tmp_config, mock_platform, agent,
                         State(tmp_path / "s.json"), make_project(),
                         make_issue(description="@forgewright {weird} text"),
                         [make_note(body="curly {braces} in a comment")])
        self._check(agent.prompts[0].replace("{weird}", "").replace(
            "{braces}", ""))
        assert "{weird}" in agent.prompts[0]  # user text kept verbatim

    def test_mr_update_prompt(self, tmp_config, mock_platform, tmp_path):
        agent = RecordingAgent(summary="## General\nok")
        mr = make_mr(source_branch="forgewright/issue-1-x",
                     author="forgewright", description="Closes #1")
        with _Patched(tmp_path):
            handle_mr(tmp_config, mock_platform, agent,
                      State(tmp_path / "s.json"), make_project(), mr,
                      [Discussion(id="d1", notes=[make_note(body="fix it")])],
                      [Pipeline(id=1, sha="s", status="failed", web_url="")],
                      None, "first-seen, pipeline failed")
        self._check(agent.prompts[0])

    def test_review_prompt(self, tmp_config, mock_platform, tmp_path):
        agent = RecordingAgent(summary="## General\nok")
        mr = make_mr(description="@forgewright review")
        with _Patched(tmp_path):
            handle_mr_review(tmp_config, mock_platform, agent,
                             State(tmp_path / "s.json"), make_project(), mr,
                             [], [], None, "first-seen")
        self._check(agent.prompts[0])


# --------------------------------------------------------------------------
# Every agent: argv limit + forge secrets
# --------------------------------------------------------------------------

def _make_agent(kind: str, tmp_path: Path):
    if kind == "claude":
        return ClaudeCodeAgent(timeout_sec=30), "forgewright.agent.claude_code"
    if kind == "hermes":
        return (HermesAgent(HermesSettings(
            model="m", base_url="https://x/v1", api_key="k",
            timeout_sec=30)),
            "forgewright.agent.hermes")
    return OpenCodeAgent(timeout_sec=30), "forgewright.agent.opencode"


def _proc():
    proc = MagicMock()
    proc.pid = None
    proc.stdout = iter(["ok\n"])
    proc.returncode = 0
    proc.wait.return_value = 0
    return proc


@pytest.mark.parametrize("kind", AGENTS)
def test_big_prompt_never_hits_argv_limit(kind, tmp_path):
    agent, mod = _make_agent(kind, tmp_path)
    with patch(f"{mod}.subprocess.Popen", return_value=_proc()) as popen:
        res = agent.run(BIG_PROMPT, tmp_path)
    if popen.called:
        cmd = popen.call_args[0][0]
        assert all(len(c.encode()) < ARGV_LIMIT for c in cmd), kind
    else:
        # OpenCode can only take the prompt as an argument: it must refuse
        # cleanly instead of crashing with E2BIG.
        assert kind == "opencode"
        assert not res.ok
        assert "limit" in res.output


@pytest.mark.parametrize("kind", AGENTS)
def test_forge_secrets_never_reach_agent(kind, tmp_path, monkeypatch):
    for var in FORGE_SECRET_ENV_VARS:
        monkeypatch.setenv(var, "super-secret-forge-token")
    agent, mod = _make_agent(kind, tmp_path)
    with patch(f"{mod}.subprocess.Popen", return_value=_proc()) as popen:
        agent.run("small prompt", tmp_path)
    env = popen.call_args[1]["env"]
    assert "super-secret-forge-token" not in env.values()


@pytest.mark.parametrize("kind", AGENTS)
def test_agent_runs_in_own_process_group(kind, tmp_path):
    agent, mod = _make_agent(kind, tmp_path)
    with patch(f"{mod}.subprocess.Popen", return_value=_proc()) as popen:
        agent.run("small prompt", tmp_path)
    assert popen.call_args[1]["start_new_session"] is True
    assert popen.call_args[1]["cwd"] == tmp_path


# --------------------------------------------------------------------------
# Multi-poll lifecycles through process_project (state persisted)
# --------------------------------------------------------------------------

class _Forge(MockPlatform):
    """Mutable fake forge: tests edit issues/notes/MRs between polls."""

    def __init__(self):
        super().__init__()
        self.issues: list = []
        self.notes: dict[int, list] = {}
        self.mrs: list = []
        self.discussions: dict[int, list] = {}
        self.pipelines: dict[int, list] = {}

    def list_issues(self, pid, after):
        return list(self.issues)

    def issue_notes(self, pid, nr):
        return list(self.notes.get(nr, []))

    def list_mrs(self, pid, after):
        return list(self.mrs)

    def mr_discussions(self, pid, nr):
        return list(self.discussions.get(nr, []))

    def mr_pipelines(self, pid, nr):
        return list(self.pipelines.get(nr, []))


def _poll(cfg, forge, agent, tmp_path, push_returns=""):
    state = State(cfg.state_file)   # fresh load each poll, like the timer
    with _Patched(tmp_path, push_returns):
        process_project(cfg, forge, agent, state, make_project())
    return len(agent.prompts)


class TestIssueLifecycle:
    def test_only_new_mentions_rerun(self, tmp_config, tmp_path):
        forge, agent = _Forge(), RecordingAgent(summary="answer")
        forge.issues = [make_issue(number=1, description="@forgewright hi")]

        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # first run
        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # unchanged

        forge.notes[1] = [make_note(note_id=10, body="thanks!",
                                    created_at="2024-02-01T00:00:00Z")]
        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # plain reply

        forge.issues = [make_issue(number=1, description="@forgewright hi",
                                   labels=["in-progress"])]
        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # board move

        forge.notes[1].append(make_note(
            note_id=11, body="@forgewright now do X",
            created_at="2024-02-02T00:00:00Z"))
        assert _poll(tmp_config, forge, agent, tmp_path) == 2   # new mention
        assert _poll(tmp_config, forge, agent, tmp_path) == 2   # settled

    def test_unauthorized_mention_ignored(self, tmp_config, tmp_path):
        tmp_config.authorization_min_role = "write"
        forge, agent = _Forge(), RecordingAgent()
        forge.access_levels = {"guest-user": 10}
        forge.issues = [make_issue(number=2, description="plain",
                                   author="guest-user")]
        forge.notes[2] = [make_note(author="guest-user",
                                    body="@forgewright rm -rf everything")]
        assert _poll(tmp_config, forge, agent, tmp_path) == 0

    def test_bot_comment_never_triggers(self, tmp_config, tmp_path):
        forge, agent = _Forge(), RecordingAgent()
        forge.issues = [make_issue(number=3, description="plain")]
        forge.notes[3] = [make_note(author="forgewright",
                                    body="@forgewright self-ping")]
        assert _poll(tmp_config, forge, agent, tmp_path) == 0


class TestBotMrCiLifecycle:
    def test_retries_new_failures_until_cap(self, tmp_config, tmp_path):
        tmp_config.max_ci_fix_attempts = 2
        forge, agent = _Forge(), RecordingAgent(summary="## General\nfixed")
        forge.mrs = [make_mr(number=5, source_branch="forgewright/issue-1-x",
                             author="forgewright", sha="s1")]

        def fail(pid):
            forge.pipelines[5] = [Pipeline(id=pid, sha=f"s{pid}",
                                           status="failed", web_url="")]

        fail(1)
        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # attempt 1
        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # same pipe
        fail(2)
        assert _poll(tmp_config, forge, agent, tmp_path) == 2   # attempt 2
        fail(3)
        assert _poll(tmp_config, forge, agent, tmp_path) == 2   # capped

        forge.discussions[5] = [Discussion(id="d", notes=[make_note(
            note_id=50, body="please look again",
            created_at="2024-03-01T00:00:00Z")])]
        assert _poll(tmp_config, forge, agent, tmp_path) == 3   # human reset
        fail(4)
        assert _poll(tmp_config, forge, agent, tmp_path) == 4   # budget back

    def test_success_pipeline_does_not_run(self, tmp_config, tmp_path):
        forge, agent = _Forge(), RecordingAgent()
        forge.mrs = [make_mr(number=6, source_branch="forgewright/issue-2-y",
                             author="forgewright")]
        forge.pipelines[6] = [Pipeline(id=1, sha="a", status="success",
                                       web_url="")]
        assert _poll(tmp_config, forge, agent, tmp_path) == 0


class TestReviewLifecycle:
    def test_review_once_then_only_on_request(self, tmp_config, tmp_path):
        forge, agent = _Forge(), RecordingAgent(summary="## General\nLGTM")
        mention = make_note(note_id=1, body="@forgewright review please",
                            created_at="2024-01-01T00:00:00Z")
        forge.mrs = [make_mr(number=7, source_branch="feature", sha="a")]
        forge.discussions[7] = [Discussion(id="d1", notes=[mention])]

        assert _poll(tmp_config, forge, agent, tmp_path) == 1
        forge.mrs = [make_mr(number=7, source_branch="feature", sha="b")]
        assert _poll(tmp_config, forge, agent, tmp_path) == 1   # new push

        forge.discussions[7].append(Discussion(id="d2", notes=[make_note(
            note_id=2, author="bob", body="unrelated chatter",
            created_at="2024-01-02T00:00:00Z")]))
        assert _poll(tmp_config, forge, agent, tmp_path) == 1

        forge.discussions[7].append(Discussion(id="d3", notes=[make_note(
            note_id=3, body="@forgewright and now?",
            created_at="2024-01-03T00:00:00Z")]))
        assert _poll(tmp_config, forge, agent, tmp_path) == 2


# --------------------------------------------------------------------------
# Drift between code, example config and deployment files
# --------------------------------------------------------------------------

class TestDrift:
    def test_platforms_implement_whole_interface(self):
        # Instantiating fails if any abstract Platform method is missing.
        GitLabPlatform("https://git.example.com", "t")
        GitHubPlatform("https://api.github.com", "t")

    def test_example_config_loads(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_TOKEN", "t")
        cfg = Config.load(str(ROOT / "config.example.yaml"))
        assert cfg.agent_type in AGENTS
        assert cfg.authorization_min_role == "write"

    def test_example_config_documents_every_key(self):
        src = (ROOT / "forgewright" / "config.py").read_text()
        keys = set(re.findall(r'raw\.get\(\s*"([a-z_]+)"', src))
        keys |= set(re.findall(r'raw\["([a-z_]+)"\]', src))
        keys |= set(re.findall(r'_optional_int\(raw, "([a-z_]+)"\)', src))
        keys -= {"gitlab_url", "gitlab_token"}   # legacy aliases
        example = yaml.safe_load((ROOT / "config.example.yaml").read_text())
        assert sorted(keys - set(example)) == []
        assert sorted(set(example) - keys) == []

    @pytest.mark.parametrize("agent", AGENTS)
    def test_every_agent_is_deployable(self, agent):
        dockerfile = ROOT / f"Dockerfile.{agent}"
        assert dockerfile.exists()
        text = dockerfile.read_text()
        assert "COPY forgewright/ /opt/forgewright/forgewright/" in text
        assert "forgewright-entrypoint" in text

        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        for svc in (f"poller-{agent}", f"webhook-{agent}"):
            assert svc in compose["services"], svc
            assert agent in compose["services"][svc]["profiles"]
        assert (f"forgewright-{agent}:latest"
                in compose["services"][f"poller-{agent}"]["image"])

        wf = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "docker.yml").read_text())
        matrix = next(iter(wf["jobs"].values()))["strategy"]["matrix"]
        assert agent in matrix["agent"]

        assert agent in (ROOT / "install.docker.sh").read_text()

    def test_hermes_profiles_persist_in_docker(self):
        # With hermes_learning on, what Hermes learns lives in its profiles;
        # without a volume an image update (pull_policy: always) wipes it.
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        for svc in ("poller-hermes", "webhook-hermes"):
            vols = compose["services"][svc]["volumes"]
            assert any(str(v).endswith(":/home/forgewright/.hermes/profiles")
                       for v in vols), svc
        assert "mkdir -p /home/forgewright/.hermes/profiles" in \
            (ROOT / "Dockerfile.hermes").read_text()

    def test_process_project_sets_agent_scope(self, tmp_config, tmp_path):
        forge = _Forge()
        agent = RecordingAgent()
        agent.set_scope = MagicMock()
        _poll(tmp_config, forge, agent, tmp_path)
        agent.set_scope.assert_called_once_with(make_project().path)

    def test_webhook_services_bind_all_interfaces(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        for name, svc in compose["services"].items():
            if name.startswith("webhook-"):
                assert "0.0.0.0" in str(svc["environment"]["WEBHOOK_HOST"])
                assert svc["environment"].get("WEBHOOK_SECRET") is not None
