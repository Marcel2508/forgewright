"""Formatting functions for notes, discussions, and diffs."""

from __future__ import annotations

from forgewright.helpers import shortdt
from forgewright.types import DiffChange, Discussion, Note


def notes_from_discussions(discussions: list[Discussion]) -> list[Note]:
    """Flatten discussions into a flat list of notes (for fingerprinting etc.)."""
    out = []
    for d in discussions:
        for n in d.notes:
            out.append(n)
    return out


def format_notes(notes: list[Note], limit: int = 40) -> str:
    if not notes:
        return "_(no comments)_"
    lines = []
    for n in notes[-limit:]:
        if n.system:
            continue
        ts = shortdt(n.created_at)
        author = n.author.username
        body = n.body.strip()
        lines.append(f"### @{author} \u2014 {ts}\n{body}")
    return "\n\n".join(lines) if lines else "_(no user comments)_"


def _format_note_header(note: Note) -> str:
    """Format a single note with optional inline diff context."""
    ts = shortdt(note.created_at)
    author = note.author.username
    body = note.body.strip()
    if note.type == "DiffNote" and note.position:
        pos = note.position
        path = pos.new_path or pos.old_path or "?"
        if pos.new_line:
            loc = f"`{path}` line {pos.new_line}"
        elif pos.old_line:
            loc = f"`{path}` (removed line {pos.old_line})"
        else:
            loc = f"`{path}`"
        return f"### @{author} \u2014 {ts}\n**File:** {loc}\n{body}"
    return f"### @{author} \u2014 {ts}\n{body}"


def format_discussions(discussions: list[Discussion], limit: int = 40,
                       bot_username: str | None = None) -> str:
    """Format MR discussions with inline context and discussion IDs."""
    if not discussions:
        return "_(no comments)_"
    blocks = []
    count = 0
    for d in discussions:
        if not d.notes:
            continue
        user_notes = [n for n in d.notes
                      if not n.system
                      and (not bot_username
                           or n.author.username != bot_username)]
        if not user_notes:
            continue
        if count >= limit:
            break

        first = d.notes[0]
        header = _format_note_header(first)

        block_lines = [f"**[discussion:{d.id}]**", header]

        for reply in d.notes[1:]:
            if reply.system:
                continue
            r_author = reply.author.username
            r_ts = shortdt(reply.created_at)
            r_body = reply.body.strip()
            block_lines.append(f"> **@{r_author}** ({r_ts}): {r_body}")

        blocks.append("\n".join(block_lines))
        count += 1

    return "\n\n---\n\n".join(blocks) if blocks else "_(no user comments)_"


def format_diff_for_review(changes: list[DiffChange],
                           max_chars: int = 80000) -> str:
    """Format MR changes into a readable diff block for review."""
    if not changes:
        return "_(no changes)_"

    blocks = []
    total = 0
    for c in changes:
        header_parts = []
        if c.new_file:
            header_parts.append(f"**NEW FILE:** `{c.new_path}`")
        elif c.deleted_file:
            header_parts.append(f"**DELETED:** `{c.old_path}`")
        elif c.renamed_file:
            header_parts.append(
                f"**RENAMED:** `{c.old_path}` \u2192 `{c.new_path}`")
        else:
            header_parts.append(f"**Modified:** `{c.new_path}`")

        block = f"{' '.join(header_parts)}\n```diff\n{c.diff}\n```"

        if total + len(block) > max_chars:
            blocks.append(
                f"\n_(diff truncated \u2014 {len(changes) - len(blocks)} "
                f"file(s) omitted due to size)_")
            break
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks)
