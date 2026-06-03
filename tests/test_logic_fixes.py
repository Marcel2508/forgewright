"""Tests for the logic/flow fixes (note ordering, posting, GitHub API, etc.)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from forgewright.decision import fingerprint_mr, should_process_mr
from forgewright.formatting import format_notes, notes_from_discussions
from forgewright.helpers import parse_ts
from forgewright.parsing import parse_sections
from forgewright.platform.github import GitHubPlatform
from forgewright.posting import post_review_comments
from forgewright.state import State
from forgewright.types import DiffRefs, Discussion, MRDetail, Note, User
from tests.conftest import (
    MockAgent, MockPlatform, make_mr, make_note, make_project,
)


class TestParseTs:
    def test_z_suffix(self):
        assert parse_ts("2024-01-01T00:00:00Z").tzinfo is not None

    def test_offset_and_z_compare_equal(self):
        assert parse_ts("2024-01-01T00:00:00Z") == \
            parse_ts("2024-01-01T00:00:00+00:00")

    def test_ordering(self):
        assert parse_ts("2024-01-02T00:00:00Z") > parse_ts("2024-01-01T23:00:00Z")

    def test_naive_treated_as_utc(self):
        assert parse_ts("2024-01-01T00:00:00").tzinfo is not None

    def test_none_and_garbage_are_min(self):
        lo = datetime.min.replace(tzinfo=timezone.utc)
        assert parse_ts(None) == lo
        assert parse_ts("not-a-date") == lo


class TestNotesChronologicalOrdering:
    def test_flatten_sorts_by_created_at(self):
        # Mimic GitHub: a review thread (newer) appended before an older issue
        # comment — list order is NOT chronological.
        discs = [
            Discussion(id="review:70", notes=[
                make_note(note_id=70, author="dev", body="newer review",
                          created_at="2024-01-02T00:00:00Z")]),
            Discussion(id="issue:100", notes=[
                make_note(note_id=100, author="dev", body="older issue",
                          created_at="2024-01-01T00:00:00Z")]),
        ]
        flat = notes_from_discussions(discs)
        assert [n.id for n in flat] == [100, 70]  # chronological


class TestNewestNoteFingerprint:
    """The core GitHub bug: a new review comment with a *lower* id than an
    existing issue comment must still be detected as new activity."""

    def _fp(self, discs):
        notes = notes_from_discussions(discs)
        return fingerprint_mr(make_mr(source_branch="forgewright/x"),
                              notes, [], "forgewright")

    def test_new_lower_id_review_comment_triggers(self):
        old = [Discussion(id="issue:100", notes=[
            make_note(note_id=100, author="dev", body="q",
                      created_at="2024-01-01T00:00:00Z")])]
        prev = {"fingerprint": self._fp(old)}

        new = [
            Discussion(id="review:70", notes=[
                make_note(note_id=70, author="dev", body="@forgewright fix",
                          created_at="2024-01-02T00:00:00Z")]),
            Discussion(id="issue:100", notes=[
                make_note(note_id=100, author="dev", body="q",
                          created_at="2024-01-01T00:00:00Z")]),
        ]
        notes = notes_from_discussions(new)
        go, reason = should_process_mr(
            make_mr(source_branch="forgewright/x"), notes, [], prev,
            "forgewright", "forgewright/")
        assert go is True
        assert "new comment" in reason

    def test_no_change_does_not_trigger(self):
        discs = [Discussion(id="issue:100", notes=[
            make_note(note_id=100, author="dev", body="q",
                      created_at="2024-01-01T00:00:00Z")])]
        prev = {"fingerprint": self._fp(discs)}
        notes = notes_from_discussions(discs)
        go, reason = should_process_mr(
            make_mr(source_branch="forgewright/x"), notes, [], prev,
            "forgewright", "forgewright/")
        assert go is False


class TestFormatNotesFilterBeforeSlice:
    def test_user_request_not_dropped_by_system_burst(self):
        notes = [make_note(note_id=1, author="dev", body="THE REQUEST",
                           created_at="2024-01-01T00:00:00Z")]
        notes += [make_note(note_id=i, author="sys", body="sys", system=True,
                            created_at="2024-01-01T00:00:00Z")
                  for i in range(2, 60)]
        out = format_notes(notes, limit=40)
        assert "THE REQUEST" in out


class TestParseSectionsNoDuplicate:
    def test_inline_only_has_empty_general(self):
        summary = "## Inline: foo.py:10\nThis is a bug.\n"
        inlines, replies, general = parse_sections(summary)
        assert len(inlines) == 1
        assert inlines[0]["file_path"] == "foo.py"
        assert inlines[0]["line"] == 10
        assert replies == {}
        assert general == ""

    def test_all_three_section_types(self):
        summary = (
            "preamble text\n"
            "## Inline: a.py:3\ninline body\n"
            "## Reply to discussion abc\nreply body\n"
            "## General\ngeneral body\n")
        inlines, replies, general = parse_sections(summary)
        assert inlines[0]["file_path"] == "a.py"
        assert replies == {"abc": "reply body"}
        assert "preamble text" in general
        assert "general body" in general
        assert "inline body" not in general
        assert "reply body" not in general

    def test_review_inline_not_posted_as_duplicate_comment(self):
        platform = MockPlatform()
        mr_detail = MRDetail(
            number=1,
            diff_refs=DiffRefs(base_sha="b", head_sha="h", start_sha="s"),
            changes=[])
        summary = "## Inline: foo.py:10\nThis line is buggy.\n"
        post_review_comments(platform, 42, mr_detail, summary)
        names = [c[0] for c in platform.calls]
        assert "create_mr_discussion" in names
        assert "comment_mr" not in names  # not duplicated as top-level comment


class TestGitHubListMrsClientSideFilter:
    def test_stops_at_cutoff(self):
        plat = GitHubPlatform("https://api.github.com", "ghp")
        prs = [
            {"number": 3, "updated_at": "2024-03-01T00:00:00Z",
             "head": {}, "base": {}, "user": {}},
            {"number": 2, "updated_at": "2024-02-01T00:00:00Z",
             "head": {}, "base": {}, "user": {}},
            {"number": 1, "updated_at": "2024-01-01T00:00:00Z",
             "head": {}, "base": {}, "user": {}},
        ]
        with patch.object(plat, "_paginate", return_value=iter(prs)):
            out = plat.list_mrs("o/r", "2024-02-01T00:00:00Z")
        # newest-first; stops once older than cutoff (PR #1 excluded)
        assert [m.number for m in out] == [3, 2]

    def test_no_cutoff_returns_all(self):
        plat = GitHubPlatform("https://api.github.com", "ghp")
        prs = [{"number": 1, "updated_at": "2024-01-01T00:00:00Z",
                "head": {}, "base": {}, "user": {}}]
        with patch.object(plat, "_paginate", return_value=iter(prs)):
            out = plat.list_mrs("o/r", None)
        assert [m.number for m in out] == [1]


class TestGitHubPipelinesDedup:
    def test_latest_per_workflow_and_failure_first(self):
        plat = GitHubPlatform("https://api.github.com", "ghp")
        pr_resp = MagicMock()
        pr_resp.json.return_value = {"head": {"sha": "abc"}}
        pr_resp.raise_for_status = lambda: None
        runs_resp = MagicMock()
        runs_resp.json.return_value = {"workflow_runs": [
            # workflow "build": two attempts; latest (id 12) succeeded
            {"id": 10, "workflow_id": 1, "name": "build",
             "status": "completed", "conclusion": "failure",
             "head_sha": "abc", "html_url": "u"},
            {"id": 12, "workflow_id": 1, "name": "build",
             "status": "completed", "conclusion": "success",
             "head_sha": "abc", "html_url": "u"},
            # workflow "test": failed
            {"id": 11, "workflow_id": 2, "name": "test",
             "status": "completed", "conclusion": "failure",
             "head_sha": "abc", "html_url": "u"},
        ]}
        runs_resp.raise_for_status = lambda: None
        with patch.object(plat, "_req", side_effect=[pr_resp, runs_resp]):
            pipes = plat.mr_pipelines("o/r", 1)
        # one entry per workflow (deduped), and the failed one sorts first
        assert len(pipes) == 2
        assert pipes[0].status == "failed"
        # the failing workflow is "test"; "build"'s latest run is success
        statuses = {p.status for p in pipes}
        assert statuses == {"failed", "success"}


class TestGitHubUserAccessLevel:
    @pytest.mark.parametrize("role,level", [
        ("admin", 50), ("maintain", 40), ("write", 30),
        ("triage", 20), ("read", 10), ("none", 0),
    ])
    def test_role_mapping(self, role, level):
        plat = GitHubPlatform("https://api.github.com", "ghp")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"role_name": role}
        resp.raise_for_status = lambda: None
        with patch.object(plat, "_req", return_value=resp):
            assert plat.user_access_level("o/r", User("bob", id=1)) == level

    def test_non_collaborator_404_is_zero(self):
        plat = GitHubPlatform("https://api.github.com", "ghp")
        resp = MagicMock(status_code=404)
        with patch.object(plat, "_req", return_value=resp):
            assert plat.user_access_level("o/r", User("bob", id=1)) == 0


class TestHeadShaSelfTriggerFix:
    def test_pushed_sha_recorded_in_fingerprint(self, tmp_config, tmp_path):
        from forgewright.handlers import handle_mr
        platform = MockPlatform()
        agent = MockAgent(ok=True, summary="## General\nupdated")
        state = State(tmp_path / "state.json")
        project = make_project()
        mr = make_mr(number=5, source_branch="forgewright/x", sha="OLDSHA")

        with patch("forgewright.handlers.clone_or_update_mirror") as mc, \
             patch("forgewright.handlers.make_worktree") as mw, \
             patch("forgewright.handlers.cleanup_worktree"), \
             patch("forgewright.handlers.push_branch", return_value="NEWSHA"), \
             patch("forgewright.handlers._wait_for_mr_update"):
            mc.return_value = tmp_path / "mirror"
            wt = tmp_path / "wt"
            (wt / ".claude").mkdir(parents=True, exist_ok=True)
            mw.return_value = wt
            handle_mr(tmp_config, platform, agent, state, project, mr,
                      [], [], None, "new commits")

        fp = state.proj(project.id)["merge_requests"]["5"]["fingerprint"]
        assert fp["head_sha"] == "NEWSHA"  # not the pre-push OLDSHA


class TestConfigNewFields:
    def _base(self, tmp_path, extra=""):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "platform_url: https://git.example.com\n"
            "platform_token: tok\n"
            "bot_username: forgewright\n"
            f"workdir: {tmp_path/'w'}\n"
            f"state_file: {tmp_path/'s.json'}\n"
            f"lock_dir: {tmp_path/'l'}\n"
            f"log_file: {tmp_path/'b.log'}\n" + extra)
        return str(cfg)

    def test_authorization_min_role_loaded(self, tmp_path):
        from forgewright.config import Config
        c = Config.load(self._base(tmp_path, "authorization_min_role: write\n"))
        assert c.authorization_min_role == "write"

    def test_authorization_default_none(self, tmp_path):
        from forgewright.config import Config
        c = Config.load(self._base(tmp_path))
        assert c.authorization_min_role is None

    def test_webhook_host_env_override(self, tmp_path, monkeypatch):
        from forgewright.config import Config
        monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")
        c = Config.load(self._base(tmp_path))
        assert c.webhook_host == "0.0.0.0"

    def test_webhook_host_config_beats_env(self, tmp_path, monkeypatch):
        from forgewright.config import Config
        monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")
        c = Config.load(self._base(tmp_path, 'webhook_host: "1.2.3.4"\n'))
        assert c.webhook_host == "1.2.3.4"


class TestWebhookSecretStartupGuard:
    @patch("forgewright.main.create_platform")
    def test_serve_refuses_without_secret(self, mock_plat, tmp_path):
        from forgewright.main import main
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "platform_url: https://git.example.com\n"
            "platform_token: tok\n"
            "bot_username: forgewright\n"
            f"workdir: {tmp_path/'w'}\n"
            f"state_file: {tmp_path/'s.json'}\n"
            f"lock_dir: {tmp_path/'l'}\n"
            f"log_file: {tmp_path/'b.log'}\n"
            "webhook_enabled: true\n")
        with patch("sys.argv", ["forgewright", "--config", str(cfg),
                                 "--serve"]):
            assert main() == 1  # refuses: no webhook_secret
        mock_plat.assert_not_called()
