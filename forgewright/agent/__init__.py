"""AI coding agent abstraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forgewright.agent.base import Agent, AgentResult

if TYPE_CHECKING:
    from forgewright.config import Config

__all__ = ["Agent", "AgentResult", "create_agent"]


def create_agent(cfg: Config) -> Agent:
    """Create an Agent instance based on config."""
    atype = cfg.agent_type
    if atype == "claude":
        from forgewright.agent.claude_code import ClaudeCodeAgent

        return ClaudeCodeAgent(
            binary=cfg.claude_binary,
            model=cfg.claude_model,
            timeout_sec=cfg.claude_timeout_sec,
        )
    if atype == "hermes":
        from forgewright.agent.hermes import HermesAgent

        return HermesAgent(cfg.hermes_settings())
    if atype == "opencode":
        import logging

        from forgewright.agent.opencode import OpenCodeAgent

        logging.warning(
            "agent_type 'opencode' is deprecated — consider 'hermes' "
            "(Hermes Agent, any OpenAI-compatible endpoint)")

        return OpenCodeAgent(
            binary=cfg.opencode_binary,
            model=cfg.opencode_model,
            timeout_sec=cfg.claude_timeout_sec,
        )
    raise ValueError(
        f"unknown agent_type: {atype!r} (expected claude, hermes or opencode)")
