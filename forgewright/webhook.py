"""Webhook HTTP server for receiving platform events in near-real-time."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from flask import Flask, Request, request, jsonify

from forgewright.agent import create_agent
from forgewright.decision import select_projects
from forgewright.handlers import process_project
from forgewright.helpers import file_lock
from forgewright.platform import create_platform
from forgewright.state import State

if TYPE_CHECKING:
    from forgewright.config import Config


def _process_event(cfg: Config, project_id: int | str,
                   event_type: str) -> None:
    """Process a webhook event in a background thread."""
    lock_path = cfg.lock_dir / "poller.lock"
    with file_lock(lock_path) as got:
        if not got:
            logging.info("webhook: poller lock busy, skipping event")
            return

        platform = create_platform(cfg)
        agent = create_agent(cfg)
        state = State(cfg.state_file)

        allowed = select_projects(platform, cfg)
        allowed_ids = {p.id for p in allowed}

        if project_id not in allowed_ids:
            logging.info("webhook: project %s not in watched projects",
                         project_id)
            return

        project = platform.project(project_id)
        logging.info(
            "webhook: processing %s (event=%s)",
            project.path,
            event_type,
        )
        try:
            process_project(cfg, platform, agent, state, project)
        except Exception as e:
            logging.exception(
                "webhook: project %s crashed: %s", project.path, e)


class _DebounceManager:
    """Coalesces rapid webhook events per project into a single processing run.

    Each call to ``schedule`` resets the timer for the given project.  When the
    timer expires (after *delay* seconds with no new events), the callback fires
    in a background thread.  Setting *delay* to ``0`` disables debouncing and
    fires immediately (old behaviour).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timers: dict[int | str, threading.Timer] = {}

    def schedule(self, key: int | str, delay: float,
                 callback, args=()) -> None:
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
                logging.info(
                    "webhook: debounce reset for project %s", key)

            if delay <= 0:
                thread = threading.Thread(
                    target=callback, args=args, daemon=True,
                    name=f"webhook-{key}")
                thread.start()
            else:
                timer = threading.Timer(delay, callback, args=args)
                timer.daemon = True
                timer.name = f"webhook-debounce-{key}"
                self._timers[key] = timer
                timer.start()

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    def pending(self) -> dict[int | str, threading.Timer]:
        with self._lock:
            return dict(self._timers)


def create_app(cfg: Config) -> Flask:
    """Create and configure the Flask webhook application."""
    app = Flask("forgewright-webhook")

    webhook_platform = create_platform(cfg)
    debounce = _DebounceManager()
    app.debounce = debounce  # type: ignore[attr-defined]

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

    @app.route("/webhook", methods=["POST"])
    def webhook():
        raw_body = request.get_data()

        if not webhook_platform.validate_webhook(
                request.headers, raw_body, cfg.webhook_secret):
            logging.warning("webhook: invalid secret token from %s",
                            request.remote_addr)
            return jsonify({"error": "unauthorized"}), 401

        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({"error": "invalid JSON"}), 400

        event_type, project_id, _ = (
            webhook_platform.parse_webhook_event(request.headers, payload))

        if not project_id:
            logging.warning("webhook: no project id in payload")
            return jsonify({"error": "no project id"}), 400

        project_name = (
            (payload.get("project") or {}).get("path_with_namespace")
            or (payload.get("repository") or {}).get("full_name")
            or "?"
        )
        logging.info(
            "webhook: received %s for %s (project %s)",
            event_type, project_name, project_id,
        )

        debounce.schedule(
            project_id,
            cfg.webhook_debounce_sec,
            _process_event,
            args=(cfg, project_id, event_type),
        )

        return jsonify({"status": "accepted", "event": event_type}), 202

    return app


def run_server(cfg: Config) -> None:
    """Start the webhook server (blocking)."""
    app = create_app(cfg)
    logging.info(
        "webhook server starting on %s:%d", cfg.webhook_host, cfg.webhook_port
    )
    app.run(
        host=cfg.webhook_host,
        port=cfg.webhook_port,
        debug=False,
        use_reloader=False,
    )
