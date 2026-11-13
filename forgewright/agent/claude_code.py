"""Claude Code CLI agent implementation."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from forgewright.agent.base import (
    FORGE_SECRET_ENV_VARS,
    Agent,
    AgentResult,
    kill_process_group,
    reap_leftovers,
)
from forgewright.parsing import read_summary


def _feed_stdin(proc, text: str) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.write(text)
            proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass  # agent exited early; its exit code / output tell the story


class ClaudeCodeAgent(Agent):
    """Runs Claude Code CLI in headless mode."""

    def __init__(self, binary: str = "claude", model: str | None = None,
                 timeout_sec: int = 3600):
        self._binary = binary
        self._model = model
        self._timeout = timeout_sec

    @property
    def name(self) -> str:
        return "Claude Code"

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        # The prompt goes in on stdin, not argv: a big review diff exceeds
        # Linux' 128 KiB per-argument limit and Popen would fail with E2BIG.
        # (`claude -p` reads the prompt from stdin when no argument is given.)
        cmd = [self._binary, "-p", "--dangerously-skip-permissions"]
        if self._model:
            cmd += ["--model", self._model]

        env = os.environ.copy()
        for secret_var in FORGE_SECRET_ENV_VARS:
            env.pop(secret_var, None)
        env.setdefault("CI", "1")

        live_log = cwd / ".claude" / "claude-live.log"
        live_log.parent.mkdir(parents=True, exist_ok=True)

        logging.info("AGENT [%s] starting in %s (prompt %d chars)",
                     self.name, cwd, len(prompt))
        logging.info("AGENT live output: tail -f %s", live_log)

        try:
            with open(live_log, "w") as lf:
                proc = subprocess.Popen(
                    cmd, cwd=cwd, env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
                # Feed the prompt from a thread so a large prompt can never
                # deadlock against the stdout reader below.
                feeder = threading.Thread(
                    target=_feed_stdin, args=(proc, prompt), daemon=True)
                feeder.start()
                timed_out = threading.Event()

                def _kill_on_timeout():
                    timed_out.set()
                    kill_process_group(proc)

                timer = threading.Timer(self._timeout, _kill_on_timeout)
                timer.start()
                chunks = []
                try:
                    for line in proc.stdout:
                        chunks.append(line)
                        lf.write(line)
                        lf.flush()
                    proc.wait()
                    feeder.join(timeout=5)
                finally:
                    timer.cancel()
                    reap_leftovers(proc)

                if timed_out.is_set():
                    output = "".join(chunks)
                    return AgentResult(
                        ok=False,
                        output=f"TIMEOUT after {self._timeout}s\n{output}",
                        summary="",
                    )
        except Exception as e:
            logging.error("AGENT [%s] failed to start: %s", self.name, e)
            raise

        output = "".join(chunks)
        ok = proc.returncode == 0
        logging.info("AGENT [%s] exit=%s", self.name, proc.returncode)

        summary = read_summary(cwd)
        return AgentResult(ok=ok, output=output, summary=summary)
