"""Tests for forgewright.decision."""

import pytest

from forgewright.decision import (
    extract_user_instructions,
    fingerprint_issue,
    fingerprint_mr,
    is_review_mode,
    select_projects,
    should_process_issue,
    should_process_mr,
)
from forgewright.types import Pipeline, Project, User
from tests.conftest import make_issue, make_mr, make_note, make_project


class TestIsReviewMode:
    def test_bot_branch_and_bot_author(self):
        mr = make_mr(source_branch="forgewright/issue-1-fix", author="forgewright")
        assert not is_review_mode(mr, "forgewright", "forgewright/")

    def test_non_bot_branch_non_bot_author(self):
        mr = make_mr(source_branch="feature/foo", author="alice")
        assert is_review_mode(mr, "forgewright", "forgewright/")

    def test_bot_branch_non_bot_author(self):
        mr = make_mr(source_branch="forgewright/issue-1-fix", author="alice")
        assert not is_review_mode(mr, "forgewright", "forgewright/")

    def test_non_bot_branch_bot_author(self):
        mr = make_mr(source_branch="feature/foo", author="forgewright")
        assert not is_review_mode(mr, "forgewright", "forgewright/")

    def test_custom_prefix(self):
        mr = make_mr(source_branch="ai/fix-bug", author="alice")
        assert not is_review_mode(mr, "forgewright", "ai/")

    def test_empty_source_branch(self):
        mr = make_mr(source_branch="", author="alice")
        assert is_review_mode(mr, "forgewright", "forgewright/")


class TestFingerprintIssue:
    def test_basic(self):
        issue = make_issue(updated_at="2024-01-01", labels=["bug", "urgent"])
        notes = [make_note(note_id=10, author="alice")]
        fp = fingerprint_issue(issue, notes, "forgewright")
        assert "description_hash" in fp
        assert "updated_at" not in fp
        assert fp["last_note_id"] == 10
        assert fp["labels"] == ["bug", "urgent"]

    def test_description_hash_changes(self):
        fp1 = fingerprint_issue(make_issue(description="v1"), [], "forgewright")
        fp2 = fingerprint_issue(make_issue(description="v2"), [], "forgewright")
        assert fp1["description_hash"] != fp2["description_hash"]

    def test_description_hash_stable(self):
        fp1 = fingerprint_issue(make_issue(description="same"), [], "forgewright")
        fp2 = fingerprint_issue(make_issue(description="same"), [], "forgewright")
        assert fp1["description_hash"] == fp2["description_hash"]

    def test_filters_bot_notes(self):
        notes = [
            make_note(note_id=1, author="alice"),
            make_note(note_id=2, author="forgewright"),
            make_note(note_id=3, author="bob"),
        ]
        fp = fingerprint_issue(make_issue(), notes, "forgewright")
        assert fp["last_note_id"] == 3

    def test_filters_system_notes(self):
        notes = [
            make_note(note_id=1, author="alice"),
            make_note(note_id=2, author="alice", system=True),
        ]
        fp = fingerprint_issue(make_issue(), notes, "forgewright")
        assert fp["last_note_id"] == 1

    def test_no_notes(self):
        fp = fingerprint_issue(make_issue(), [], "forgewright")
        assert fp["last_note_id"] is None
        assert fp["last_note_at"] is None

    def test_labels_sorted(self):
        issue = make_issue(labels=["z-label", "a-label"])
        fp = fingerprint_issue(issue, [], "forgewright")
        assert fp["labels"] == ["a-label", "z-label"]


class TestFingerprintMR:
    def test_basic(self):
        mr = make_mr(updated_at="2024-01-01", sha="abc")
        notes = [make_note(note_id=5, author="alice")]
        pipes = [Pipeline(id=1, sha="abc", status="success", web_url="")]
        fp = fingerprint_mr(mr, notes, pipes, "forgewright")
        assert "description_hash" in fp
        assert "updated_at" not in fp
        assert fp["last_note_id"] == 5
        assert fp["pipeline_sha"] == "abc"
        assert fp["pipeline_status"] == "success"
        assert fp["head_sha"] == "abc"

    def test_no_pipelines(self):
        fp = fingerprint_mr(make_mr(), [make_note()], [], "forgewright")
        assert fp["pipeline_sha"] is None
        assert fp["pipeline_status"] is None


class TestShouldProcessIssue:
    def test_no_mention(self):
        issue = make_issue(description="No mention here")
        notes = [make_note(body="Just a comment")]
        go, reason = should_process_issue(issue, notes, None, "forgewright")
        assert not go
        assert "no @forgewright mention" in reason

    def test_no_mention_reason_uses_bot_username(self):
        issue = make_issue(description="No mention here")
        go, reason = should_process_issue(issue, [], None, "jarvis")
        assert not go
        assert "no @jarvis mention" in reason

    def test_mention_with_custom_bot_username(self):
        issue = make_issue(description="Hey @jarvis fix this")
        go, reason = should_process_issue(issue, [], None, "jarvis")
        assert go
        assert "mention" in reason

    def test_claude_mention_ignored_when_bot_is_jarvis(self):
        issue = make_issue(description="Hey @forgewright fix this")
        go, reason = should_process_issue(issue, [], None, "jarvis")
        assert not go
        assert "no @jarvis mention" in reason

    def test_mention_in_description(self):
        issue = make_issue(description="Hey @forgewright fix this")
        go, reason = should_process_issue(issue, [], None, "forgewright")
        assert go
        assert "mention" in reason

    def test_mention_in_note(self):
        issue = make_issue(description="No mention")
        notes = [make_note(body="@forgewright please help", author="alice")]
        go, reason = should_process_issue(issue, notes, None, "forgewright")
        assert go

    def test_bot_note_mention_ignored(self):
        issue = make_issue(description="No mention")
        notes = [make_note(body="@forgewright test", author="forgewright")]
        go, reason = should_process_issue(issue, notes, None, "forgewright")
        assert not go

    def test_no_change_since_last_run(self):
        issue = make_issue(description="@forgewright fix")
        notes = [make_note(note_id=1, author="alice")]
        fp = fingerprint_issue(issue, notes, "forgewright")
        prev = {"fingerprint": fp}
        go, reason = should_process_issue(issue, notes, prev, "forgewright")
        assert not go
        assert "no change" in reason

    def test_new_comment_triggers(self):
        issue = make_issue(description="@forgewright fix")
        notes = [make_note(note_id=5, author="alice")]
        fp = fingerprint_issue(issue, [], "forgewright")
        prev = {"fingerprint": fp}
        go, reason = should_process_issue(issue, notes, prev, "forgewright")
        assert go
        assert "new comment" in reason

    def test_description_edit_triggers(self):
        issue = make_issue(description="@forgewright fix v2")
        notes = [make_note(note_id=1, author="alice")]
        fp = fingerprint_issue(
            make_issue(description="@forgewright fix v1"), notes, "forgewright")
        prev = {"fingerprint": fp}
        go, reason = should_process_issue(issue, notes, prev, "forgewright")
        assert go
        assert "description edited" in reason

    def test_no_meaningful_change_skips(self):
        issue = make_issue(description="@forgewright fix")
        notes = [make_note(note_id=1, author="alice")]
        fp = fingerprint_issue(issue, notes, "forgewright")
        prev_fp = dict(fp, _extra="stale")
        prev = {"fingerprint": prev_fp}
        go, reason = should_process_issue(issue, notes, prev, "forgewright")
        assert not go


class TestShouldProcessMR:
    def test_not_a_claude_mr(self):
        mr = make_mr(source_branch="feature/foo", author="alice",
                     description="No mention")
        go, reason = should_process_mr(mr, [], [], None, "forgewright", "forgewright/")
        assert not go
        assert "not a bot-owned MR" in reason

    def test_claude_branch_first_seen_no_activity(self):
        mr = make_mr(source_branch="forgewright/issue-1", author="forgewright")
        go, reason = should_process_mr(mr, [], [], None, "forgewright", "forgewright/")
        assert not go
        assert "first-seen, no human activity" in reason

    def test_claude_branch_first_seen_with_comment(self):
        mr = make_mr(source_branch="forgewright/issue-1", author="forgewright")
        notes = [make_note(author="alice", body="looks good")]
        go, reason = should_process_mr(mr, notes, [], None, "forgewright", "forgewright/")
        assert go
        assert "first-seen with human activity" in reason

    def test_mention_in_description(self):
        mr = make_mr(source_branch="feature/foo", author="alice",
                     description="@forgewright review this")
        go, reason = should_process_mr(mr, [], [], None, "forgewright", "forgewright/")
        assert go

    def test_reviewer(self):
        mr = make_mr(source_branch="feature/foo", author="alice",
                     reviewers=[User(username="forgewright")])
        go, reason = should_process_mr(mr, [], [], None, "forgewright", "forgewright/")
        assert go

    def test_assignee(self):
        mr = make_mr(source_branch="feature/foo", author="alice",
                     assignees=[User(username="forgewright")])
        go, reason = should_process_mr(mr, [], [], None, "forgewright", "forgewright/")
        assert go

    def test_no_change_since_last_run(self):
        mr = make_mr(source_branch="forgewright/issue-1", author="forgewright")
        notes = [make_note(note_id=1, author="alice")]
        pipes = [Pipeline(id=1, sha="abc", status="success", web_url="")]
        fp = fingerprint_mr(mr, notes, pipes, "forgewright")
        prev = {"fingerprint": fp}
        go, reason = should_process_mr(
            mr, notes, pipes, prev, "forgewright", "forgewright/")
        assert not go

    def test_new_comment_triggers(self):
        mr = make_mr(source_branch="forgewright/issue-1", author="forgewright")
        notes = [make_note(note_id=5, author="alice")]
        pipes = [Pipeline(id=1, sha="abc", status="success", web_url="")]
        prev = {"fingerprint": {
            "description_hash": fingerprint_mr(mr, [], pipes, "forgewright")["description_hash"],
            "last_note_id": 1,
            "last_note_at": "2024-01-01",
            "labels": [],
            "pipeline_sha": "abc",
            "pipeline_status": "success",
            "head_sha": "abc123",
        }}
        go, reason = should_process_mr(
            mr, notes, pipes, prev, "forgewright", "forgewright/")
        assert go
        assert "new comment" in reason

    def test_pipeline_change_triggers(self):
        mr = make_mr(source_branch="forgewright/issue-1", author="forgewright")
        notes = [make_note(note_id=1, author="alice")]
        pipes = [Pipeline(id=1, sha="abc", status="failed", web_url="")]
        prev = {"fingerprint": {
            "description_hash": fingerprint_mr(mr, notes, pipes, "forgewright")["description_hash"],
            "last_note_id": 1,
            "last_note_at": "2024-01-01T00:00:00Z",
            "labels": [],
            "pipeline_sha": "abc",
            "pipeline_status": "success",
            "head_sha": "abc123",
        }}
        go, reason = should_process_mr(
            mr, notes, pipes, prev, "forgewright", "forgewright/")
        assert go
        assert "pipeline" in reason

    def test_label_change_triggers(self):
        mr = make_mr(source_branch="forgewright/issue-1", author="forgewright",
                     labels=["new-label"])
        notes = [make_note(note_id=1, author="alice")]
        pipes = []
        prev = {"fingerprint": {
            "description_hash": fingerprint_mr(mr, notes, pipes, "forgewright")["description_hash"],
            "last_note_id": 1,
            "last_note_at": "2024-01-01T00:00:00Z",
            "labels": [],
            "pipeline_sha": None,
            "pipeline_status": None,
            "head_sha": "abc123",
        }}
        go, reason = should_process_mr(
            mr, notes, pipes, prev, "forgewright", "forgewright/")
        assert go
        assert "labels changed" in reason


class TestSelectProjects:
    def test_no_filters(self, mock_platform, tmp_config):
        mock_platform.list_member_projects = lambda: [
            Project(id=1, path="a/b", web_url="", default_branch="main",
                    http_clone_url=""),
            Project(id=2, path="c/d", web_url="", default_branch="main",
                    http_clone_url=""),
        ]
        result = select_projects(mock_platform, tmp_config)
        assert len(result) == 2

    def test_include_filter(self, mock_platform, tmp_config):
        mock_platform.list_member_projects = lambda: [
            Project(id=1, path="a/b", web_url="", default_branch="main",
                    http_clone_url=""),
            Project(id=2, path="c/d", web_url="", default_branch="main",
                    http_clone_url=""),
        ]
        tmp_config.projects_include = ["a/b"]
        result = select_projects(mock_platform, tmp_config)
        assert len(result) == 1
        assert result[0].path == "a/b"

    def test_exclude_filter(self, mock_platform, tmp_config):
        mock_platform.list_member_projects = lambda: [
            Project(id=1, path="a/b", web_url="", default_branch="main",
                    http_clone_url=""),
            Project(id=2, path="c/d", web_url="", default_branch="main",
                    http_clone_url=""),
        ]
        tmp_config.projects_exclude = ["c/d"]
        result = select_projects(mock_platform, tmp_config)
        assert len(result) == 1
        assert result[0].path == "a/b"


class TestExtractUserInstructions:
    def test_mention_in_description(self):
        mr = make_mr(description="@forgewright review this carefully")
        result = extract_user_instructions(mr, [], "forgewright")
        assert "review this carefully" in result

    def test_mention_in_note(self):
        mr = make_mr(description="No mention")
        notes = [make_note(author="alice", body="@forgewright check the auth")]
        result = extract_user_instructions(mr, notes, "forgewright")
        assert "check the auth" in result

    def test_no_instructions(self):
        mr = make_mr(description="No mention")
        result = extract_user_instructions(mr, [], "forgewright")
        assert "no specific instructions" in result

    def test_bot_notes_excluded(self):
        mr = make_mr(description="No mention")
        notes = [make_note(author="forgewright", body="@forgewright self-mention")]
        result = extract_user_instructions(mr, notes, "forgewright")
        assert "no specific instructions" in result
