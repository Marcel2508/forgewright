"""OpenCode CLI agent implementation.

OpenCode (https://github.com/opencode-ai/opencode) is an open-source
terminal-based AI coding assistant. This implementation invokes it in
non-interactive mode.

Note: OpenCode's CLI flags may vary by version. Adjust the command
construction below if your version uses different flags.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from forgewright.agent.base import Agent, AgentResult
from forgewright.parsing import read_summary


class OpenCodeAgent(Agent):
    """Runs OpenCode CLI in non-interactive mode."""

    def __init__(self, binary: str = "opencode", model: str | None = None,
                 timeout_sec: int = 3600):
        self._binary = binary
        self._model = model
        self._timeout = timeout_sec

    @property
    def name(self) -> str:
        return "OpenCode"

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        # OpenCode non-interactive invocation.
        # Uses --non-interactive and --prompt flags.
        # If your version differs, adjust these flags accordingly.
        cmd = [self._binary, "--non-interactive", "--prompt", prompt]
        if self._model:
            cmd += ["--model", self._model]

        env = os.environ.copy()
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
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True)
                timed_out = threading.Event()

                def _kill_on_timeout():
                    timed_out.set()
                    try:
                        proc.kill()
                    except OSError:
                        pass

                timer = threading.Timer(self._timeout, _kill_on_timeout)
                timer.start()
                chunks = []
                try:
                    for line in proc.stdout:
                        chunks.append(line)
                        lf.write(line)
                        lf.flush()
                    proc.wait()
                finally:
                    timer.cancel()

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
