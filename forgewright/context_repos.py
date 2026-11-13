"""Extra repositories the agent may read for context (``context_repos``).

Configured per watched project (or ``"*"`` for all)::

    context_repos:
      group/app:
        - group/shared-lib            # default branch
        - path: group/api-spec
          ref: v2                     # branch, tag or commit
      "*":
        - group/docs

forgewright clones them with its own token (as mirrors, like the main repo)
and checks each out — detached and read-only by convention — under
``.forgewright/context/<name>/`` inside the worktree. That directory is
git-excluded, so nothing there is ever committed or pushed, and the agent
never needs the forge token to read them.

Visibility: whatever the agent reads there can end up in comments on the
watched project. Only list repos whose content may be shown to everyone who
can see that project.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from forgewright.helpers import run
from forgewright.workspace import CONTEXT_DIR, WORK_DIR, ensure_work_dir, \
    safe_name

if TYPE_CHECKING:
    from forgewright.config import Config
    from forgewright.platform.base import Platform
    from forgewright.types import Project


@dataclass(frozen=True)
class ContextRepoSpec:
    path: str
    ref: str | None = None


@dataclass
class ContextRepo:
    spec: ContextRepoSpec
    rel_dir: str = ""
    ref: str = ""
    sha: str = ""
    error: str = ""
    _mirror: Path | None = None
    _dir: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.error


def parse_context_repos(raw) -> dict[str, list[ContextRepoSpec]]:
    """Validate the ``context_repos`` config value (raises SystemExit)."""
    if raw in (None, {}, []):
        return {}
    if not isinstance(raw, dict):
        raise SystemExit("context_repos must be a mapping of project path → "
                         "list of repositories")
    out: dict[str, list[ContextRepoSpec]] = {}
    for project, items in raw.items():
        if not isinstance(items, list):
            raise SystemExit(f"context_repos[{project!r}] must be a list")
        specs = []
        for item in items:
            if isinstance(item, str) and item.strip():
                specs.append(ContextRepoSpec(path=item.strip().strip("/")))
            elif isinstance(item, dict) and isinstance(item.get("path"), str) \
                    and item["path"].strip():
                ref = item.get("ref")
                if ref is not None and not isinstance(ref, (str, int)):
                    raise SystemExit(
                        f"context_repos[{project!r}]: ref must be a string")
                specs.append(ContextRepoSpec(
                    path=item["path"].strip().strip("/"),
                    ref=str(ref) if ref is not None else None))
            else:
                raise SystemExit(
                    f"context_repos[{project!r}]: each entry must be a repo "
                    f"path or {{path: ..., ref: ...}} — got {item!r}")
        out[str(project)] = specs
    return out


def specs_for(cfg: "Config", project: "Project") -> list[ContextRepoSpec]:
    """Context repos for *project*: its own entries plus ``"*"``, deduped by
    path (project-specific entries win), never the project itself."""
    table = getattr(cfg, "context_repos", None) or {}
    seen: dict[str, ContextRepoSpec] = {}
    for key in (project.path, "*"):
        for spec in table.get(key, []):
            if spec.path == project.path or spec.path in seen:
                continue
            seen[spec.path] = spec
    return list(seen.values())


def _resolve(mirror: Path, ref: str) -> str:
    for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        r = run(["git", "rev-parse", "--verify", "--quiet",
                 f"{candidate}^{{commit}}"],
                cwd=mirror, check=False, capture=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    raise RuntimeError(f"ref {ref!r} not found")


def attach_context_repos(cfg: "Config", platform: "Platform",
                         project: "Project", wt: Path) -> list[ContextRepo]:
    """Check out the configured context repos into the worktree.

    Never raises: a repo that can't be fetched is reported (``error``) and
    shown to the agent as unavailable, the run goes on.
    """
    from forgewright.git import clone_or_update_mirror

    repos: list[ContextRepo] = []
    for spec in specs_for(cfg, project):
        repo = ContextRepo(spec=spec)
        repos.append(repo)
        try:
            ctx_project = platform.project(spec.path)
            mirror = clone_or_update_mirror(cfg, platform, ctx_project)
            ref = spec.ref or ctx_project.default_branch or "main"
            sha = _resolve(mirror, ref)
            name = safe_name(spec.path.replace("/", "-"))
            target = ensure_work_dir(wt) / CONTEXT_DIR / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "worktree", "add", "--detach", str(target), sha],
                cwd=mirror)
            repo.ref, repo.sha = ref, sha
            repo.rel_dir = f"{WORK_DIR}/{CONTEXT_DIR}/{name}"
            repo._mirror, repo._dir = mirror, target
            logging.info("context repo %s @ %s (%s) → %s",
                         spec.path, ref, sha[:12], repo.rel_dir)
        except Exception as e:  # noqa: BLE001 — never fail the main run
            repo.error = str(e) or e.__class__.__name__
            logging.warning("context repo %s unavailable for %s: %s",
                            spec.path, project.path, repo.error)
    return repos


def detach_context_repos(repos: list[ContextRepo]) -> None:
    for repo in repos:
        if repo._mirror and repo._dir:
            run(["git", "worktree", "remove", "--force", str(repo._dir)],
                cwd=repo._mirror, check=False)
            if repo._dir.exists():
                shutil.rmtree(repo._dir, ignore_errors=True)


def format_context_block(repos: list[ContextRepo]) -> str:
    """Prompt section listing the context repos ('' when there are none)."""
    if not repos:
        return ""
    lines = [
        "# Additional repositories (read-only context)",
        "These repositories are checked out inside this worktree for "
        "reference. Read them as needed (code, docs, git log). Do NOT modify "
        "them: they are excluded from git here, changes are discarded and "
        "never pushed. Your own changes belong in the main repository only.",
    ]
    for r in repos:
        if r.ok:
            lines.append(f"- `{r.spec.path}` @ `{r.ref}` ({r.sha[:12]}) → "
                         f"`{r.rel_dir}/`")
        else:
            lines.append(f"- `{r.spec.path}` — not available this run "
                         f"({r.error[:200]})")
    return "\n".join(lines) + "\n"
