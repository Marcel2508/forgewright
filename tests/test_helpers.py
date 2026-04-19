"""Tests for forgewright.helpers."""

import pytest

from forgewright.helpers import has_mention, shortdt, slugify


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify("Fix: bug #42!") == "fix-bug-42"

    def test_max_len(self):
        result = slugify("a very long title that should be truncated", max_len=10)
        assert len(result) <= 10
        assert result == "a-very-lon"

    def test_max_len_no_trailing_dash(self):
        result = slugify("hello world foo", max_len=12)
        # "hello-world-" would be 12, trailing dash stripped -> "hello-world"
        assert not result.endswith("-")

    def test_empty(self):
        assert slugify("") == "task"

    def test_only_special(self):
        assert slugify("!!!") == "task"

    def test_unicode(self):
        assert slugify("Bugfix für Ärger") == "bugfix-f-r-rger"


class TestHasMention:
    def test_present(self):
        assert has_mention("Hey @forgewright please help", "forgewright")

    def test_absent(self):
        assert not has_mention("No mention here", "forgewright")

    def test_none_input(self):
        assert not has_mention(None, "forgewright")

    def test_empty_string(self):
        assert not has_mention("", "forgewright")

    def test_case_insensitive(self):
        assert has_mention("Hey @Forgewright do this", "forgewright")
        assert has_mention("Hey @FORGEWRIGHT do this", "forgewright")

    def test_custom_username(self):
        assert has_mention("Hey @mybot help", "mybot")
        assert not has_mention("Hey @forgewright help", "mybot")

    def test_not_part_of_email(self):
        # @forgewright in an email address should NOT match
        assert not has_mention("user@forgewright.com", "forgewright")

    def test_at_start_of_text(self):
        assert has_mention("@forgewright fix this", "forgewright")

    def test_special_chars_in_username(self):
        # Regex special chars should be escaped
        assert has_mention("Hey @bot.v2 help", "bot.v2")


class TestShortdt:
    def test_normal(self):
        assert shortdt("2024-01-15T10:30:45.123Z") == "2024-01-15 10:30:45"

    def test_none(self):
        assert shortdt(None) == "\u2014"

    def test_empty(self):
        assert shortdt("") == "\u2014"

    def test_no_fractional(self):
        assert shortdt("2024-01-15T10:30:45Z") == "2024-01-15 10:30:45Z"
