"""Tests for the .forgewright/ scratch dir.

These use real git: the point is git behaviour (excludes, worktrees), which
mocks would only pretend to check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from forgewright.git import clone_or_update_mirror, make_worktree
from forgewright.types import Project
from forgewright.workspace import exclude_work_dir, summary_path
from tests.conftest import MockPlatform


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
