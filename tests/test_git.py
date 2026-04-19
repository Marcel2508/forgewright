"""Tests for forgewright.git -- push_branch with real git repos."""

import subprocess
from pathlib import Path

import pytest

from forgewright.git import push_branch
from tests.conftest import MockPlatform


def _git(args: list[str], cwd: Path, **kwargs):
    """Run a git command in tests."""
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True,
        check=True, **kwargs)


@pytest.fixture
def git_repos(tmp_path, tmp_config):
    """Bare origin + mirror clone + feature-branch worktree.

    Returns (cfg, platform, worktree_path).
    """
    origin = tmp_path / "origin.git"
    mirror = tmp_path / "mirror"
    wt = tmp_path / "worktree"

    # Bootstrap a bare origin with one commit on "main".
    init_dir = tmp_path / "init"
    _git(["init", "-b", "main", str(init_dir)], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=init_dir)
    _git(["config", "user.email", "test@test.com"], cwd=init_dir)
    (init_dir / "README.md").write_text("init")
    _git(["add", "."], cwd=init_dir)
    _git(["commit", "-m", "initial"], cwd=init_dir)
    _git(["clone", "--bare", str(init_dir), str(origin)], cwd=tmp_path)

    # Clone as the bot's "mirror".
    _git(["clone", str(origin), str(mirror)], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=mirror)
    _git(["config", "user.email", "test@test.com"], cwd=mirror)

    # Create a feature-branch worktree (mimics make_worktree).
    _git(["worktree", "add", "-b", "forgewright/test-branch",
          str(wt), "main"], cwd=mirror)
    _git(["config", "user.name", "Test"], cwd=wt)
    _git(["config", "user.email", "test@test.com"], cwd=wt)

    tmp_config.workdir = tmp_path / "work"
    platform = MockPlatform()
    return tmp_config, platform, wt


class TestPushBranch:
    def test_no_commits_beyond_base_returns_empty(self, git_repos):
        """Branch forked from main with zero new commits -> don't push."""
        cfg, platform, wt = git_repos
        assert push_branch(cfg, platform, wt, "forgewright/test-branch", "main") == ""

    def test_with_commits_pushes_and_returns_sha(self, git_repos):
        """Branch with a new commit -> push and return the commit SHA."""
        cfg, platform, wt = git_repos
        (wt / "feature.py").write_text("print('hello')\n")
        _git(["add", "feature.py"], cwd=wt)
        _git(["commit", "-m", "add feature"], cwd=wt)

        result = push_branch(cfg, platform, wt, "forgewright/test-branch", "main")
        assert result  # truthy (non-empty SHA)
        assert len(result) == 40  # full SHA

        # Verify the branch landed on the remote.
        ls = _git(["ls-remote", "origin", "refs/heads/forgewright/test-branch"],
                  cwd=wt)
        assert "forgewright/test-branch" in ls.stdout

    def test_already_pushed_returns_empty(self, git_repos):
        """Same HEAD already on remote -> nothing to push."""
        cfg, platform, wt = git_repos
        (wt / "feature.py").write_text("print('hello')\n")
        _git(["add", "feature.py"], cwd=wt)
        _git(["commit", "-m", "add feature"], cwd=wt)

        assert push_branch(cfg, platform, wt, "forgewright/test-branch", "main")
        assert push_branch(cfg, platform, wt, "forgewright/test-branch", "main") == ""

    def test_new_commits_after_previous_push(self, git_repos):
        """Second commit on top of an already-pushed branch -> push again."""
        cfg, platform, wt = git_repos
        (wt / "a.py").write_text("v1\n")
        _git(["add", "a.py"], cwd=wt)
        _git(["commit", "-m", "first"], cwd=wt)
        assert push_branch(cfg, platform, wt, "forgewright/test-branch", "main")

        (wt / "b.py").write_text("v2\n")
        _git(["add", "b.py"], cwd=wt)
        _git(["commit", "-m", "second"], cwd=wt)
        assert push_branch(cfg, platform, wt, "forgewright/test-branch", "main")

    def test_rewritten_history_pushes_with_force_lease(self, git_repos):
        """Amend after push (non-fast-forward) succeeds via --force-with-lease."""
        cfg, platform, wt = git_repos
        (wt / "a.py").write_text("v1\n")
        _git(["add", "a.py"], cwd=wt)
        _git(["commit", "-m", "first"], cwd=wt)
        assert push_branch(cfg, platform, wt, "forgewright/test-branch", "main")

        # Amend the commit — rewrites history.
        (wt / "a.py").write_text("v2\n")
        _git(["add", "a.py"], cwd=wt)
        _git(["commit", "--amend", "-m", "first (amended)"], cwd=wt)

        result = push_branch(cfg, platform, wt, "forgewright/test-branch", "main")
        assert result
        assert len(result) == 40
