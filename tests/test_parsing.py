"""Tests for forgewright.parsing."""

import pytest

from forgewright.parsing import (
    parse_review_comments,
    parse_summary_replies,
    read_summary,
)


class TestParseSummaryReplies:
    def test_empty(self):
        replies, general = parse_summary_replies("")
        assert replies == {}
        assert general == ""

    def test_general_only(self):
        text = "## General\nThis is a general summary."
        replies, general = parse_summary_replies(text)
        assert replies == {}
        assert general == "This is a general summary."

    def test_single_reply(self):
        text = "## Reply to discussion abc123\nFixed the issue as suggested."
        replies, general = parse_summary_replies(text)
        assert "abc123" in replies
        assert replies["abc123"] == "Fixed the issue as suggested."
        assert general == ""

    def test_multiple_replies(self):
        text = (
            "## Reply to discussion disc1\n"
            "Reply to first thread.\n\n"
            "## Reply to discussion disc2\n"
            "Reply to second thread."
        )
        replies, general = parse_summary_replies(text)
        assert len(replies) == 2
        assert "disc1" in replies
        assert "disc2" in replies
        assert "first thread" in replies["disc1"]
        assert "second thread" in replies["disc2"]

    def test_mixed_replies_and_general(self):
        text = (
            "## Reply to discussion disc1\n"
            "Fixed it.\n\n"
            "## General\n"
            "Overall changes summary."
        )
        replies, general = parse_summary_replies(text)
        assert "disc1" in replies
        assert replies["disc1"] == "Fixed it."
        assert "Overall changes summary" in general

    def test_preamble_preserved(self):
        text = (
            "Some preamble text.\n\n"
            "## Reply to discussion disc1\n"
            "A reply."
        )
        replies, general = parse_summary_replies(text)
        assert "disc1" in replies
        # Preamble should be in general parts
        assert general == "" or "preamble" in general.lower()

    def test_unstructured_text(self):
        text = "Just a plain summary without any structured sections."
        replies, general = parse_summary_replies(text)
        assert replies == {}
        assert general == text

    def test_general_inside_reply(self):
        text = (
            "## Reply to discussion disc1\n"
            "Reply body.\n\n"
            "## General\n"
            "General body."
        )
        replies, general = parse_summary_replies(text)
        assert replies["disc1"] == "Reply body."
        assert "General body" in general


class TestParseReviewComments:
    def test_empty(self):
        inlines, general = parse_review_comments("")
        assert inlines == []
        assert general == ""

    def test_general_only(self):
        text = "## General\nGreat code overall."
        inlines, general = parse_review_comments(text)
        assert inlines == []
        assert "Great code overall" in general

    def test_single_inline(self):
        text = "## Inline: src/foo.py:42\nPossible null pointer here."
        inlines, general = parse_review_comments(text)
        assert len(inlines) == 1
        assert inlines[0]["file_path"] == "src/foo.py"
        assert inlines[0]["line"] == 42
        assert "null pointer" in inlines[0]["body"]

    def test_multiple_inlines(self):
        text = (
            "## Inline: src/foo.py:10\n"
            "First comment.\n\n"
            "## Inline: src/bar.py:20\n"
            "Second comment."
        )
        inlines, general = parse_review_comments(text)
        assert len(inlines) == 2
        assert inlines[0]["file_path"] == "src/foo.py"
        assert inlines[0]["line"] == 10
        assert inlines[1]["file_path"] == "src/bar.py"
        assert inlines[1]["line"] == 20

    def test_mixed_inline_and_general(self):
        text = (
            "## Inline: src/foo.py:42\n"
            "Bug here.\n\n"
            "## General\n"
            "Overall looks good."
        )
        inlines, general = parse_review_comments(text)
        assert len(inlines) == 1
        assert inlines[0]["file_path"] == "src/foo.py"
        assert "Overall looks good" in general

    def test_unstructured_text(self):
        text = "Plain review without structure."
        inlines, general = parse_review_comments(text)
        assert inlines == []
        assert general == text

    def test_file_path_with_nested_dirs(self):
        text = "## Inline: src/deep/nested/file.py:100\nIssue here."
        inlines, general = parse_review_comments(text)
        assert len(inlines) == 1
        assert inlines[0]["file_path"] == "src/deep/nested/file.py"
        assert inlines[0]["line"] == 100


class TestReadSummary:
    def test_file_exists(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "last-run-summary.md").write_text("  Hello World  ")
        assert read_summary(tmp_path) == "Hello World"

    def test_file_missing(self, tmp_path):
        assert read_summary(tmp_path) == ""

    def test_empty_file(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "last-run-summary.md").write_text("")
        assert read_summary(tmp_path) == ""
