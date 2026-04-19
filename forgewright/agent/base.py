"""Abstract base class for AI coding agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


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
