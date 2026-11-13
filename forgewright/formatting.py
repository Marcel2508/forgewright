"""Formatting functions for notes, discussions, and diffs."""

from __future__ import annotations

from forgewright.helpers import parse_ts, shortdt
from forgewright.types import DiffChange, Discussion, Note


def notes_from_discussions(discussions: list[Discussion]) -> list[Note]:
    """Flatten discussions into a chronologically-sorted flat list of notes.

    Sorting by ``created_at`` is what makes "newest note" well-defined for
    fingerprinting: GitHub serves issue comments and review comments from two
    endpoints with independent ID counters, and a reply can land on an older
    GitLab thread, so list position is not chronological on its own.
    """
    out: list[Note] = []
    for d in discussions:
        out.extend(d.notes)
    out.sort(key=lambda n: (parse_ts(n.created_at), str(n.id)))
    return out


def format_notes(notes: list[Note], limit: int = 40) -> str:
    if not notes:
        return "_(no comments)_"
    # Filter system notes BEFORE slicing so a burst of system notes can't push
    # the user's actual request out of the window.
    user_notes = [n for n in notes if not n.system]
    lines = []
    for n in user_notes[-limit:]:
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
                f"\n_(excerpt ends here \u2014 {len(changes) - len(blocks)} "
                f"more file(s) are only in the full diff file)_")
            break
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks)


def _diff_stats(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in (diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def unified_diff_file(changes: list[DiffChange]) -> str:
    """The complete MR diff as one git-style patch (never truncated).

    Built from the platform's per-file patches, so line numbers match the
    positions inline review comments must use.
    """
    parts: list[str] = []
    for c in changes:
        old = c.old_path or c.new_path
        new = c.new_path or c.old_path
        parts.append(f"diff --git a/{old} b/{new}")
        if c.new_file:
            parts.append("new file")
        elif c.deleted_file:
            parts.append("deleted file")
        elif c.renamed_file:
            parts.append(f"rename from {old}\nrename to {new}")
        parts.append(f"--- {'/dev/null' if c.new_file else 'a/' + old}")
        parts.append(f"+++ {'/dev/null' if c.deleted_file else 'b/' + new}")
        body = (c.diff or "").rstrip("\n")
        parts.append(body if body else "(binary or empty change)")
    return "\n".join(parts) + "\n" if parts else ""


def format_review_diff(changes: list[DiffChange], diff_path: str,
                       base_sha: str | None = None,
                       head_sha: str | None = None,
                       inline_max_chars: int = 40000) -> str:
    """Diff section of the review prompt.

    A complete file overview, pointers to the full diff (file + git), and the
    diff itself inline up to *inline_max_chars* — so normal MRs need no tool
    calls, and big ones are never silently cut off.
    """
    if not changes:
        return "_(no changes)_"
    lines = [f"## Changed files ({len(changes)})"]
    for c in changes:
        added, removed = _diff_stats(c.diff)
        kind = ("new" if c.new_file else "deleted" if c.deleted_file
                else f"renamed from `{c.old_path}`" if c.renamed_file else "")
        tag = f" — {kind}" if kind else ""
        lines.append(f"- `{c.new_path or c.old_path}` (+{added} −{removed})"
                     f"{tag}")
    lines.append("")
    lines.append(f"The **complete** diff of all {len(changes)} file(s) is in "
                 f"`{diff_path}` (never truncated — read it, or parts of it, "
                 f"whenever the excerpt below is cut off).")
    if base_sha and head_sha:
        lines.append(
            f"You can also use git directly in this checkout: "
            f"`git diff {base_sha}...{head_sha}` (add `-- <path>` for one "
            f"file), `git log --oneline {base_sha}..{head_sha}`. If git "
            f"cannot find a commit, fall back to `{diff_path}`.")
    lines.append("")
    lines.append("## Diff")
    lines.append(format_diff_for_review(changes, max_chars=inline_max_chars))
    return "\n".join(lines)
