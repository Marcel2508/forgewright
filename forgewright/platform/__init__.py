"""Code hosting platform abstraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forgewright.platform.base import Platform, ProjectID

if TYPE_CHECKING:
    from forgewright.config import Config

__all__ = ["Platform", "ProjectID", "create_platform"]


def create_platform(cfg: Config) -> Platform:
    """Create a Platform instance based on config."""
    ptype = cfg.platform_type
    if ptype == "gitlab":
        from forgewright.platform.gitlab import GitLabPlatform

        return GitLabPlatform(
            base_url=cfg.platform_url,
            token=cfg.platform_token,
            request_timeout=cfg.request_timeout_sec,
            http_retries=cfg.http_retries,
        )
    if ptype == "github":
        from forgewright.platform.github import GitHubPlatform

        return GitHubPlatform(
            base_url=cfg.platform_url,
            token=cfg.platform_token,
            request_timeout=cfg.request_timeout_sec,
            http_retries=cfg.http_retries,
        )
    raise ValueError(f"unknown platform_type: {ptype!r}")
