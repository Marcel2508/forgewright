"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "/etc/forgewright/config.yaml"

# Cache compiled regexes keyed by bot_username.
_mention_re_cache: dict[str, re.Pattern] = {}


def _mention_re(bot_username: str = "forgewright") -> re.Pattern:
    """Return a compiled regex that matches @<bot_username> mentions."""
    if bot_username in _mention_re_cache:
        return _mention_re_cache[bot_username]
    pattern = re.compile(
        rf"(?<![\w@])@{re.escape(bot_username)}\b", re.IGNORECASE)
    _mention_re_cache[bot_username] = pattern
    return pattern


@dataclass
class Config:
    # Platform-generic fields (preferred for new configs)
    platform_url: str
    platform_token: str
    bot_username: str
    workdir: Path
    state_file: Path
    lock_dir: Path
    log_file: Path
    claude_binary: str = "claude"
    claude_model: str | None = None
    branch_prefix: str = "forgewright/"
    default_base_branch: str | None = None  # None = use project default
    git_user_name: str = "Forgewright"
    git_user_email: str = "forgewright@example.com"
    projects_include: list[str] = field(default_factory=list)  # empty = all
    projects_exclude: list[str] = field(default_factory=list)
    # Authorization: minimum role an actor must have on the repo to interact
    # with the bot.  None / "none" disables the check (any user may trigger).
    # Accepted: read|guest, triage|reporter, write|developer|contributor,
    # maintain|maintainer, admin|owner.
    authorization_min_role: str | None = None
    claude_timeout_sec: int = 60 * 60  # 1h per run
    # How many times in a row the bot may try to fix a failing pipeline on its
    # own MR before it stops and waits for a human comment.
    max_ci_fix_attempts: int = 3
    request_timeout_sec: int = 30
    http_retries: int = 3
    # Platform/agent selection
    platform_type: str = "gitlab"  # "gitlab" | "github" | future: "forgejo"
    agent_type: str = "claude"  # "claude" | "opencode"
    opencode_binary: str = "opencode"
    opencode_model: str | None = None
    # Hermes Agent (agent_type: "hermes"). Setting hermes_base_url switches to
    # BYOK mode: forgewright manages a dedicated Hermes profile.
    hermes_binary: str = "hermes"
    hermes_model: str | None = None
    hermes_base_url: str | None = None
    hermes_api_key: str | None = None
    hermes_reasoning_effort: str | None = None
    hermes_context_length: int | None = None
    hermes_max_turns: int | None = None
    hermes_profile: str | None = None
    hermes_learning: bool = False
    # Webhook server
    webhook_enabled: bool = False
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 5000
    webhook_secret: str = ""
    webhook_debounce_sec: int = 60

    @property
    def gitlab_url(self) -> str:
        """Backward-compatible alias for platform_url."""
        return self.platform_url

    @property
    def gitlab_token(self) -> str:
        """Backward-compatible alias for platform_token."""
        return self.platform_token

    def hermes_settings(self):
        """Hermes agent settings derived from this config."""
        from forgewright.agent.hermes import HermesSettings
        return HermesSettings(
            binary=self.hermes_binary,
            model=self.hermes_model,
            base_url=self.hermes_base_url,
            api_key=self.hermes_api_key,
            reasoning_effort=self.hermes_reasoning_effort,
            context_length=self.hermes_context_length,
            max_turns=self.hermes_max_turns,
            profile=self.hermes_profile,
            timeout_sec=self.claude_timeout_sec,
            learning=self.hermes_learning,
        )

    @classmethod
    def load(cls, path: str) -> Config:
        with open(path, "r") as f:
            raw = yaml.safe_load(f)

        # Support both old (gitlab_url/gitlab_token) and new
        # (platform_url/platform_token) config keys.
        platform_url = (
            raw.get("platform_url")
            or raw.get("gitlab_url")
            or ""
        )
        if not platform_url:
            raise SystemExit(
                "platform_url (or gitlab_url) missing from config")

        # Token: config key → env var fallback
        platform_token = (
            raw.get("platform_token")
            or raw.get("gitlab_token")
            or os.environ.get("PLATFORM_TOKEN", "")
            or os.environ.get("GITLAB_TOKEN", "")
        )
        if not platform_token:
            raise SystemExit(
                "platform_token missing (config, PLATFORM_TOKEN, or "
                "GITLAB_TOKEN env)")

        for key in ("bot_username", "workdir", "state_file",
                    "lock_dir", "log_file"):
            if key not in raw:
                raise SystemExit(f"required config key missing: {key}")
        return cls(
            platform_url=platform_url.rstrip("/"),
            platform_token=platform_token,
            bot_username=raw["bot_username"],
            workdir=Path(raw["workdir"]).expanduser(),
            state_file=Path(raw["state_file"]).expanduser(),
            lock_dir=Path(raw["lock_dir"]).expanduser(),
            log_file=Path(raw["log_file"]).expanduser(),
            claude_binary=raw.get("claude_binary", "claude"),
            claude_model=raw.get("claude_model"),
            branch_prefix=raw.get("branch_prefix", "forgewright/"),
            default_base_branch=raw.get("default_base_branch"),
            git_user_name=raw.get("git_user_name", "Forgewright"),
            git_user_email=raw.get("git_user_email", "forgewright@example.com"),
            projects_include=raw.get("projects_include", []) or [],
            projects_exclude=raw.get("projects_exclude", []) or [],
            # Secure default: only users with write/developer access may
            # trigger the bot. Set "none" explicitly to allow anyone.
            authorization_min_role=raw.get("authorization_min_role", "write"),
            claude_timeout_sec=int(raw.get("claude_timeout_sec", 3600)),
            max_ci_fix_attempts=int(raw.get("max_ci_fix_attempts", 3)),
            request_timeout_sec=int(raw.get("request_timeout_sec", 30)),
            http_retries=int(raw.get("http_retries", 3)),
            platform_type=raw.get("platform_type", "gitlab"),
            agent_type=raw.get("agent_type", "claude"),
            opencode_binary=raw.get("opencode_binary", "opencode"),
            opencode_model=raw.get("opencode_model"),
            **_hermes_fields(raw),
            webhook_enabled=bool(raw.get("webhook_enabled", False)),
            webhook_host=(raw.get("webhook_host")
                          or os.environ.get("WEBHOOK_HOST")
                          or "127.0.0.1"),
            webhook_port=int(raw.get("webhook_port", 5000)),
            webhook_secret=raw.get("webhook_secret")
            or os.environ.get("WEBHOOK_SECRET", ""),
            webhook_debounce_sec=int(
                raw.get("webhook_debounce_sec", 60)),
        )

    def git_auth_env(self, token: str | None = None) -> dict:
        """Return env dict providing git credentials via GIT_ASKPASS.

        Uses an env var + helper script so the token never appears in
        git config or in error messages.

        *token* defaults to ``self.platform_token`` when not given,
        but callers should prefer passing ``platform.git_token``
        explicitly so that authentication is fully platform-controlled.
        """
        effective_token = token if token is not None else self.platform_token
        env = os.environ.copy()
        askpass = self.workdir / ".git-askpass"
        if not askpass.exists():
            self.workdir.mkdir(parents=True, exist_ok=True)
            askpass.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$FORGEWRIGHT_GIT_TOKEN\"\n")
            askpass.chmod(0o700)
        env["FORGEWRIGHT_GIT_TOKEN"] = effective_token
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env


def _optional_int(raw: dict, key: str) -> int | None:
    val = raw.get(key)
    if val in (None, ""):
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise SystemExit(f"{key} must be an integer, got {val!r}")
    if n <= 0:
        raise SystemExit(f"{key} must be positive, got {n}")
    return n


def _hermes_fields(raw: dict) -> dict:
    """Parse and validate the hermes_* config keys."""
    from forgewright.agent.hermes import REASONING_EFFORTS, valid_profile_name

    base_url = (raw.get("hermes_base_url")
                or os.environ.get("HERMES_BASE_URL") or None)
    model = raw.get("hermes_model") or None
    effort = raw.get("hermes_reasoning_effort")
    if effort is not None:
        effort = str(effort).strip().lower() or None
    if effort and effort not in REASONING_EFFORTS:
        raise SystemExit(
            f"hermes_reasoning_effort must be one of "
            f"{', '.join(REASONING_EFFORTS)} — got {effort!r}")
    if base_url and not model:
        raise SystemExit(
            "hermes_base_url is set but hermes_model is missing — a custom "
            "endpoint needs an explicit model name")
    if base_url and not str(base_url).startswith(("http://", "https://")):
        raise SystemExit(
            f"hermes_base_url must start with http:// or https:// — got "
            f"{base_url!r}")
    profile = raw.get("hermes_profile") or None
    if profile is not None:
        profile = str(profile).strip()
        if not valid_profile_name(profile):
            raise SystemExit(
                f"hermes_profile must be lowercase letters, digits, - or _ "
                f"— got {profile!r}")
    learning = raw.get("hermes_learning", False)
    if not isinstance(learning, bool):
        raise SystemExit(
            f"hermes_learning must be true or false — got {learning!r}")
    if learning and not base_url:
        raise SystemExit(
            "hermes_learning only applies to the managed profile (set "
            "hermes_base_url); with your own Hermes setup, configure memory "
            "in that profile instead")
    return {
        "hermes_binary": raw.get("hermes_binary", "hermes"),
        "hermes_model": model,
        "hermes_base_url": str(base_url).rstrip("/") if base_url else None,
        "hermes_api_key": (raw.get("hermes_api_key")
                           or os.environ.get("HERMES_API_KEY") or None),
        "hermes_reasoning_effort": effort,
        "hermes_context_length": _optional_int(raw, "hermes_context_length"),
        "hermes_max_turns": _optional_int(raw, "hermes_max_turns"),
        "hermes_profile": profile,
        "hermes_learning": learning,
    }
