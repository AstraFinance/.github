"""What a deploy shipped, read from the repository's tags and commit subjects.

Git history is the record of what reached Production; Linear is not, so the
ticket list starts here.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_VERSION = re.compile(r"^(v?)(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


def _parse(tag: str) -> tuple[str, tuple[int, int, int]] | None:
    match = _VERSION.match(tag)
    if not match:
        return None
    prefix, major, minor, patch = match.groups()
    return prefix, (int(major), int(minor), int(patch))


def previous_tag(tags: Iterable[str], current: str) -> str:
    """The highest tag below `current` written in the same style.

    Repositories differ on the `v` prefix, so a tag in the other style is never
    a candidate, and versions compare numerically so 1.10.0 follows 1.9.0.
    """
    parsed_current = _parse(current)
    if parsed_current is None:
        raise ValueError(f"{current!r} is not a version tag")
    prefix, version = parsed_current
    lower = []
    for tag in tags:
        parsed = _parse(tag)
        if parsed and parsed[0] == prefix and parsed[1] < version:
            lower.append((parsed[1], tag))
    if not lower:
        raise ValueError(f"no tag lower than {current!r}; pass the previous tag explicitly")
    return max(lower)[1]


def ticket_ids(subjects: Iterable[str], team_key: str) -> list[str]:
    """Ticket ids in first-seen order, one entry per ticket.

    Branch names in merge commits are lower case, so matching ignores case and
    the result is normalized to the form Linear displays.
    """
    pattern = re.compile(rf"\b{re.escape(team_key)}-(\d+)\b", re.IGNORECASE)
    seen: dict[str, None] = {}
    for subject in subjects:
        for number in pattern.findall(subject):
            seen.setdefault(f"{team_key.upper()}-{int(number)}", None)
    return list(seen)


def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def read_tags(repo_dir: Path) -> list[str]:
    return _git(repo_dir, "tag", "--list").split()


def read_commits(repo_dir: Path, base: str, head: str) -> list[Commit]:
    output = _git(repo_dir, "log", "--format=%H%x09%s", f"{base}..{head}")
    commits = []
    for line in output.splitlines():
        sha, _, subject = line.partition("\t")
        commits.append(Commit(sha=sha, subject=subject))
    return commits


def tag_sha(repo_dir: Path, tag: str) -> str:
    return _git(repo_dir, "rev-list", "-n1", tag).strip()
