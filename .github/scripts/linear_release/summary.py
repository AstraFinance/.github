"""The run summary a deployer reads, checks and copies into #releases."""

from typing import Sequence

from .apply import Context, Outcome
from .git_range import Commit
from .model import Plan

PENDING_NOTES = "Notes are still generating. Open the release in Linear to copy them."

_NOTES_MESSAGES = {
    "pending": PENDING_NOTES,
    "missing": (
        "Linear has not started notes for this release. Check that the pipeline generates notes "
        "on completion, or generate them from the release."
    ),
    "unchanged": (
        "This release was already completed, so Linear did not regenerate its notes. "
        "If tickets were added, regenerate them from the release."
    ),
    "dry run": "Dry run: Linear generates notes when the release is completed.",
}

_RELEASE_STATUS = {True: "created", False: "existing release"}


def render(
    ctx: Context,
    plan: Plan,
    outcome: Outcome,
    not_found: Sequence[str],
    unticketed: Sequence[Commit],
) -> str:
    title = f"## Linear release · {ctx.service} {ctx.tag}" + (" (dry run)" if ctx.dry_run else "")
    lines = [title, "", f"`{ctx.previous_tag} → {ctx.tag}` · [compare]({ctx.compare_url})", ""]

    lines += ["### Releases", ""]
    if outcome.releases:
        lines += ["| Release | Tickets | |", "|---|---|---|"]
        for release in outcome.releases:
            name = f"[{release.name}]({release.url})" if release.url else release.name
            status = "would be created" if ctx.dry_run and release.created else _RELEASE_STATUS[release.created]
            lines.append(f"| {name} | {', '.join(release.tickets)} | {status} |")
    else:
        lines.append("No release: no ticket qualified.")
    lines.append("")

    lines += ["### Statuses", ""]
    if outcome.moved:
        grouped: dict[str, list[str]] = {}
        for identifier, state in outcome.moved:
            grouped.setdefault(state, []).append(identifier)
        lines += ["| Moves to | Tickets |", "|---|---|"]
        lines += [f"| {state} | {', '.join(ids)} |" for state, ids in grouped.items()]
    else:
        lines.append("No status changes.")
    lines.append("")

    if plan.notes:
        lines += ["### Not in a release", "", "| Ticket | Why |", "|---|---|"]
        for note in plan.notes:
            marker = "⚠️ " if note.attention else ""
            lines.append(f"| {marker}{note.identifier} | {note.reason} |")
        lines.append("")

    if not_found:
        lines += ["### Not found in Linear", "", ", ".join(not_found), ""]

    if unticketed:
        lines += ["### Commits with no ticket", ""]
        lines += [f"- `{commit.sha[:7]}` {commit.subject}" for commit in unticketed]
        lines.append("")

    if outcome.releases:
        lines += ["### Notes for #releases", "", "Review, then paste.", ""]
        for release in outcome.releases:
            body = release.notes if release.notes_status == "completed" and release.notes else (
                _NOTES_MESSAGES.get(release.notes_status, PENDING_NOTES)
            )
            link = release.url or "(not created yet)"
            # Four backticks so a fence inside generated notes cannot close this block.
            lines += ["````text", f"{release.name} · {link}", "", body, "````", ""]

    return "\n".join(lines)
