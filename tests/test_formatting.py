"""Tests for forgewright.formatting."""

import pytest

from forgewright.formatting import (
    format_diff_for_review,
    format_discussions,
    format_notes,
    notes_from_discussions,
)
from forgewright.types import DiffChange, Discussion, NotePosition
from tests.conftest import make_note


class TestFormatNotes:
    def test_empty(self):
        assert format_notes([]) == "_(no comments)_"

    def test_system_notes_filtered(self):
        notes = [
            make_note(author="alice", body="hello"),
            make_note(author="system", body="changed label", system=True),
        ]
        result = format_notes(notes)
        assert "hello" in result
        assert "changed label" not in result

    def test_limit(self):
        notes = [make_note(note_id=i, body=f"note {i}") for i in range(10)]
        result = format_notes(notes, limit=3)
        assert "note 7" in result
        assert "note 8" in result
        assert "note 9" in result
        assert "note 0" not in result

    def test_all_system_notes(self):
        notes = [make_note(system=True), make_note(system=True)]
        assert format_notes(notes) == "_(no user comments)_"

    def test_author_and_timestamp(self):
        notes = [make_note(author="bob", created_at="2024-03-15T10:00:00Z")]
        result = format_notes(notes)
        assert "@bob" in result


class TestNotesFromDiscussions:
    def test_empty(self):
        assert notes_from_discussions([]) == []

    def test_flattens(self):
        discussions = [
            Discussion(id="d1", notes=[make_note(note_id=1),
                                       make_note(note_id=2)]),
            Discussion(id="d2", notes=[make_note(note_id=3)]),
        ]
        result = notes_from_discussions(discussions)
        assert len(result) == 3

    def test_empty_discussions(self):
        discussions = [Discussion(id="d1", notes=[]),
                       Discussion(id="d2", notes=[])]
        result = notes_from_discussions(discussions)
        assert result == []


class TestFormatDiscussions:
    def test_empty(self):
        assert format_discussions([]) == "_(no comments)_"

    def test_discussion_id_present(self):
        discussions = [Discussion(
            id="disc-abc",
            notes=[make_note(author="alice", body="Review comment")],
        )]
        result = format_discussions(discussions)
        assert "[discussion:disc-abc]" in result
        assert "Review comment" in result

    def test_bot_notes_filtered(self):
        discussions = [Discussion(
            id="disc-1",
            notes=[make_note(author="forgewright", body="Bot reply")],
        )]
        result = format_discussions(discussions, bot_username="forgewright")
        assert result == "_(no user comments)_"

    def test_system_notes_skipped(self):
        discussions = [Discussion(
            id="disc-1",
            notes=[make_note(author="alice", body="Good", system=True)],
        )]
        result = format_discussions(discussions)
        assert result == "_(no user comments)_"

    def test_inline_diff_note(self):
        discussions = [Discussion(
            id="disc-1",
            notes=[make_note(
                author="alice",
                body="Bug here",
                note_type="DiffNote",
                position=NotePosition(new_path="src/foo.py", new_line=42),
            )],
        )]
        result = format_discussions(discussions)
        assert "src/foo.py" in result
        assert "line 42" in result

    def test_limit(self):
        discussions = [
            Discussion(
                id=f"disc-{i}",
                notes=[make_note(note_id=i, body=f"Comment {i}")],
            )
            for i in range(10)
        ]
        result = format_discussions(discussions, limit=3)
        assert "Comment 0" in result
        assert "Comment 2" in result
        assert "Comment 5" not in result

    def test_follow_up_notes(self):
        discussions = [Discussion(
            id="disc-1",
            notes=[
                make_note(author="alice", body="First note"),
                make_note(author="bob", body="Reply to alice"),
            ],
        )]
        result = format_discussions(discussions)
        assert "Reply to alice" in result


class TestFormatDiffForReview:
    def test_empty(self):
        assert format_diff_for_review([]) == "_(no changes)_"

    def test_new_file(self):
        changes = [DiffChange(new_file=True, new_path="src/new.py",
                              old_path="src/new.py", diff="+hello")]
        result = format_diff_for_review(changes)
        assert "NEW FILE" in result
        assert "src/new.py" in result

    def test_deleted_file(self):
        changes = [DiffChange(deleted_file=True, old_path="src/old.py",
                              new_path="src/old.py", diff="-goodbye")]
        result = format_diff_for_review(changes)
        assert "DELETED" in result

    def test_renamed_file(self):
        changes = [DiffChange(renamed_file=True, old_path="old.py",
                              new_path="new.py", diff="")]
        result = format_diff_for_review(changes)
        assert "RENAMED" in result
        assert "old.py" in result
        assert "new.py" in result

    def test_modified_file(self):
        changes = [DiffChange(new_path="src/foo.py", old_path="src/foo.py",
                              diff="@@ -1,3 +1,3 @@\n-old\n+new")]
        result = format_diff_for_review(changes)
        assert "Modified" in result

    def test_truncation(self):
        changes = [
            DiffChange(new_path=f"file{i}.py", old_path=f"file{i}.py",
                       diff="x" * 1000)
            for i in range(100)
        ]
        result = format_diff_for_review(changes, max_chars=5000)
        assert "truncated" in result
        assert "omitted" in result
