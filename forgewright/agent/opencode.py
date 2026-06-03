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
import re
import subprocess
import threading
from pathlib import Path

from forgewright.agent.base import Agent, AgentResult
from forgewright.parsing import read_summary

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NOISE_PREFIXES = (
    "Performing one time database migration",
    "sqlite-migration",
    "Database migration complete",
)


def _clean_opencode_output(text: str) -> str:
    """Strip opencode's ANSI codes, startup/migration chatter and the
    ``> build · <model>`` banner, leaving the assistant's actual response."""
    text = _ANSI_RE.sub("", text)
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if any(s.startswith(p) for p in _NOISE_PREFIXES):
            continue
        if s.startswith("> ") and " · " in s:  # agent/model banner line
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


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
        # sst/opencode (opencode.ai) non-interactive invocation: `opencode run
        # <message>` executes the agent headlessly in CWD (running tools without
        # prompting) and prints the assistant response to stdout. The model is
        # given as provider/model; the provider is defined in
        # ~/.config/opencode/opencode.json.
        cmd = [self._binary, "run"]
        if self._model:
            cmd += ["--model", self._model]
        cmd.append(prompt)

        env = os.environ.copy()
        # Keep high-privilege forge credentials out of the agent's environment so
        # a prompt-injection payload in untrusted issue/MR text can't read and
        # exfiltrate them. The agent never needs these (git push is handled by the
        # wrapper via GIT_ASKPASS in a separate subprocess env).
        for secret_var in ("PLATFORM_TOKEN", "GITLAB_TOKEN", "GITHUB_TOKEN",
                           "FORGEWRIGHT_GIT_TOKEN", "WEBHOOK_SECRET"):
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
        if ok and not summary:
            # opencode (esp. with smaller models) often answers directly in its
            # stdout response instead of writing .claude/last-run-summary.md as
            # the prompt asks. Fall back to the cleaned stdout so the user still
            # gets the answer rather than "(no summary)".
            summary = _clean_opencode_output(output)
        return AgentResult(ok=ok, output=output, summary=summary)
