"""Tests for agent-initiated repository requests with human approval."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from forgewright.agent.base import Agent, AgentResult
from forgewright.config import Config
from forgewright.context_repos import (
    ContextRepo,
    ContextRepoSpec,
    format_context_block,
    parse_context_repos,
)
from forgewright.context_requests import (
    RepoRequest,
    apply_access_commands,
    denied_repos,
    extract_repo_requests,
    granted_specs,
    request_access,
    valid_repo_path,
)
from forgewright.handlers import process_project
from forgewright.state import State
from forgewright.types import Project
from tests.conftest import MockPlatform, make_issue, make_note, make_project

BOT = "forgewright"


class Forge(MockPlatform):
    """Mock forge with per-repo access levels and a set of repos the bot
    token can read."""

    def __init__(self, readable=("g/lib", "g/secret"), levels=None):
        super().__init__()
        self.readable = set(readable)
        self.levels = levels or {}          # (repo, user) -> level
        self.posts: list[str] = []
        self.issues, self.notes = [], {}

    def project(self, project_id):
        if project_id not in self.readable:
            raise RuntimeError("404 Project Not Found")
        return Project(id=project_id, path=str(project_id), web_url="",
                       default_branch="main", http_clone_url="")

    def user_access_level(self, project_id, user):
        return self.levels.get((project_id, user.username), 0)

    def list_issues(self, pid, after):
        return list(self.issues)

    def issue_notes(self, pid, nr):
        return list(self.notes.get(nr, []))

    def comment_issue(self, pid, nr, body):
        self.posts.append(body)
        super().comment_issue(pid, nr, body)


def post_to(forge):
    return forge.posts.append


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.json")


PROJECT = make_project(path="g/app")


# --------------------------------------------------------------------------
# parsing the agent's summary
# --------------------------------------------------------------------------

class TestExtract:
    def test_requests_are_split_off(self):
        summary = ("## General\nI did half of it.\n\n"
                   "## Request repo: g/lib\nRetry logic lives there.\n\n"
                   "## Inline: a.py:3\nnit\n\n"
                   "## Request repo: `g/spec/`\n")
        reqs, rest = extract_repo_requests(summary)
        assert reqs == [RepoRequest("g/lib", "Retry logic lives there."),
                        RepoRequest("g/spec", "")]
        assert "Request repo" not in rest
        assert "## General\nI did half of it." in rest
        assert "## Inline: a.py:3\nnit" in rest

    def test_no_requests_untouched(self):
        assert extract_repo_requests("## General\nok") == ([], "## General\nok")

    def test_duplicates_collapsed(self):
        reqs, _ = extract_repo_requests(
            "## Request repo: g/lib\na\n## Request repo: g/lib\nb\n")
        assert [r.path for r in reqs] == ["g/lib"]

    @pytest.mark.parametrize("path, ok", [
        ("g/lib", True), ("group/sub/lib.js", True), ("lib", False),
        ("../etc/passwd", False), ("g/../x", False), ("/g/lib", False),
        ("g/lib;rm -rf", False), (".hidden/x", False),
    ])
    def test_path_validation(self, path, ok):
        assert valid_repo_path(path) is ok


# --------------------------------------------------------------------------
# asking for access
# --------------------------------------------------------------------------

class TestRequestAccess:
    def test_asks_once_with_commands(self, tmp_config, state):
        forge = Forge()
        reqs = [RepoRequest("g/lib", "need the client")]
        request_access(tmp_config, forge, state, PROJECT, reqs, post_to(forge))
        request_access(tmp_config, forge, state, PROJECT, reqs, post_to(forge))
        assert len(forge.posts) == 1
        q = forge.posts[0]
        assert "`@forgewright allow repo g/lib`" in q
        assert "deny repo g/lib" in q and "need the client" in q
        assert "g/lib" in state.proj(PROJECT.id)["context_access"]["pending"]

    def test_unreadable_repo_reported_once(self, tmp_config, state):
        forge = Forge(readable=())
        for _ in range(2):
            request_access(tmp_config, forge, state, PROJECT,
                           [RepoRequest("g/hidden")], post_to(forge))
        assert len(forge.posts) == 1
        assert "no access" in forge.posts[0]
        assert denied_repos(state, PROJECT.id)["g/hidden"]["reason"] == \
            "not accessible with the bot token"

    def test_skips_self_invalid_configured_granted_denied(self, tmp_config,
                                                          state):
        forge = Forge(readable=("g/conf", "g/granted", "g/denied"))
        tmp_config.context_repos = parse_context_repos({"g/app": ["g/conf"]})
        ctx = state.proj(PROJECT.id).setdefault("context_access", {})
        ctx.update({"granted": {"g/granted": {}}, "denied": {"g/denied": {}}})
        request_access(tmp_config, forge, state, PROJECT, [
            RepoRequest("g/app"), RepoRequest("nope"), RepoRequest("g/conf"),
            RepoRequest("g/granted"), RepoRequest("g/denied")],
            post_to(forge))
        assert forge.posts == []


# --------------------------------------------------------------------------
# allow / deny / revoke
# --------------------------------------------------------------------------

def cmd(text, author="alice", note_id=1, created="2024-01-01T00:00:00Z"):
    return make_note(note_id=note_id, author=author, body=text,
                     created_at=created)


class TestCommands:
    def test_allow_with_code_access(self, tmp_config, state):
        forge = Forge(levels={("g/lib", "alice"): 20})
        n = apply_access_commands(tmp_config, forge, state, PROJECT,
                                  [cmd("@forgewright allow repo g/lib")],
                                  post_to(forge))
        assert n == 1
        assert granted_specs(state, PROJECT.id) == [ContextRepoSpec("g/lib")]
        grant = state.proj(PROJECT.id)["context_access"]["granted"]["g/lib"]
        assert grant["by"] == "alice"
        assert "now readable" in forge.posts[-1]

    def test_allow_without_code_access_refused(self, tmp_config, state):
        # GitLab guest (10) can't see code of a private project.
        forge = Forge(levels={("g/secret", "mallory"): 10})
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("@forgewright allow repo g/secret",
                                   author="mallory")], post_to(forge))
        assert granted_specs(state, PROJECT.id) == []
        assert "only someone who can read the code" in forge.posts[-1]

    def test_github_read_level_is_enough(self, tmp_config, state):
        forge = Forge(levels={("g/lib", "alice"): 10})
        forge.CODE_READ_LEVEL = 10
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("@forgewright allow repo g/lib")],
                              post_to(forge))
        assert granted_specs(state, PROJECT.id) == [ContextRepoSpec("g/lib")]

    def test_access_check_error_fails_closed(self, tmp_config, state):
        forge = Forge()
        forge.user_access_level = MagicMock(side_effect=RuntimeError("500"))
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("@forgewright allow repo g/lib")],
                              post_to(forge))
        assert granted_specs(state, PROJECT.id) == []

    def test_allow_but_bot_token_cannot_read(self, tmp_config, state):
        forge = Forge(readable=(), levels={("g/x", "alice"): 50})
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("@forgewright allow repo g/x")],
                              post_to(forge))
        assert granted_specs(state, PROJECT.id) == []
        assert "my token" in forge.posts[-1]

    def test_bot_cannot_approve_itself(self, tmp_config, state):
        # The bot's own question contains the exact command text.
        forge = Forge(levels={("g/lib", BOT): 50})
        apply_access_commands(tmp_config, forge, state, PROJECT, [cmd(
            "to continue reply `@forgewright allow repo g/lib`",
            author=BOT)], post_to(forge))
        assert granted_specs(state, PROJECT.id) == []
        assert forge.posts == []

    def test_each_note_applied_once(self, tmp_config, state):
        forge = Forge(levels={("g/lib", "alice"): 20})
        notes = [cmd("@forgewright allow repo g/lib")]
        apply_access_commands(tmp_config, forge, state, PROJECT, notes,
                              post_to(forge))
        apply_access_commands(tmp_config, forge, state, PROJECT, notes,
                              post_to(forge))
        assert len(forge.posts) == 1

    def test_deny_then_revoke(self, tmp_config, state):
        forge = Forge(levels={("g/lib", "alice"): 20})
        apply_access_commands(tmp_config, forge, state, PROJECT, [
            cmd("@forgewright allow repo g/lib", note_id=1),
            cmd("@forgewright deny repo g/lib", note_id=2,
                created="2024-01-02T00:00:00Z"),
        ], post_to(forge))
        assert granted_specs(state, PROJECT.id) == []
        assert denied_repos(state, PROJECT.id)["g/lib"]["reason"] == "declined"

        apply_access_commands(tmp_config, forge, state, PROJECT, [
            cmd("@forgewright allow repo g/lib", note_id=3,
                created="2024-01-03T00:00:00Z")], post_to(forge))
        assert granted_specs(state, PROJECT.id) == [ContextRepoSpec("g/lib")]
        assert "g/lib" not in denied_repos(state, PROJECT.id)

        apply_access_commands(tmp_config, forge, state, PROJECT, [
            cmd("@forgewright revoke repo g/lib", note_id=4,
                created="2024-01-04T00:00:00Z")], post_to(forge))
        assert granted_specs(state, PROJECT.id) == []
        assert "revoked" in forge.posts[-1]

    def test_case_and_punctuation(self, tmp_config, state):
        forge = Forge(levels={("g/lib", "alice"): 20})
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("ok, @Forgewright ALLOW repo `g/lib`.")],
                              post_to(forge))
        assert granted_specs(state, PROJECT.id) == [ContextRepoSpec("g/lib")]

    def test_invalid_path_reply(self, tmp_config, state):
        forge = Forge()
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("@forgewright allow repo ../../etc")],
                              post_to(forge))
        assert "not a repository path" in forge.posts[-1]

    def test_other_bot_name_ignored(self, tmp_config, state):
        forge = Forge(levels={("g/lib", "alice"): 50})
        apply_access_commands(tmp_config, forge, state, PROJECT,
                              [cmd("@forgewright-staging allow repo g/lib")],
                              post_to(forge))
        assert granted_specs(state, PROJECT.id) == []


# --------------------------------------------------------------------------
# prompt section
# --------------------------------------------------------------------------

class TestPromptBlock:
    def test_request_instructions(self):
        block = format_context_block([], requests_enabled=True)
        assert "## Request repo: <group/path>" in block

    def test_disabled_and_empty(self):
        assert format_context_block([], requests_enabled=False) == ""

    def test_lists_repos_and_denied(self):
        repo = ContextRepo(spec=ContextRepoSpec("g/lib"), rel_dir=".fw/x",
                           ref="main", sha="a" * 40)
        block = format_context_block(
            [repo], denied={"g/no": {"reason": "declined"}},
            requests_enabled=True)
        assert "`g/lib` @ `main`" in block
        assert "`g/no` (declined)" in block
        assert "Request repo" in block


# --------------------------------------------------------------------------
# full lifecycle through process_project
# --------------------------------------------------------------------------

class RequestingAgent(Agent):
    name = "requester"

    def __init__(self):
        self.prompts: list[str] = []

    def run(self, prompt, cwd):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return AgentResult(ok=True, output="", summary=(
                "## General\nPartial answer.\n\n"
                "## Request repo: g/lib\nThe client lives there."))
        return AgentResult(ok=True, output="", summary="Full answer.")


class TestLifecycle:
    def _poll(self, cfg, forge, agent, tmp_path, attached):
        wt = tmp_path / "wt"
        (wt / ".forgewright").mkdir(parents=True, exist_ok=True)

        def fake_attach(cfg, platform, project, wt, extra=None):
            attached.append([s.path for s in (extra or [])])
            return []

        with patch("forgewright.handlers.clone_or_update_mirror",
                   return_value=tmp_path / "mirror"), \
                patch("forgewright.handlers.make_worktree", return_value=wt), \
                patch("forgewright.handlers.cleanup_worktree"), \
                patch("forgewright.handlers.push_branch", return_value=""), \
                patch("forgewright.handlers.run"), \
                patch("forgewright.handlers.attach_context_repos",
                      side_effect=fake_attach):
            process_project(cfg, forge, agent, State(cfg.state_file),
                            make_project(path="g/app"))

    def test_request_approve_rerun(self, tmp_config, tmp_path):
        forge = Forge(levels={("g/lib", "alice"): 30})
        agent, attached = RequestingAgent(), []
        forge.issues = [make_issue(number=1, description="@forgewright help")]

        self._poll(tmp_config, forge, agent, tmp_path, attached)
        answer, question = forge.posts
        assert "Partial answer." in answer and "Request repo" not in answer
        assert "allow repo g/lib" in question
        assert attached == [[]]

        # alice approves → command applied, and the mention re-runs the agent
        forge.notes[1] = [cmd("@forgewright allow repo g/lib", note_id=7,
                              created="2024-02-01T00:00:00Z")]
        self._poll(tmp_config, forge, agent, tmp_path, attached)
        assert "now readable" in forge.posts[2]
        assert attached[-1] == ["g/lib"]
        assert "Full answer." in forge.posts[-1]

        # nothing new → no run, no new posts
        before = len(forge.posts)
        self._poll(tmp_config, forge, agent, tmp_path, attached)
        assert len(forge.posts) == before and len(agent.prompts) == 2

    def test_unauthorized_approver_filtered(self, tmp_config, tmp_path):
        # write access on the main project is required (authz), and that
        # filter runs before commands are looked at
        tmp_config.authorization_min_role = "write"
        forge = Forge(levels={("g/lib", "guest"): 50})
        forge.access_levels = {"guest": 10}
        forge.notes[1] = [cmd("@forgewright allow repo g/lib", author="guest")]
        forge.issues = [make_issue(number=1, description="plain",
                                   author="guest")]
        self._poll(tmp_config, forge, RequestingAgent(), tmp_path, [])
        assert granted_specs(State(tmp_config.state_file),
                             42) == []

    def test_disabled(self, tmp_config, tmp_path):
        tmp_config.context_requests = False
        forge = Forge(levels={("g/lib", "alice"): 30})
        agent = RequestingAgent()
        forge.issues = [make_issue(number=1, description="@forgewright help")]
        self._poll(tmp_config, forge, agent, tmp_path, [])
        assert len(forge.posts) == 1          # no question posted
        assert "Request repo" not in agent.prompts[0]


class TestConfigAndPlatform:
    def test_config_flag(self, tmp_path):
        p = tmp_path / "c.yaml"
        base = ("platform_url: https://g\nplatform_token: t\n"
                "bot_username: forgewright\n"
                f"workdir: {tmp_path}/w\nstate_file: {tmp_path}/s\n"
                f"lock_dir: {tmp_path}/l\nlog_file: {tmp_path}/b.log\n")
        p.write_text(base)
        assert Config.load(str(p)).context_requests is True
        p.write_text(base + "context_requests: false\n")
        assert Config.load(str(p)).context_requests is False
        p.write_text(base + "context_requests: maybe\n")
        with pytest.raises(SystemExit):
            Config.load(str(p))

    def test_code_read_levels(self):
        from forgewright.platform.github import GitHubPlatform
        from forgewright.platform.gitlab import GitLabPlatform
        assert GitLabPlatform.CODE_READ_LEVEL == 20     # reporter
        assert GitHubPlatform.CODE_READ_LEVEL == 10     # read
