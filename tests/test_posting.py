"""Tests for forgewright.posting."""

import pytest

from forgewright.posting import post_mr_responses, post_review_comments
from forgewright.types import DiffChange, DiffRefs, MRDetail
from tests.conftest import MockPlatform


class TestPostMrResponses:
    def test_single_reply(self, mock_platform: MockPlatform):
        summary = (
            "## Reply to discussion disc-1\n"
            "Fixed as suggested."
        )
        post_mr_responses(mock_platform, pid=1, mr_number=10, summary=summary)
        reply_calls = [c for c in mock_platform.calls
                       if c[0] == "reply_to_discussion"]
        assert len(reply_calls) == 1
        assert reply_calls[0][1][2] == "disc-1"
        assert "Fixed as suggested" in reply_calls[0][1][3]

    def test_general_comment(self, mock_platform: MockPlatform):
        summary = "## General\nOverall summary of changes."
        post_mr_responses(mock_platform, pid=1, mr_number=10, summary=summary)
        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert "Overall summary" in comment_calls[0][1][2]

    def test_reply_failure_falls_back(self, mock_platform: MockPlatform):
        def fail_reply(*args, **kwargs):
            raise RuntimeError("API error")
        mock_platform.reply_to_discussion = fail_reply

        summary = (
            "## Reply to discussion disc-1\n"
            "This reply will fail.\n\n"
            "## General\n"
            "General text."
        )
        post_mr_responses(mock_platform, pid=1, mr_number=10, summary=summary)
        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        body = comment_calls[0][1][2]
        assert "disc-1" in body
        assert "This reply will fail" in body

    def test_prefix(self, mock_platform: MockPlatform):
        summary = "Just a comment."
        post_mr_responses(mock_platform, pid=1, mr_number=10, summary=summary,
                          prefix="BOT: ")
        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert comment_calls[0][1][2].startswith("BOT: ")

    def test_empty_summary(self, mock_platform: MockPlatform):
        post_mr_responses(mock_platform, pid=1, mr_number=10, summary="")
        assert len(mock_platform.calls) == 0


class TestPostReviewComments:
    def test_inline_comment(self, mock_platform: MockPlatform):
        mr_detail = MRDetail(
            number=5,
            diff_refs=DiffRefs(base_sha="aaa", head_sha="bbb",
                               start_sha="ccc"),
            changes=[DiffChange(new_path="foo.py", old_path="foo.py",
                                diff="")],
        )
        summary = "## Inline: foo.py:10\nBug here.\n\n## General\nLooks ok."
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary)

        disc_calls = [c for c in mock_platform.calls
                      if c[0] == "create_mr_discussion"]
        assert len(disc_calls) == 1
        position = disc_calls[0][1][3]
        assert position["new_path"] == "foo.py"
        assert position["new_line"] == 10

    def test_inline_failure_falls_back(self, mock_platform: MockPlatform):
        def fail_disc(*args, **kwargs):
            mock_platform._record("create_mr_discussion", *args, **kwargs)
            raise RuntimeError("API error")
        mock_platform.create_mr_discussion = fail_disc

        mr_detail = MRDetail(
            number=5,
            diff_refs=DiffRefs(base_sha="aaa", head_sha="bbb",
                               start_sha="ccc"),
            changes=[],
        )
        summary = "## Inline: foo.py:10\nBug here."
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary)

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert "foo.py:10" in comment_calls[0][1][2]

    def test_renamed_file_old_path(self, mock_platform: MockPlatform):
        mr_detail = MRDetail(
            number=5,
            diff_refs=DiffRefs(base_sha="aaa", head_sha="bbb",
                               start_sha="ccc"),
            changes=[DiffChange(new_path="new.py", old_path="old.py",
                                diff="")],
        )
        summary = "## Inline: new.py:5\nComment."
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary)

        disc_calls = [c for c in mock_platform.calls
                      if c[0] == "create_mr_discussion"]
        position = disc_calls[0][1][3]
        assert position["new_path"] == "new.py"
        assert position["old_path"] == "old.py"

    def test_no_diff_refs(self, mock_platform: MockPlatform):
        mr_detail = MRDetail(number=5, diff_refs=None, changes=[])
        summary = "## Inline: foo.py:10\nBug.\n\n## General\nSummary."
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary)
        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert "foo.py:10" in comment_calls[0][1][2]

    def test_threaded_replies_in_review(self, mock_platform: MockPlatform):
        mr_detail = MRDetail(number=5, diff_refs=None, changes=[])
        summary = (
            "## Reply to discussion disc-abc\n"
            "Done, fixed as you suggested.\n\n"
            "## General\n"
            "Applied the requested changes."
        )
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary)

        reply_calls = [c for c in mock_platform.calls
                       if c[0] == "reply_to_discussion"]
        assert len(reply_calls) == 1
        assert reply_calls[0][1][2] == "disc-abc"
        assert "fixed as you suggested" in reply_calls[0][1][3]

        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        assert "Applied the requested changes" in comment_calls[0][1][2]

    def test_mixed_inline_and_replies(self, mock_platform: MockPlatform):
        mr_detail = MRDetail(
            number=5,
            diff_refs=DiffRefs(base_sha="aaa", head_sha="bbb",
                               start_sha="ccc"),
            changes=[DiffChange(new_path="foo.py", old_path="foo.py",
                                diff="")],
        )
        summary = (
            "## Inline: foo.py:10\n"
            "New issue found here.\n\n"
            "## Reply to discussion disc-xyz\n"
            "Fixed your earlier comment.\n\n"
            "## General\n"
            "Overall summary."
        )
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary)

        disc_calls = [c for c in mock_platform.calls
                      if c[0] == "create_mr_discussion"]
        assert len(disc_calls) == 1

        reply_calls = [c for c in mock_platform.calls
                       if c[0] == "reply_to_discussion"]
        assert len(reply_calls) == 1
        assert reply_calls[0][1][2] == "disc-xyz"

    def test_prefix_on_review(self, mock_platform: MockPlatform):
        mr_detail = MRDetail(number=5, diff_refs=None, changes=[])
        summary = "## General\nApplied the fix."
        post_review_comments(mock_platform, pid=1, mr_detail=mr_detail,
                             summary=summary,
                             prefix="\U0001f916 forgewright: pushed changes.\n\n")
        comment_calls = [c for c in mock_platform.calls
                         if c[0] == "comment_mr"]
        assert len(comment_calls) == 1
        body = comment_calls[0][1][2]
        assert body.startswith("\U0001f916 forgewright: pushed changes.")
        assert "Applied the fix" in body
