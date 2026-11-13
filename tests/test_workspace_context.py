"""Tests for the .forgewright/ scratch dir, diff/CI files and context repos.

Most tests use real git: the point is git behaviour (excludes, worktrees,
refs), which mocks would only pretend to check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from forgewright.config import Config
from forgewright.context_repos import (
    ContextRepoSpec,
    attach_context_repos,
    detach_context_repos,
    format_context_block,
    parse_context_repos,
    specs_for,
)
from forgewright.formatting import format_review_diff, unified_diff_file
from forgewright.git import clone_or_update_mirror, make_worktree
from forgewright.handlers import _fetch_failed_job_logs, handle_mr_review
from forgewright.state import State
from forgewright.types import DiffChange, DiffRefs, Job, MRDetail, Pipeline, \
    Project
from forgewright.workspace import exclude_work_dir, summary_path
from tests.conftest import MockAgent, MockPlatform, make_mr, make_project


def git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def make_upstream(root: Path, name: str, files: dict[str, str],
                  tag: str | None = None) -> Path:
    repo = root / "upstream" / name
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    git("add", "-A", cwd=repo)
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m",
        "init", cwd=repo)
    if tag:
        git("tag", tag, cwd=repo)
        (repo / "after-tag.txt").write_text("newer\n")
        git("add", "-A", cwd=repo)
        git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
            "-m", "after tag", cwd=repo)
    return repo


def local_project(pid, path: str, upstream: Path) -> Project:
    return Project(id=pid, path=path, web_url="", default_branch="main",
                   http_clone_url=str(upstream))


class LocalForge(MockPlatform):
    """Serves projects from local upstream repos (clone_url = file path)."""

    def __init__(self, projects: dict[str, Project]):
        super().__init__()
        self.projects = projects

    def project(self, project_id):
        if project_id not in self.projects:
            raise RuntimeError("404 Project Not Found")
        return self.projects[project_id]

    def clone_url(self, project):
        return project.http_clone_url


# --------------------------------------------------------------------------
# .forgewright/ is never committed
# --------------------------------------------------------------------------

class TestScratchDirExcluded:
    def test_git_add_all_skips_scratch_dir(self, tmp_config, tmp_path):
        up = make_upstream(tmp_path, "app", {"README.md": "hi\n"})
        forge = LocalForge({})
        mirror = clone_or_update_mirror(
            tmp_config, forge, local_project(1, "g/app", up))
        wt = make_worktree(tmp_config, mirror, 1, "forgewright/x", "main")

        summary_path(wt).write_text("## General\nsummary")
        (wt / ".forgewright" / "agent-live.log").write_text("transcript")
        (wt / "feature.py").write_text("print(1)\n")
        # Claude Code's real config dir stays a normal, committable path
        (wt / ".claude").mkdir()
        (wt / ".claude" / "settings.json").write_text("{}")
        git("add", "-A", cwd=wt)
        staged = git("diff", "--cached", "--name-only", cwd=wt).splitlines()

        assert sorted(staged) == [".claude/settings.json", "feature.py"]

    def test_exclude_entry_added_once(self, tmp_path):
        mirror = tmp_path / "m.git"
        (mirror / "info").mkdir(parents=True)
        (mirror / "info" / "exclude").write_text("# existing\n*.tmp")
        exclude_work_dir(mirror)
        exclude_work_dir(mirror)
        text = (mirror / "info" / "exclude").read_text()
        assert text.count("/.forgewright/") == 1
        assert "*.tmp\n" in text   # existing content kept, newline fixed


# --------------------------------------------------------------------------
# full diff file + review prompt section
# --------------------------------------------------------------------------

def _changes(n: int, size: int) -> list[DiffChange]:
    return [DiffChange(diff=f"@@ -1 +1 @@\n-old{i}\n+" + "n" * size,
                       old_path=f"src/f{i}.py", new_path=f"src/f{i}.py")
            for i in range(n)]


class TestReviewDiff:
    def test_diff_file_is_complete(self):
        changes = _changes(50, 5000)          # 250 KB, way over any budget
        text = unified_diff_file(changes)
        for i in range(50):
            assert f"diff --git a/src/f{i}.py b/src/f{i}.py" in text
        assert text.count("+++ b/") == 50

    def test_new_deleted_renamed_headers(self):
        text = unified_diff_file([
            DiffChange(diff="+x", old_path="a.py", new_path="a.py",
                       new_file=True),
            DiffChange(diff="-y", old_path="b.py", new_path="b.py",
                       deleted_file=True),
            DiffChange(diff="", old_path="old.py", new_path="new.py",
                       renamed_file=True),
        ])
        assert "--- /dev/null\n+++ b/a.py" in text
        assert "--- a/b.py\n+++ /dev/null" in text
        assert "rename from old.py\nrename to new.py" in text

    def test_prompt_section_overview_pointers_and_excerpt(self):
        changes = _changes(50, 5000)
        block = format_review_diff(changes, ".forgewright/mr.diff",
                                   base_sha="b" * 40, head_sha="h" * 40,
                                   inline_max_chars=20000)
        assert "## Changed files (50)" in block
        assert "- `src/f49.py` (+1 −1)" in block       # overview lists all
        assert ".forgewright/mr.diff" in block
        assert f"git diff {'b' * 40}...{'h' * 40}" in block
        assert "excerpt ends here" in block
        assert len(block) < 30000                       # budget respected

    def test_review_handler_writes_diff_file(self, tmp_config, tmp_path):
        wt = tmp_path / "wt"
        (wt / ".forgewright").mkdir(parents=True)
        forge = MockPlatform()
        changes = _changes(30, 4000)
        forge.mr_changes = lambda pid, nr: MRDetail(
            number=nr, diff_refs=DiffRefs(base_sha="b1", head_sha="h1"),
            changes=changes)
        prompts = []

        class A(MockAgent):
            def run(self, prompt, cwd):
                prompts.append(prompt)
                return super().run(prompt, cwd)

        with patch("forgewright.handlers.clone_or_update_mirror",
                   return_value=tmp_path / "mirror"), \
                patch("forgewright.handlers.make_worktree", return_value=wt), \
                patch("forgewright.handlers.cleanup_worktree"), \
                patch("forgewright.handlers.push_branch", return_value=""):
            handle_mr_review(tmp_config, forge, A(summary="## General\nok"),
                             State(tmp_path / "s.json"), make_project(),
                             make_mr(description="@forgewright review"),
                             [], [], None, "first-seen")
        diff_file = wt / ".forgewright" / "mr.diff"
        assert diff_file.read_text().count("diff --git") == 30
        assert "git diff b1...h1" in prompts[0]
        assert ".forgewright/mr.diff" in prompts[0]


# --------------------------------------------------------------------------
# full CI logs as files
# --------------------------------------------------------------------------

class TestCiLogFiles:
    def _forge(self, log: str):
        forge = MockPlatform()
        forge.pipeline_jobs = lambda pid, pl: [
            Job(id=7, name="unit tests/py3.12", stage="test",
                status="failed")]
        forge.job_log = lambda pid, jid: log
        return forge

    def test_full_log_written_tail_in_prompt(self, tmp_path):
        log = "".join(f"line {i}\n" for i in range(20000))   # ~200 KB
        block = _fetch_failed_job_logs(
            self._forge(log), 1, Pipeline(id=3, sha="s", status="failed",
                                          web_url=""), tmp_path)
        files = list((tmp_path / ".forgewright" / "ci").iterdir())
        assert len(files) == 1
        assert files[0].read_text() == log                   # complete
        assert files[0].name == "7-unit-tests-py3.12.log"    # safe name
        assert f".forgewright/ci/{files[0].name}" in block
        assert "line 19999" in block and "line 0\n" not in block

    def test_huge_log_capped(self, tmp_path):
        with patch("forgewright.handlers.MAX_LOG_FILE_BYTES", 1000):
            _fetch_failed_job_logs(
                self._forge("x" * 5000 + "END"), 1,
                Pipeline(id=3, sha="s", status="failed", web_url=""),
                tmp_path)
        data = next((tmp_path / ".forgewright" / "ci").iterdir()).read_text()
        assert data.startswith("...(log start cut)")
        assert data.endswith("END") and len(data) < 1100

    def test_without_worktree_unchanged(self):
        block = _fetch_failed_job_logs(
            self._forge("boom"), 1,
            Pipeline(id=3, sha="s", status="failed", web_url=""))
        assert "boom" in block and "Full log" not in block


# --------------------------------------------------------------------------
# context_repos
# --------------------------------------------------------------------------

class TestContextReposConfig:
    def test_parse(self):
        parsed = parse_context_repos({
            "g/app": ["g/lib", {"path": "g/spec/", "ref": "v2"}],
            "*": [{"path": "g/docs", "ref": 1}],
        })
        assert parsed["g/app"] == [ContextRepoSpec("g/lib"),
                                   ContextRepoSpec("g/spec", "v2")]
        assert parsed["*"] == [ContextRepoSpec("g/docs", "1")]

    @pytest.mark.parametrize("raw", [
        ["g/lib"], {"g/app": "g/lib"}, {"g/app": [{"ref": "x"}]},
        {"g/app": [""]}, {"g/app": [{"path": "g/x", "ref": ["a"]}]},
    ])
    def test_invalid(self, raw):
        with pytest.raises(SystemExit):
            parse_context_repos(raw)

    def test_specs_for_merges_wildcard_and_skips_self(self, tmp_config):
        tmp_config.context_repos = parse_context_repos({
            "g/app": ["g/lib", {"path": "g/shared", "ref": "v2"}],
            "*": ["g/shared", "g/docs", "g/app"],
        })
        specs = specs_for(tmp_config, make_project(path="g/app"))
        assert [(s.path, s.ref) for s in specs] == [
            ("g/lib", None), ("g/shared", "v2"), ("g/docs", None)]
        other = specs_for(tmp_config, make_project(path="g/other"))
        assert [s.path for s in other] == ["g/shared", "g/docs", "g/app"]

    def test_loaded_from_yaml(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(
            "platform_url: https://g\nplatform_token: t\n"
            "bot_username: forgewright\n"
            f"workdir: {tmp_path}/w\nstate_file: {tmp_path}/s\n"
            f"lock_dir: {tmp_path}/l\nlog_file: {tmp_path}/b.log\n"
            "context_repos:\n  g/app:\n    - g/lib\n")
        assert Config.load(str(p)).context_repos == {
            "g/app": [ContextRepoSpec("g/lib")]}


class TestContextReposCheckout:
    def _setup(self, tmp_config, tmp_path):
        app = make_upstream(tmp_path, "app", {"main.py": "import lib\n"})
        lib = make_upstream(tmp_path, "lib", {"lib.py": "VERSION = 2\n"},
                            tag="v2")
        projects = {
            "g/app": local_project(1, "g/app", app),
            "g/lib": local_project(2, "g/lib", lib),
        }
        forge = LocalForge(projects)
        mirror = clone_or_update_mirror(tmp_config, forge, projects["g/app"])
        wt = make_worktree(tmp_config, mirror, 1, "forgewright/x", "main")
        return forge, projects, wt, lib

    def test_checkout_ref_readable_and_never_committed(self, tmp_config,
                                                       tmp_path):
        forge, projects, wt, lib = self._setup(tmp_config, tmp_path)
        tmp_config.context_repos = parse_context_repos({"g/app": [
            {"path": "g/lib", "ref": "v2"}, "g/missing"]})

        repos = attach_context_repos(tmp_config, forge, projects["g/app"], wt)

        ok, missing = repos
        assert ok.ok and ok.ref == "v2"
        assert ok.sha == git("rev-parse", "v2^{commit}", cwd=lib)
        ctx = wt / ok.rel_dir
        assert (ctx / "lib.py").read_text() == "VERSION = 2\n"
        assert not (ctx / "after-tag.txt").exists()     # pinned to the tag
        assert not missing.ok and "404" in missing.error

        block = format_context_block(repos)
        assert "`g/lib` @ `v2`" in block and ok.rel_dir in block
        assert "`g/missing` — not available" in block

        # the agent edits and runs `git add -A` in the main repo
        (ctx / "lib.py").write_text("hacked\n")
        (wt / "change.py").write_text("x\n")
        git("add", "-A", cwd=wt)
        assert git("diff", "--cached", "--name-only", cwd=wt) == "change.py"

        detach_context_repos(repos)
        assert not ctx.exists()
        lib_mirror = tmp_config.workdir / "mirrors" / "2.git"
        assert str(ctx) not in git("worktree", "list", cwd=lib_mirror)

    def test_default_branch_when_no_ref(self, tmp_config, tmp_path):
        forge, projects, wt, lib = self._setup(tmp_config, tmp_path)
        tmp_config.context_repos = parse_context_repos({"*": ["g/lib"]})
        (repo,) = attach_context_repos(tmp_config, forge, projects["g/app"],
                                       wt)
        assert repo.ref == "main"
        assert (wt / repo.rel_dir / "after-tag.txt").exists()

    def test_bad_ref_is_reported_not_raised(self, tmp_config, tmp_path):
        forge, projects, wt, _ = self._setup(tmp_config, tmp_path)
        tmp_config.context_repos = parse_context_repos({"g/app": [
            {"path": "g/lib", "ref": "no-such-branch"}]})
        (repo,) = attach_context_repos(tmp_config, forge, projects["g/app"],
                                       wt)
        assert not repo.ok and "no-such-branch" in repo.error

    def test_no_config_no_block(self, tmp_config):
        assert specs_for(tmp_config, make_project()) == []
        assert format_context_block([]) == ""


class TestGitLabPathEncoding:
    def test_project_path_is_url_encoded(self):
        from unittest.mock import MagicMock
        from forgewright.platform.gitlab import GitLabPlatform
        plat = GitLabPlatform("https://git.example.com", "t")
        resp = MagicMock()
        resp.json.return_value = {"id": 9, "path_with_namespace": "g/sub/x",
                                  "default_branch": "main"}
        with patch.object(plat, "_req", return_value=resp) as req:
            plat.project("g/sub/x")
            plat.project(42)
        assert req.call_args_list[0][0][1] == "/projects/g%2Fsub%2Fx"
        assert req.call_args_list[1][0][1] == "/projects/42"


class TestEndToEndPush:
    def test_pushed_branch_contains_only_real_changes(self, tmp_config,
                                                      tmp_path):
        """Real mirror, worktree, context repo and push: the agent stages
        everything with `git add -A`, yet the remote branch only gets its
        actual code change — no summary, live log, diff or context repo."""
        from forgewright.agent.base import Agent, AgentResult
        from forgewright.handlers import handle_issue
        from forgewright.parsing import read_summary
        from tests.conftest import make_issue

        app = make_upstream(tmp_path, "app", {"main.py": "print(1)\n"})
        lib = make_upstream(tmp_path, "lib", {"lib.py": "X = 1\n"})
        projects = {"g/app": local_project(1, "g/app", app),
                    "g/lib": local_project(2, "g/lib", lib)}
        forge = LocalForge(projects)
        tmp_config.context_repos = parse_context_repos({"g/app": ["g/lib"]})

        class CommittingAgent(Agent):
            name = "committer"

            def run(self, prompt, cwd):
                assert "`g/lib` @ `main`" in prompt
                (cwd / "feature.py").write_text("print(2)\n")
                summary_path(cwd).write_text("## General\nAdded feature")
                git("add", "-A", cwd=cwd)
                git("-c", "user.email=a@b", "-c", "user.name=a", "commit",
                    "-q", "-m", "feat: add feature", cwd=cwd)
                return AgentResult(ok=True, output="",
                                   summary=read_summary(cwd))

        handle_issue(tmp_config, forge, CommittingAgent(),
                     State(tmp_path / "s.json"), projects["g/app"],
                     make_issue(number=4, title="Add feature",
                                description="@forgewright add it"), [])

        branch = "forgewright/issue-4-add-feature"
        files = git("ls-tree", "-r", "--name-only", branch, cwd=app)
        assert sorted(files.splitlines()) == ["feature.py", "main.py"]
        assert any(c[0] == "create_mr" for c in forge.calls)
