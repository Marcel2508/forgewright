"""Platform-agnostic data types for code hosting platforms."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class User:
    username: str
    id: int | str | None = None


@dataclass
class Project:
    id: int | str
    path: str
    web_url: str
    default_branch: str
    http_clone_url: str


@dataclass
class NotePosition:
    new_path: str | None = None
    old_path: str | None = None
    new_line: int | None = None
    old_line: int | None = None


@dataclass
class Note:
    id: int | str
    body: str
    created_at: str
    author: User
    system: bool = False
    type: str | None = None
    position: NotePosition | None = None


@dataclass
class Discussion:
    id: str
    notes: list[Note] = field(default_factory=list)


@dataclass
class DiffRefs:
    base_sha: str
    head_sha: str
    start_sha: str | None = None


@dataclass
class Issue:
    number: int
    title: str
    description: str | None
    web_url: str
    updated_at: str
    labels: list[str] = field(default_factory=list)


@dataclass
class MergeRequest:
    number: int
    title: str
    description: str | None
    web_url: str
    source_branch: str
    target_branch: str
    updated_at: str
    sha: str
    author: User
    labels: list[str] = field(default_factory=list)
    reviewers: list[User] = field(default_factory=list)
    assignees: list[User] = field(default_factory=list)
    diff_refs: DiffRefs | None = None


@dataclass
class DiffChange:
    diff: str
    old_path: str
    new_path: str
    renamed_file: bool = False
    deleted_file: bool = False
    new_file: bool = False


@dataclass
class MRDetail:
    """Extended MR data including diff changes (from mr_changes endpoint)."""
    number: int
    diff_refs: DiffRefs | None
    changes: list[DiffChange] = field(default_factory=list)


@dataclass
class Pipeline:
    id: int | str | None
    sha: str
    status: str
    web_url: str


@dataclass
class Job:
    id: int | str
    name: str
    stage: str
    status: str
