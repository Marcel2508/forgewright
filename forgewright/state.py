"""JSON-backed state persistence for tracking processed issues/MRs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class State:
    """JSON file keyed by project -> {issues,merge_requests} -> iid -> fingerprint."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except Exception:
                self.data = {}

    def proj(self, pid: int | str) -> dict:
        return self.data.setdefault(str(pid), {
            "issues": {},
            "merge_requests": {},
            "last_checked_at": None,
        })

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)
