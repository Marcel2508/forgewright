"""Post AI agent results back to the code hosting platform."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from forgewright.parsing import parse_review_comments, parse_summary_replies
from forgewright.types import MRDetail

if TYPE_CHECKING:
    from forgewright.platform.base import Platform


def post_mr_responses(platform: Platform, pid: int | str, mr_number: int,
                      summary: str, prefix: str = "") -> None:
    """Parse summary for discussion replies and post them.

    Replies matching '## Reply to discussion <id>' are posted as thread replies.
    Everything else (## General or unstructured) is posted as a top-level comment.
    """
    replies, general = parse_summary_replies(summary)

    for disc_id, body in replies.items():
        if not body:
            continue
        try:
            platform.reply_to_discussion(
                pid, mr_number, disc_id,
                f"\U0001f916 forgewright:\n\n{body}")
            logging.info("replied to discussion %s on MR %d",
                         disc_id, mr_number)
        except Exception as e:
            logging.warning("failed to reply to discussion %s: %s \u2014 "
                            "will include in top-level comment", disc_id, e)
            general = f"{general}\n\n---\n**Reply to discussion {disc_id}:**\n{body}"

    if general.strip() or prefix:
        body = general.strip() or ""
        comment = f"{prefix}{body}" if prefix else body
        if comment.strip():
            platform.comment_mr(pid, mr_number, comment)


def post_review_comments(platform: Platform, pid: int | str,
                         mr_detail: MRDetail, summary: str,
                         prefix: str = "") -> None:
    """Parse review summary and post inline discussions, threaded replies,
    and a general comment."""
    inlines, general_from_inlines = parse_review_comments(summary)
    replies, general_from_replies = parse_summary_replies(summary)
    general = general_from_inlines or general_from_replies

    diff_refs = mr_detail.diff_refs

    old_path_map: dict[str, str] = {}
    for c in mr_detail.changes:
        if c.new_path and c.old_path:
            old_path_map[c.new_path] = c.old_path

    mr_number = mr_detail.number
    posted_inline = 0

    for comment in inlines:
        if not comment["body"]:
            continue
        body = f"\U0001f916 forgewright review:\n\n{comment['body']}"
        file_path = comment["file_path"]
        old_path = old_path_map.get(file_path, file_path)
        position = None
        if diff_refs:
            position = platform.build_inline_comment_position(
                diff_refs, file_path, old_path, comment["line"])
        if position is not None:
            try:
                platform.create_mr_discussion(pid, mr_number, body,
                                              position=position)
                posted_inline += 1
                logging.info("posted inline comment on %s:%d in MR %d",
                             comment["file_path"], comment["line"], mr_number)
                continue
            except Exception as e:
                logging.warning(
                    "inline discussion failed for %s:%d: %s \u2014 "
                    "falling back to general comment",
                    comment["file_path"], comment["line"], e)

        general = (
            f"{general}\n\n---\n"
            f"**`{comment['file_path']}:{comment['line']}`:**\n"
            f"{comment['body']}")

    for disc_id, body in replies.items():
        if not body:
            continue
        try:
            platform.reply_to_discussion(
                pid, mr_number, disc_id,
                f"\U0001f916 forgewright:\n\n{body}")
            logging.info("replied to discussion %s on MR %d",
                         disc_id, mr_number)
        except Exception as e:
            logging.warning("failed to reply to discussion %s: %s \u2014 "
                            "will include in top-level comment", disc_id, e)
            general = (f"{general}\n\n---\n"
                       f"**Reply to discussion {disc_id}:**\n{body}")

    if general.strip() or prefix:
        body_text = general.strip() or ""
        if prefix:
            comment = f"{prefix}{body_text}" if body_text else prefix.strip()
        else:
            comment = f"\U0001f916 forgewright review:\n\n{body_text}"
        if comment.strip():
            platform.comment_mr(pid, mr_number, comment)
    logging.info("review posted on MR %d: %d inline, %d replies, general=%s",
                 mr_number, posted_inline, len(replies), bool(general.strip()))
