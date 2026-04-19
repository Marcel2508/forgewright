"""Parsing of structured output from AI agent summaries."""

from __future__ import annotations

import re
from pathlib import Path

REPLY_SECTION_RE = re.compile(
    r"^##\s+Reply to discussion\s+(\S+)\s*$", re.MULTILINE)

INLINE_SECTION_RE = re.compile(
    r"^##\s+Inline:\s+(.+?):(\d+)\s*$", re.MULTILINE)


def read_summary(wt: Path) -> str:
    p = wt / ".claude" / "last-run-summary.md"
    if p.exists():
        return p.read_text().strip()
    return ""


def parse_summary_replies(summary: str) -> tuple[dict[str, str], str]:
    """Parse structured replies from the summary.

    Returns (replies_dict, general_text) where replies_dict maps
    discussion_id -> reply body, and general_text is everything else.
    """
    if not summary:
        return {}, ""

    replies: dict[str, str] = {}
    general_parts: list[str] = []

    # Split on ## Reply to discussion <id> and ## General headers
    parts = REPLY_SECTION_RE.split(summary)
    # parts[0] is text before first "## Reply to discussion" (if any)
    # then alternating: discussion_id, body, discussion_id, body, ...

    if len(parts) == 1:
        # No structured replies found -- strip a leading ## General header
        # if present, then treat everything as general.
        stripped = re.sub(r"^##\s+General\s*\n", "", summary, count=1,
                          flags=re.MULTILINE).strip()
        return {}, stripped

    # Text before first reply section
    preamble = parts[0].strip()
    if preamble:
        general_parts.append(preamble)

    i = 1
    while i < len(parts) - 1:
        disc_id = parts[i].strip()
        body = parts[i + 1].strip()
        # Check if this body contains a "## General" section
        gen_split = re.split(r"^##\s+General\s*$", body, maxsplit=1,
                             flags=re.MULTILINE)
        if len(gen_split) > 1:
            replies[disc_id] = gen_split[0].strip()
            if gen_split[1].strip():
                general_parts.append(gen_split[1].strip())
        else:
            replies[disc_id] = body
        i += 2

    # Check preamble for ## General
    if not general_parts:
        gen_match = re.split(r"^##\s+General\s*$", preamble,
                             maxsplit=1, flags=re.MULTILINE) if preamble else []
        if len(gen_match) > 1:
            general_parts = [gen_match[1].strip()]

    return replies, "\n\n".join(general_parts)


def parse_review_comments(summary: str) -> tuple[list[dict], str]:
    """Parse structured inline review comments from AI agent review output.

    Returns (inline_comments, general_text) where inline_comments is a list
    of dicts with keys: file_path, line, body.
    """
    if not summary:
        return [], ""

    inlines: list[dict] = []
    general_parts: list[str] = []

    # Split on both ## Inline: and ## General headers
    all_section_re = re.compile(
        r"^##\s+(?:Inline:\s+(.+?):(\d+)|General)\s*$", re.MULTILINE)

    parts = all_section_re.split(summary)
    # parts structure: [preamble, file1, line1, body1, file2, line2, body2, ...]
    # For ## General sections: file=None, line=None

    if len(parts) == 1:
        # No structured sections found
        stripped = re.sub(r"^##\s+General\s*\n", "", summary, count=1,
                          flags=re.MULTILINE).strip()
        return [], stripped

    # Preamble (text before first section)
    preamble = parts[0].strip()
    if preamble:
        general_parts.append(preamble)

    i = 1
    while i < len(parts):
        file_path = parts[i] if i < len(parts) else None
        line_str = parts[i + 1] if i + 1 < len(parts) else None
        body = parts[i + 2].strip() if i + 2 < len(parts) else ""
        i += 3

        if file_path and line_str:
            # Inline comment
            inlines.append({
                "file_path": file_path.strip(),
                "line": int(line_str),
                "body": body,
            })
        else:
            # General section (file_path and line_str are None)
            if body:
                general_parts.append(body)

    return inlines, "\n\n".join(general_parts)
