"""Entry point: CLI parsing, logging setup, and orchestration."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from forgewright.agent import create_agent
from forgewright.config import DEFAULT_CONFIG_PATH, Config
from forgewright.decision import select_projects
from forgewright.handlers import process_project
from forgewright.helpers import file_lock
from forgewright.platform import create_platform
from forgewright.state import State


def setup_logging(path: Path, verbose: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(path)]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="forgewright \u2014 code forge bot-mention dispatcher")
    ap.add_argument("--config", default=os.environ.get(
        "FORGEWRIGHT_CONFIG", DEFAULT_CONFIG_PATH))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print which issues/MRs would be processed, do nothing")
    ap.add_argument("--serve", action="store_true",
                    help="start the webhook HTTP server (long-running)")
    ap.add_argument("--project", action="append", default=None,
                    help="limit to given project path (repeatable)")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    setup_logging(cfg.log_file, args.verbose)

    if args.serve:
        from forgewright.webhook import run_server
        if not cfg.webhook_enabled:
            logging.error("webhook_enabled is false in config — "
                          "set webhook_enabled: true to use --serve")
            return 1
        run_server(cfg)
        return 0

    cfg.lock_dir.mkdir(parents=True, exist_ok=True)
    global_lock = cfg.lock_dir / "poller.lock"
    with file_lock(global_lock) as got:
        if not got:
            logging.info("another poller is already running \u2014 exit")
            return 0

        platform = create_platform(cfg)
        agent = create_agent(cfg)

        try:
            me = platform.current_user()
            logging.info("authenticated as @%s (expected @%s)",
                         me.username, cfg.bot_username)
            if me.username != cfg.bot_username:
                logging.warning(
                    "token belongs to @%s, not configured @%s \u2014 "
                    "using @%s for self-filtering",
                    me.username, cfg.bot_username, me.username)
                cfg.bot_username = me.username
        except Exception as e:
            logging.error("cannot auth to platform: %s", e)
            return 2

        state = State(cfg.state_file)
        projects = select_projects(platform, cfg)
        if args.project:
            wanted = set(args.project)
            projects = [p for p in projects if p.path in wanted]
        logging.info("watching %d project(s), agent: %s",
                     len(projects), agent.name)

        if args.dry_run:
            for p in projects:
                logging.info("would scan %s (id=%s)", p.path, p.id)
            return 0

        for p in projects:
            try:
                process_project(cfg, platform, agent, state, p)
            except Exception as e:
                logging.exception("project %s crashed: %s", p.path, e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
