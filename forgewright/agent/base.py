"""Abstract base class for AI coding agents."""

from __future__ import annotations

import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


# Forge credentials that must never reach the agent subprocess: the agent runs
# without permission prompts on prompts built from untrusted issue/MR text, so
# a prompt-injection payload must not be able to read and exfiltrate them. The
# agent never needs them (git push runs in the wrapper via GIT_ASKPASS).
FORGE_SECRET_ENV_VARS = ("PLATFORM_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN",
                         "FORGEWRIGHT_GIT_TOKEN", "WEBHOOK_SECRET")

# Linux caps a single argv string at 32 pages (MAX_ARG_STRLEN = 128 KiB).
# Prompts with a big review diff can exceed that, so agents must not pass the
# prompt as one command-line argument above this size.
MAX_ARG_BYTES = 128 * 1024 - 1


def _pgid(proc) -> int | None:
    """The agent's process-group id, or None if unknown. Only real pids > 1
    count, so a bogus value can never signal init's group (or our own)."""
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1 or pid == os.getpgrp():
        return None
    return pid


def kill_process_group(proc, sig: int = signal.SIGKILL) -> None:
    """Signal the agent and everything it spawned (test runners, dev
    servers, ...). The agent is started with ``start_new_session=True``, so
    its pid is also its process-group id. Falls back to killing just the
    process where process groups are unavailable."""
    pgid = _pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            return  # whole group already gone
        except OSError:
            pass
    if sig == signal.SIGKILL:
        try:
            proc.kill()
        except OSError:
            pass


def reap_leftovers(proc) -> None:
    """After a normal agent exit, terminate background processes it left
    running in its process group (they would outlive the deleted worktree)."""
    pgid = _pgid(proc)
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass


@dataclass
class AgentResult:
    """Result of running an AI coding agent."""
    ok: bool        # True if exit code was 0
    output: str     # Combined stdout/stderr text
    summary: str    # Contents of .claude/last-run-summary.md (may be empty)


class Agent(ABC):
    """Abstract interface for an AI coding agent."""

    @abstractmethod
    def run(self, prompt: str, cwd: Path) -> AgentResult:
        """Execute the agent with the given prompt in the given working directory.

        The agent should:
        - Read and modify files in cwd as needed
        - Create commits (but NOT push)
        - Write a summary to cwd/.claude/last-run-summary.md

        Returns an AgentResult with the outcome.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging."""
