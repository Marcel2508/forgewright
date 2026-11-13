"""Hermes Agent (Nous Research) CLI agent implementation.

Runs ``hermes chat --oneshot --format stream-json`` headlessly in the
worktree and reads the structured event stream (``tool_use`` /
``tool_result`` / ``text`` / ``result``).

Two ways to configure the model:

* **BYOK / managed** (``hermes_base_url`` set): forgewright maintains a
  dedicated Hermes *profile* (default ``forgewright``) under the Hermes data
  root (``$HERMES_HOME`` or ``~/.hermes``) whose config points at any
  OpenAI-compatible endpoint (LiteLLM, vLLM, OpenRouter, a local server ...).
  A profile — instead of a separate ``HERMES_HOME`` — reuses the installed
  Hermes runtime; a fresh data root would re-provision its whole Python
  runtime on the first run. Hermes profiles read API keys only from their
  own ``.env``, so the key is written there (mode 0600, rewritten before
  every run) and referenced via ``model.key_env``; it never appears on the
  command line. By default persistent memory, user profiles and
  self-improvement are switched off in that profile, so text from one
  (untrusted) issue cannot plant instructions that steer later runs.
  With ``hermes_learning: true`` Hermes keeps memory and grows skills — in a
  separate profile **per project** (``<profile>-<project-slug>-<hash>``), so
  what it learns about one codebase never leaks into another.
* **Unmanaged** (no ``hermes_base_url``): uses an existing Hermes setup
  unchanged — the default profile, or ``hermes_profile`` if given.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from forgewright.agent.base import (
    FORGE_SECRET_ENV_VARS,
    Agent,
    AgentResult,
    kill_process_group,
    reap_leftovers,
)
from forgewright.agent.opencode import _clean_opencode_output
from forgewright.parsing import read_summary
from forgewright.workspace import live_log_path

# Variable (in the managed profile's .env) holding the BYOK key; the profile
# config points at it via ``model.key_env``.
HERMES_KEY_ENV = "FORGEWRIGHT_HERMES_API_KEY"
DEFAULT_PROFILE = "forgewright"

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh",
                     "max")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SESSION_LINE = re.compile(r"^\s*(session_id:|Session:)\s*\S+\s*$")


@dataclass
class HermesSettings:
    binary: str = "hermes"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    reasoning_effort: str | None = None
    context_length: int | None = None
    max_turns: int | None = None
    profile: str | None = None
    timeout_sec: int = 3600
    # Persistent memory + skill learning (managed mode only), one profile per
    # project. ``scope`` is the project the next run is for (set_scope).
    learning: bool = False
    scope: str | None = None

    @property
    def managed(self) -> bool:
        return bool(self.base_url)

    @property
    def effective_profile(self) -> str | None:
        if not self.managed:
            return self.profile or None
        base = self.profile or DEFAULT_PROFILE
        if self.learning and self.scope:
            return project_profile_name(base, self.scope)
        return base


def project_profile_name(base: str, scope: str) -> str:
    """Stable, valid Hermes profile name for one project.

    ``<base>-<slug>-<hash>``: the slug keeps it recognisable on disk, the
    short hash keeps similar paths (``a/b-c`` vs ``a-b/c``) apart. At most 64
    characters (Hermes' profile name limit).
    """
    digest = hashlib.sha256(scope.encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-") or "project"
    room = 64 - len(base) - len(digest) - 2
    slug = slug[:max(room, 0)].rstrip("-")
    name = f"{base}-{slug}-{digest}" if slug else f"{base}-{digest}"
    return name[:64]


def valid_profile_name(name: str) -> bool:
    return bool(_PROFILE_RE.match(name))


def hermes_data_root(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    root = env.get("HERMES_HOME")
    return Path(root).expanduser() if root else Path.home() / ".hermes"


def build_hermes_config(s: HermesSettings) -> dict:
    """The ``config.yaml`` forgewright writes into the managed profile."""
    model: dict = {
        "default": s.model,
        "provider": "custom",
        "base_url": s.base_url,
    }
    if s.api_key:
        model["key_env"] = HERMES_KEY_ENV
    else:
        # Local servers (vLLM, llama.cpp, ...) often need no key at all.
        model["api_key"] = ""
    if s.context_length:
        model["context_length"] = int(s.context_length)

    agent: dict = {}
    if s.reasoning_effort:
        agent["reasoning_effort"] = s.reasoning_effort
    if s.max_turns:
        agent["max_turns"] = int(s.max_turns)

    if s.learning:
        # Learn about the codebase across runs (memory, skills, post-run
        # review), with Hermes' own nudge defaults. The "user profile" stays
        # off: the bot serves many people, one blended persona of "the user"
        # would be wrong for all of them.
        memory = {"memory_enabled": True, "user_profile_enabled": False}
        skills: dict = {}
        review = {"enabled": True}
    else:
        # No state that survives a run: prompt text comes from issues/MRs
        # and must not be able to persist instructions for later runs.
        memory = {"memory_enabled": False, "user_profile_enabled": False,
                  "nudge_interval": 0}
        skills = {"creation_nudge_interval": 0}
        review = {"enabled": False}

    cfg: dict = {
        "model": model,
        "memory": memory,
        "auxiliary": {"background_review": review},
        "terminal": {"backend": "local", "cwd": "."},
    }
    if skills:
        cfg["skills"] = skills
    if agent:
        cfg["agent"] = agent
    return cfg


def _dotenv_line(key: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._~+/=:@-]+", value):
        return f"{key}={value}\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def _write_private(path: Path, text: str) -> None:
    """Write *text* to *path* readable by the owner only (no world-readable
    window: the file is created 0600 before anything is written)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def write_managed_profile(s: HermesSettings, data_root: Path) -> Path:
    """Create/refresh the managed profile and return its directory."""
    profile = s.effective_profile or DEFAULT_PROFILE
    pdir = data_root / "profiles" / profile
    pdir.mkdir(parents=True, exist_ok=True)
    try:
        pdir.chmod(0o700)
    except OSError:
        pass
    cfg_text = ("# Managed by forgewright — regenerated before every run; "
                "edit the hermes_* keys in the forgewright config instead.\n"
                + yaml.safe_dump(build_hermes_config(s), sort_keys=False))
    cfg_path = pdir / "config.yaml"
    if not cfg_path.exists() or cfg_path.read_text() != cfg_text:
        cfg_path.write_text(cfg_text)
    env_text = ("# Managed by forgewright — do not edit.\n"
                + (_dotenv_line(HERMES_KEY_ENV, s.api_key)
                   if s.api_key else ""))
    _write_private(pdir / ".env", env_text)
    return pdir


def parse_stream(lines: list[str]) -> tuple[dict | None, str]:
    """Parse ``--format stream-json`` output.

    Returns ``(result_event, transcript)``: the final ``result`` event (or
    None if Hermes never emitted one) and a human-readable transcript of the
    run for logs and failure comments.
    """
    result = None
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            if not _SESSION_LINE.match(line):
                out.append(line)
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "text" and ev.get("text"):
            out.append(str(ev["text"]).strip())
        elif etype == "tool_use":
            args = json.dumps(ev.get("input") or {}, ensure_ascii=False)
            out.append(f"→ {ev.get('name')} {args[:300]}")
        elif etype == "tool_result" and ev.get("is_error"):
            out.append(f"✗ {ev.get('name')}: "
                       f"{str(ev.get('output'))[:500]}")
        elif etype == "result":
            result = ev
            if ev.get("error"):
                out.append(f"ERROR: {ev['error']}")
    return result, "\n".join(out)


class HermesAgent(Agent):
    """Runs the Hermes Agent CLI in one-shot (headless) mode."""

    def __init__(self, settings: HermesSettings):
        self.s = settings

    @property
    def name(self) -> str:
        return "Hermes"

    def set_scope(self, scope: str) -> None:
        self.s.scope = scope
        if self.s.managed and self.s.learning:
            logging.info("AGENT [%s] learning profile for %s: %s",
                         self.name, scope, self.s.effective_profile)

    def build_command(self, prompt_file: Path) -> list[str]:
        cmd = [self.s.binary]
        profile = self.s.effective_profile
        if profile:
            cmd += ["-p", profile]
        # --oneshot: answer and exit; --yolo: no approval prompts (nobody is
        # there to answer them — like Claude's --dangerously-skip-
        # permissions); stream-json: structured events, implies quiet.
        # The prompt goes through a file, not argv: big review diffs exceed
        # Linux' 128 KiB per-argument limit (E2BIG).
        cmd += ["chat", "--oneshot", "--yolo", "--format", "stream-json",
                "--query-file", str(prompt_file)]
        if self.s.model:
            cmd += ["--model", self.s.model]
        if self.s.max_turns:
            cmd += ["--max-turns", str(int(self.s.max_turns))]
        return cmd

    def prepare_env(self) -> dict:
        """Environment for the Hermes subprocess (writes the managed profile
        as a side effect)."""
        env = os.environ.copy()
        for secret_var in FORGE_SECRET_ENV_VARS:
            env.pop(secret_var, None)
        # Never leak the BYOK key through the process environment; the
        # managed profile's .env is the only place Hermes reads it from.
        env.pop(HERMES_KEY_ENV, None)
        env.pop("HERMES_API_KEY", None)
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        if self.s.managed:
            if self.s.learning and not self.s.scope:
                logging.warning(
                    "AGENT [%s] hermes_learning is on but no project scope "
                    "was set — using the shared profile %r", self.name,
                    self.s.effective_profile)
            write_managed_profile(self.s, hermes_data_root(env))
        return env

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        env = self.prepare_env()

        live_log = live_log_path(cwd)
        live_log.parent.mkdir(parents=True, exist_ok=True)

        fd, prompt_path = tempfile.mkstemp(prefix="forgewright-prompt-",
                                           suffix=".md")
        with os.fdopen(fd, "w") as pf:
            pf.write(prompt)
        cmd = self.build_command(Path(prompt_path))

        logging.info("AGENT [%s] starting in %s (prompt %d chars, %s)",
                     self.name, cwd, len(prompt),
                     f"managed profile {self.s.effective_profile!r}"
                     if self.s.managed else "hermes' own config")
        logging.info("AGENT live output: tail -f %s", live_log)

        chunks: list[str] = []
        try:
            try:
                with open(live_log, "w") as lf:
                    proc = subprocess.Popen(
                        cmd, cwd=cwd, env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, start_new_session=True)
                    timed_out = threading.Event()

                    def _kill_on_timeout():
                        timed_out.set()
                        kill_process_group(proc)

                    timer = threading.Timer(self.s.timeout_sec,
                                            _kill_on_timeout)
                    timer.start()
                    try:
                        for line in proc.stdout:
                            chunks.append(line)
                            lf.write(line)
                            lf.flush()
                        proc.wait()
                    finally:
                        timer.cancel()
                        reap_leftovers(proc)
            except Exception as e:
                logging.error("AGENT [%s] failed to start: %s", self.name, e)
                raise
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

        result_ev, transcript = parse_stream(chunks)
        if timed_out.is_set():
            return AgentResult(
                ok=False,
                output=f"TIMEOUT after {self.s.timeout_sec}s\n{transcript}",
                summary="")

        ok = proc.returncode == 0
        if result_ev is not None and result_ev.get("exit_code") not in (0,
                                                                        None):
            ok = False
        logging.info("AGENT [%s] exit=%s", self.name, proc.returncode)

        summary = read_summary(cwd)
        if ok and not summary:
            # Models sometimes answer in their reply instead of writing
            # .forgewright/last-run-summary.md — use the final reply text.
            if result_ev is not None and result_ev.get("text"):
                summary = str(result_ev["text"]).strip()
            else:
                summary = _clean_opencode_output("\n".join(
                    ln for ln in "".join(chunks).splitlines()
                    if not _SESSION_LINE.match(ln)))
        return AgentResult(ok=ok, output=transcript or "".join(chunks),
                           summary=summary)


__all__ = ["HermesAgent", "HermesSettings", "HERMES_KEY_ENV",
           "DEFAULT_PROFILE", "REASONING_EFFORTS", "build_hermes_config",
           "parse_stream", "project_profile_name", "valid_profile_name",
           "write_managed_profile"]
