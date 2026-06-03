"""Authorization: gate interactions by the actor's role on the repository.

When ``authorization_min_role`` is configured, only users whose access level on
the project meets the threshold can trigger the bot, and comments from users
below the threshold are stripped before any text reaches the agent prompt.  This
shrinks the prompt-injection surface to people the repo already trusts with at
least the configured role (default: write / "contributor").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from forgewright.types import Discussion, Note, User

if TYPE_CHECKING:
    from forgewright.config import Config
    from forgewright.platform.base import Platform
    from forgewright.types import Project

# Platform-neutral role names -> normalized access level (GitLab scale).
ROLE_LEVELS: dict[str, int] = {
    "none": 0,
    "read": 10, "guest": 10,
    "triage": 20, "reporter": 20,
    "write": 30, "developer": 30, "contributor": 30,
    "maintain": 40, "maintainer": 40,
    "admin": 50, "owner": 50,
}

# A predicate that answers "is this user allowed to interact with the bot?"
Authorizer = Callable[[User | None], bool]


def min_level_from_config(cfg: "Config") -> int | None:
    """Resolve the configured minimum access level, or None if disabled."""
    raw = getattr(cfg, "authorization_min_role", None)
    if not raw:
        return None
    key = str(raw).strip().lower()
    if key in ("none", "off", "disabled", "false", ""):
        return None
    if key not in ROLE_LEVELS:
        logging.warning(
            "authz: unknown authorization_min_role %r — defaulting to 'write'",
            raw)
        return ROLE_LEVELS["write"]
    return ROLE_LEVELS[key]


def make_authorizer(platform: "Platform", project: "Project",
                    cfg: "Config") -> Authorizer | None:
    """Return a cached authorizer predicate, or None when the check is off.

    The predicate fails closed: if the platform call errors (or the role cannot
    be determined) the user is treated as unauthorized.
    """
    min_level = min_level_from_config(cfg)
    if min_level is None:
        return None

    cache: dict[str, bool] = {}

    def authorized(user: User | None) -> bool:
        if not user or not user.username:
            return False
        uname = user.username
        if uname in cache:
            return cache[uname]
        try:
            level = platform.user_access_level(project.id, user)
        except Exception as e:  # noqa: BLE001 — fail closed on any error
            logging.warning(
                "authz: could not resolve role for @%s on %s: %s — denying",
                uname, project.path, e)
            cache[uname] = False
            return False
        ok = level >= min_level
        if not ok:
            logging.info(
                "authz: @%s (level %d) below required %d on %s — filtered",
                uname, level, min_level, project.path)
        cache[uname] = ok
        return ok

    return authorized


def filter_notes(notes: list[Note], authorized: Authorizer) -> list[Note]:
    """Drop human notes whose author is not authorized (keep system notes)."""
    return [n for n in notes if n.system or authorized(n.author)]


def filter_discussions(discussions: list[Discussion],
                       authorized: Authorizer) -> list[Discussion]:
    """Strip unauthorized human notes from each discussion thread."""
    out: list[Discussion] = []
    for d in discussions:
        kept = [n for n in d.notes if n.system or authorized(n.author)]
        if kept:
            out.append(Discussion(id=d.id, notes=kept))
    return out
