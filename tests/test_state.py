"""Tests for forgewright.state."""

import json
from pathlib import Path

import pytest

from forgewright.state import State


class TestState:
    def test_new_state(self, tmp_path):
        path = tmp_path / "state.json"
        s = State(path)
        assert s.data == {}

    def test_load_existing(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"42": {"issues": {}, "merge_requests": {},
                                            "last_checked_at": "2024-01-01"}}))
        s = State(path)
        assert "42" in s.data

    def test_load_corrupt_file(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not valid json{{{")
        s = State(path)
        assert s.data == {}

    def test_proj_creates_skeleton(self, tmp_path):
        s = State(tmp_path / "state.json")
        p = s.proj(123)
        assert "issues" in p
        assert "merge_requests" in p
        assert p["last_checked_at"] is None

    def test_proj_returns_same_dict(self, tmp_path):
        s = State(tmp_path / "state.json")
        p1 = s.proj(123)
        p2 = s.proj(123)
        assert p1 is p2

    def test_save_and_reload(self, tmp_path):
        path = tmp_path / "state.json"
        s = State(path)
        s.proj(42)["issues"]["1"] = {"fingerprint": {"a": 1}}
        s.save()

        s2 = State(path)
        assert s2.proj(42)["issues"]["1"]["fingerprint"] == {"a": 1}

    def test_save_atomic(self, tmp_path):
        """Save uses a tmp file then rename — check no partial writes."""
        path = tmp_path / "state.json"
        s = State(path)
        s.proj(1)
        s.save()
        # The file should exist and be valid JSON
        data = json.loads(path.read_text())
        assert "1" in data

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "state.json"
        s = State(path)
        s.proj(1)
        s.save()
        assert path.exists()
