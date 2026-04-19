"""Git plumbing: mirrors, worktrees, push."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from forgewright.helpers import run, slugify

if TYPE_CHECKING:
    from forgewright.config import Config
    from forgewright.platform.base import Platform
    from forgewright.types import Project


def _prune_stale_worktrees(cfg: Config, mirror: Path,
                           project_id: int | str) -> None:
    """Force-remove all worktrees left over from previous crashed runs."""
    wt_root = cfg.workdir / "worktrees" / str(project_id)
    if wt_root.exists():
        for entry in wt_root.iterdir():
            if entry.is_dir():
                run(["git", "worktree", "remove", "--force", str(entry)],
                    cwd=mirror, check=False)
                if entry.exists():
                    shutil.rmtree(entry, ignore_errors=True)
    run(["git", "worktree", "prune"], cwd=mirror, check=False)


def clone_or_update_mirror(cfg: Config, platform: Platform,
                           project: Project) -> Path:
    """Keep a --mirror bare repo per project; use worktrees per branch."""
    mirror = cfg.workdir / "mirrors" / f"{project.id}.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)

    clone_url = platform.clone_url(project)
    git_env = cfg.git_auth_env(platform.git_token)

    if not mirror.exists():
        run(["git", "clone", "--mirror", clone_url, str(mirror)], env=git_env)
        run(["git", "config", "--unset", "remote.origin.mirror"],
            cwd=mirror, check=False)
    else:
        run(["git", "remote", "set-url", "origin", clone_url], cwd=mirror)
        run(["git", "config", "--unset", "remote.origin.mirror"],
            cwd=mirror, check=False)
        _prune_stale_worktrees(cfg, mirror, project.id)
        run(["git", "remote", "update", "--prune"], cwd=mirror, env=git_env)
    return mirror


def make_worktree(cfg: Config, mirror: Path, project_id: int | str,
                  branch: str, base_branch: str) -> Path:
    wt_root = cfg.workdir / "worktrees" / str(project_id)
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / slugify(branch, 80)

    if wt_path.exists():
        run(["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=mirror, check=False)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

    remote_has = run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=mirror, check=False
    ).returncode == 0

    if remote_has:
        run(["git", "worktree", "add", str(wt_path), branch], cwd=mirror)
    else:
        run(["git", "worktree", "add", "-b", branch,
             str(wt_path), base_branch], cwd=mirror)

    run(["git", "config", "user.name", cfg.git_user_name], cwd=wt_path)
    run(["git", "config", "user.email", cfg.git_user_email], cwd=wt_path)

    (wt_path / ".claude").mkdir(exist_ok=True)

    return wt_path


def cleanup_worktree(mirror: Path, wt_path: Path) -> None:
    run(["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=mirror, check=False)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


def push_branch(cfg: Config, platform: Platform, wt_path: Path, branch: str,
                base_branch: str = "main") -> str:
    """Return the pushed commit SHA if anything was pushed, empty string otherwise."""
    git_env = cfg.git_auth_env(platform.git_token)
    local = run(["git", "rev-parse", "HEAD"],
                cwd=wt_path, capture=True).stdout.strip()

    merge_base = run(
        ["git", "merge-base", "HEAD", base_branch],
        cwd=wt_path, capture=True, check=False).stdout.strip()
    if local == merge_base:
        return ""

    ls = run(["git", "ls-remote", "origin", f"refs/heads/{branch}"],
             cwd=wt_path, capture=True, check=False, env=git_env).stdout.strip()
    remote = ls.split()[0] if ls else ""
    if local and local == remote:
        return ""
    push_cmd = ["git", "push", "origin",
                f"refs/heads/{branch}:refs/heads/{branch}"]
    if remote:
        push_cmd.insert(2, f"--force-with-lease=refs/heads/{branch}:{remote}")
    run(push_cmd, cwd=wt_path, env=git_env)
    return local
