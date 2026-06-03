"""Parsing of structured output from AI agent summaries."""

from __future__ import annotations

import re
from pathlib import Path

REPLY_SECTION_RE = re.compile(
    r"^##\s+Reply to discussion\s+(\S+)\s*$", re.MULTILINE)

INLINE_SECTION_RE = re.compile(
    r"^##\s+Inline:\s+(.+?):(\d+)\s*$", re.MULTILINE)

# One regex that recognizes all three section headers, so a single pass can
# partition a summary into inline comments, threaded replies, and general text
# without double-counting (the cause of inline comments being posted twice).
_ANY_SECTION_RE = re.compile(
    r"^##[ \t]+(?:"
    r"Inline:[ \t]+(?P<file>.+?):(?P<line>\d+)"
    r"|Reply to discussion[ \t]+(?P<disc>\S+)"
    r"|General"
    r")[ \t]*$",
    re.MULTILINE,
)


def parse_sections(summary: str) -> tuple[list[dict], dict[str, str], str]:
    """Partition an agent summary into (inlines, replies, general) in one pass.

    - inlines: list of ``{"file_path", "line", "body"}`` from ``## Inline:`` blocks
    - replies: ``{discussion_id: body}`` from ``## Reply to discussion`` blocks
    - general: everything else (preamble + ``## General`` blocks), joined

    Unlike calling the per-type parsers separately, this never attributes the
    same text to more than one bucket.
    """
    if not summary:
        return [], {}, ""

    matches = list(_ANY_SECTION_RE.finditer(summary))
    if not matches:
        stripped = re.sub(r"^##\s+General\s*\n", "", summary, count=1,
                          flags=re.MULTILINE).strip()
        return [], {}, stripped

    inlines: list[dict] = []
    replies: dict[str, str] = {}
    general_parts: list[str] = []

    preamble = summary[:matches[0].start()].strip()
    if preamble:
        general_parts.append(preamble)

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(summary)
        body = summary[m.end():end].strip()
        if m.group("file") is not None:
            inlines.append({
                "file_path": m.group("file").strip(),
                "line": int(m.group("line")),
                "body": body,
            })
        elif m.group("disc") is not None:
            replies[m.group("disc").strip()] = body
        else:  # ## General
            if body:
                general_parts.append(body)

    return inlines, replies, "\n\n".join(general_parts)


def read_summary(wt: Path) -> str:
    p = wt / ".claude" / "last-run-summary.md"
    if p.exists():
        # errors="replace": a non-UTF-8 summary must not crash the handler
        # (which would abort the run and re-process the item every cycle).
        return p.read_text(encoding="utf-8", errors="replace").strip()
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
