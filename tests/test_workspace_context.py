"""Tests for the .forgewright/ scratch dir and the diff/CI files in it.

Most tests use real git: the point is git behaviour (excludes, worktrees),
which mocks would only pretend to check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

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
