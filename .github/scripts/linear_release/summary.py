"""The run summary a deployer reads before releasing in Linear."""

from typing import Sequence

from .apply import Context, Outcome, ReleaseResult
from .git_range import Commit
from .model import Plan


def _status(ctx: Context, release: ReleaseResult) -> str:
    if release.released:
        return "Released"
    if release.created:
        return "would be created, In Progress" if ctx.dry_run else "In Progress, created by this run"
    return "In Progress"


def _link(release: ReleaseResult) -> str:
    return f"[{release.name}]({release.url})" if release.url else release.name


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
        lines += ["| Release | Tickets | Status |", "|---|---|---|"]
        for release in outcome.releases:
            lines.append(f"| {_link(release)} | {', '.join(release.tickets)} | {_status(ctx, release)} |")
        for release in outcome.releases:
            if release.removed:
                verb = "Would remove" if ctx.dry_run else "Removed"
                lines.append(f"\n{verb} from {release.name}: {', '.join(release.removed)} (no longer qualifies).")
            # A released release keeps what shipped whatever the plan says now.
            if not release.tickets and not release.released:
                lines.append(f"\n{release.name} has no tickets left. Cancel it in Linear.")
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

    attention = [n for n in plan.notes if n.attention]
    if attention:
        lines += ["### Needs attention", "", "| Ticket | Problem | What to do |", "|---|---|---|"]
        lines += [f"| {n.identifier} | {n.reason} | {n.hint} |" for n in attention]
        if outcome.commented:
            told = ", ".join(outcome.commented)
            lines.append("")
            lines.append(
                f"A real run would comment on {told} in Linear." if ctx.dry_run
                else f"Commented on {told} in Linear so their owners see it."
            )
        lines.append("")

    left_out = [n for n in plan.notes if not n.attention]
    if left_out:
        lines += ["### Left out of the release", "", "| Ticket | Why |", "|---|---|"]
        lines += [f"| {n.identifier} | {n.reason} |" for n in left_out]
        lines.append("")

    if not_found:
        lines += ["### Not found in Linear", "", ", ".join(not_found), ""]

    if unticketed:
        lines += ["### Commits with no ticket", ""]
        lines += [f"- `{commit.sha[:7]}` {commit.subject}" for commit in unticketed]
        lines.append("")

    open_releases = [r for r in outcome.releases if not r.released and r.tickets]
    released = [r for r in outcome.releases if r.released]
    if open_releases or released:
        lines += ["### Next steps", ""]
        step = 1
        if attention:
            lines.append(f"{step}. Fix the tickets under **Needs attention**, then run this again with the same tag.")
            step += 1
        for release in open_releases:
            lines.append(
                f"{step}. Review {_link(release)} in Linear and move it to **Released**. "
                "Linear then writes its notes and moves its tickets to Done."
            )
            step += 1
        for release in released:
            lines.append(
                f"{step}. {_link(release)} was already Released. If this run added tickets, regenerate its notes in Linear."
            )
            step += 1
        lines.append(f"{step}. Copy each release's notes from Linear into #releases.")
        lines.append("")

    return "\n".join(lines)
