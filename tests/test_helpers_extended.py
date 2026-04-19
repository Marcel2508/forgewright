"""Tests for run() and file_lock() in forgewright.helpers."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from forgewright.helpers import file_lock, run


class TestRun:
    def test_basic_command(self):
        result = run(["echo", "hello"], capture=True)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_check_raises_on_failure(self):
        with pytest.raises(subprocess.CalledProcessError):
            run(["false"], check=True)

    def test_check_false_no_raise(self):
        result = run(["false"], check=False)
        assert result.returncode != 0

    def test_capture_true(self):
        result = run(["echo", "captured"], capture=True)
        assert "captured" in result.stdout

    def test_capture_false(self):
        result = run(["echo", "uncaptured"], capture=False)
        assert result.stdout is None

    def test_cwd(self, tmp_path):
        result = run(["pwd"], cwd=tmp_path, capture=True)
        assert str(tmp_path) in result.stdout

    def test_env(self):
        result = run(["env"], env={"MY_VAR": "hello", "PATH": "/usr/bin"},
                     capture=True)
        assert "MY_VAR=hello" in result.stdout

    def test_timeout(self):
        with pytest.raises(subprocess.TimeoutExpired):
            run(["sleep", "10"], timeout=1)


class TestFileLock:
    def test_acquires_lock(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        with file_lock(lock_path) as got:
            assert got is True
            assert lock_path.exists()

    def test_creates_parent_dirs(self, tmp_path):
        lock_path = tmp_path / "sub" / "dir" / "test.lock"
        with file_lock(lock_path) as got:
            assert got is True
            assert lock_path.parent.exists()

    def test_concurrent_lock_returns_false(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        results = []

        def try_lock():
            with file_lock(lock_path) as got:
                results.append(got)
                if got:
                    import time
                    time.sleep(0.2)

        t1 = threading.Thread(target=try_lock)
        t2 = threading.Thread(target=try_lock)
        t1.start()
        import time
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()

        assert True in results
        assert False in results

    def test_lock_released_after_context(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        with file_lock(lock_path) as got:
            assert got is True

        with file_lock(lock_path) as got:
            assert got is True
