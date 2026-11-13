"""Agent-initiated access to other repositories, approved by a human.

Stage 2 of ``context_repos``: when the agent needs a repository that is not
configured, it asks for it in its summary::

    ## Request repo: group/other-repo
    The client library's retry logic lives there.

forgewright strips that section from what it posts, checks that its own token
can read the repository, and asks in the issue/MR. A human answers with a
comment::

    @forgewright allow repo group/other-repo
    @forgewright deny repo group/other-repo
    @forgewright revoke repo group/other-repo

Decisions are stored per watched project in the state file and apply to all
later runs of that project (allowed repos are checked out read-only like
configured ``context_repos``). The approval comment is itself a mention, so
the agent picks the task up again with the repository available.

Security: whatever the agent reads can end up in comments on the watched
project. So only a user who can read the requested repository's code
(``Platform.CODE_READ_LEVEL`` on it) may allow it — the bot's token often sees
more than the people in the MR, and must not become a way around that.
Commands only come from notes that passed the authorization filter.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from forgewright.context_repos import ContextRepoSpec, specs_for

if TYPE_CHECKING:
    from forgewright.config import Config
    from forgewright.platform.base import Platform
    from forgewright.state import State
    from forgewright.types import Note, Project

_REPO_PATH_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")
_REQUEST_RE = re.compile(
    r"^##[ \t]+Request repo:[ \t]*(?P<path>[^\s]+)[ \t]*\n?"
    r"(?P<body>.*?)(?=^##[ \t]|\Z)",
    re.MULTILINE | re.DOTALL)
_MAX_SEEN = 2000

Poster = Callable[[str], None]


@dataclass
class RepoRequest:
    path: str
    reason: str = ""


def valid_repo_path(path: str) -> bool:
    return bool(_REPO_PATH_RE.fullmatch(path)) and \
        ".." not in path.split("/") and not path.startswith(".")


def _command_re(bot_username: str) -> re.Pattern:
    return re.compile(
        rf"(?<![\w@])@{re.escape(bot_username)}\b[ \t]+"
        r"(?P<verb>allow|deny|revoke)[ \t]+repo[ \t]+`?(?P<path>[^\s`'\"]+)",
        re.IGNORECASE)


def extract_repo_requests(summary: str) -> tuple[list[RepoRequest], str]:
    """Split ``## Request repo:`` sections off the agent's summary."""
    if not summary or "Request repo:" not in summary:
        return [], summary
    requests: list[RepoRequest] = []
    for m in _REQUEST_RE.finditer(summary):
        path = m.group("path").strip().strip("`").strip("/")
        if not any(r.path == path for r in requests):
            requests.append(RepoRequest(path=path,
                                        reason=m.group("body").strip()))
    rest = _REQUEST_RE.sub("", summary).strip()
    return requests, rest


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def _ctx(state: "State", pid) -> dict:
    proj = state.proj(pid)
    ctx = proj.setdefault("context_access", {})
    for key in ("granted", "denied", "pending", "unavailable"):
        ctx.setdefault(key, {})
    ctx.setdefault("seen_notes", [])
    return ctx


def granted_specs(state: "State", pid) -> list[ContextRepoSpec]:
    return [ContextRepoSpec(path=p) for p in sorted(_ctx(state, pid)["granted"])]


def denied_repos(state: "State", pid) -> dict[str, dict]:
    """Repos the agent must not ask for again: declined by a human, or not
    readable with the bot's token (``reason`` says which)."""
    ctx = _ctx(state, pid)
    out = {p: {**v, "reason": v.get("reason") or "declined"}
           for p, v in ctx["unavailable"].items()}
    out.update({p: {**v, "reason": "declined"} for p, v in ctx["denied"].items()})
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _note_key(note: "Note") -> str:
    # GitHub issue and review comments come from separate id spaces.
    return f"{note.type or 'note'}:{note.id}"


# --------------------------------------------------------------------------
# after an agent run: ask for requested repositories
# --------------------------------------------------------------------------

def request_access(cfg: "Config", platform: "Platform", state: "State",
                   project: "Project", requests: list[RepoRequest],
                   post: Poster) -> None:
    """Post one approval question per new, plausible request."""
    if not requests:
        return
    ctx = _ctx(state, project.id)
    configured = {s.path for s in specs_for(cfg, project)}
    bot = cfg.bot_username
    for req in requests:
        path = req.path
        if not valid_repo_path(path) or path == project.path:
            logging.info("repo request %r ignored (invalid or self)", path)
            continue
        if path in configured or path in ctx["granted"]:
            continue          # already available — nothing to ask
        if path in ctx["denied"] or path in ctx["pending"] \
                or path in ctx["unavailable"]:
            continue          # decided, already asked, or known unreadable
        try:
            platform.project(path)
        except Exception as e:  # noqa: BLE001
            logging.info("repo request %s: not readable with bot token: %s",
                         path, e)
            post(f"\U0001f916 forgewright: I'd like to read `{path}` for this "
                 f"task, but my token has no access to it (or it doesn't "
                 f"exist). Grant the bot user read access, or add it to "
                 f"`context_repos`, if it should be available.")
            ctx["unavailable"][path] = {
                "at": _now(), "reason": "not accessible with the bot token"}
            state.save()
            continue
        reason = f"\n\n> {req.reason[:500]}" if req.reason else ""
        post(f"\U0001f916 forgewright: to work on this I'd like to **read** "
             f"`{path}` (read-only, nothing is pushed there).{reason}\n\n"
             f"Someone who can read `{path}` can allow it for this project "
             f"by replying:\n\n"
             f"`@{bot} allow repo {path}`\n\n"
             f"or decline with `@{bot} deny repo {path}`. Note that content "
             f"from it may then appear in comments here.")
        ctx["pending"][path] = {"at": _now(), "reason": req.reason[:500]}
        state.save()


# --------------------------------------------------------------------------
# before deciding on a run: apply allow / deny / revoke comments
# --------------------------------------------------------------------------

def apply_access_commands(cfg: "Config", platform: "Platform",
                          state: "State", project: "Project",
                          notes: "list[Note]", post: Poster) -> int:
    """Apply new ``@bot allow|deny|revoke repo X`` comments. Returns how many
    commands were applied (each note is handled once)."""
    from forgewright.helpers import parse_ts

    bot = cfg.bot_username
    if not any(f"@{bot}".lower() in (n.body or "").lower() and
               "repo" in (n.body or "").lower() for n in notes):
        return 0
    rx = _command_re(bot)
    ctx = _ctx(state, project.id)
    seen = set(ctx["seen_notes"])
    applied = 0
    for note in sorted(notes, key=lambda n: (parse_ts(n.created_at),
                                             str(n.id))):
        if note.system or note.author.username == bot:
            continue
        key = _note_key(note)
        if key in seen:
            continue
        commands = list(rx.finditer(note.body or ""))
        if not commands:
            continue
        seen.add(key)
        ctx["seen_notes"].append(key)
        who = note.author.username
        for m in commands:
            verb = m.group("verb").lower()
            path = m.group("path").strip().strip("`").strip("/").rstrip(".,;:")
            if not valid_repo_path(path) or path == project.path:
                post(f"\U0001f916 forgewright: `{path}` is not a repository "
                     f"path I can use (expected `group/name`).")
                continue
            if verb == "allow":
                applied += _allow(platform, ctx, path, note, post)
            elif verb == "deny":
                ctx["granted"].pop(path, None)
                ctx["pending"].pop(path, None)
                ctx["denied"][path] = {"by": who, "at": _now()}
                post(f"\U0001f916 forgewright: OK @{who}, I won't read "
                     f"`{path}` for this project.")
                applied += 1
            else:  # revoke
                had = ctx["granted"].pop(path, None) is not None
                ctx["pending"].pop(path, None)
                post(f"\U0001f916 forgewright: access to `{path}` "
                     f"{'revoked' if had else 'was not granted'} "
                     f"(@{who}).")
                applied += 1
    if len(ctx["seen_notes"]) > _MAX_SEEN:
        ctx["seen_notes"] = ctx["seen_notes"][-_MAX_SEEN:]
    state.save()
    return applied


def _allow(platform: "Platform", ctx: dict, path: str, note: "Note",
           post: Poster) -> int:
    who = note.author.username
    need = getattr(platform, "CODE_READ_LEVEL", 20)
    try:
        level = platform.user_access_level(path, note.author)
    except Exception as e:  # noqa: BLE001 — fail closed
        logging.warning("repo grant %s by @%s: access check failed: %s",
                        path, who, e)
        level = 0
    if level < need:
        post(f"\U0001f916 forgewright: @{who}, only someone who can read the "
             f"code of `{path}` can allow it — I couldn't confirm that you "
             f"can. Nothing changed.")
        return 0
    try:
        platform.project(path)
    except Exception:  # noqa: BLE001
        post(f"\U0001f916 forgewright: allowed by @{who}, but my token "
             f"can't read `{path}` — grant the bot user access first.")
        return 0
    ctx["denied"].pop(path, None)
    ctx["pending"].pop(path, None)
    ctx["unavailable"].pop(path, None)
    ctx["granted"][path] = {"by": who, "at": _now(),
                            "note": _note_key(note)}
    post(f"\U0001f916 forgewright: thanks @{who} — `{path}` is now readable "
         f"for this project (read-only). Revoke any time with "
         f"`revoke repo {path}`.")
    return 1
