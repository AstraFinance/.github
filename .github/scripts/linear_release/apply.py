"""Carry out a release plan in Linear, in an order that is safe to repeat.

Releases are left In Progress: a person reviews each one in Linear and moves it
to Released, which makes Linear write its notes and move its tickets to Done.
Until then a re-run can still correct the release, adding tickets that now
qualify and removing ones that no longer do.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .config import Config
from .linear_api import LinearError, Pipeline
from .model import Audience, Note, Plan, ReleaseRef

AUDIENCES: tuple[Audience, ...] = ("astra", "internal")


@dataclass(frozen=True)
class Context:
    service: str
    tag: str
    previous_tag: str
    commit_sha: str
    compare_url: str
    today: date
    dry_run: bool

    @property
    def marker(self) -> str:
        """How a release made for this deploy is recognized on a re-run."""
        return f"{self.service} {self.tag}"

    @property
    def comment_header(self) -> str:
        """How this deploy's comment on a ticket is recognized, so it is posted once."""
        return f"Linear release check · {self.marker}"


@dataclass(frozen=True)
class ReleaseResult:
    audience: Audience
    name: str
    url: str
    tickets: tuple[str, ...]
    created: bool
    # A person has already moved it to Released.
    released: bool
    # Tickets taken out because they no longer qualify.
    removed: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    releases: tuple[ReleaseResult, ...]
    moved: tuple[tuple[str, str], ...]
    # Tickets whose owners were asked to fix something (in a dry run, would be asked).
    commented: tuple[str, ...]


def release_name(prefix: str, day: date, existing: Iterable[str]) -> str:
    base = f"{prefix} {day:%Y.%m.%d}"
    suffixed = re.compile(rf"^{re.escape(base)}\.(\d+)$")
    taken = 0
    for name in existing:
        if name == base:
            taken = max(taken, 1)
        elif match := suffixed.match(name):
            taken = max(taken, int(match.group(1)))
    return base if taken == 0 else f"{base}.{taken + 1}"


def _pipeline_name(cfg: Config, audience: Audience) -> str:
    return cfg.pipeline_astra if audience == "astra" else cfg.pipeline_internal


def _prefix(cfg: Config, audience: Audience) -> str:
    return cfg.prefix_astra if audience == "astra" else cfg.prefix_internal


def find_own_releases(client, cfg: Config, ctx: Context) -> dict[Audience, tuple[ReleaseRef, bool]]:
    found: dict[Audience, tuple[ReleaseRef, bool]] = {}
    for audience in AUDIENCES:
        pipeline = client.fetch_pipeline(_pipeline_name(cfg, audience))
        own = client.find_own_release(pipeline.id, ctx.tag, ctx.marker)
        if own:
            found[audience] = own
    return found


def comment_body(ctx: Context, note: Note) -> str:
    return f"**{ctx.comment_header}**\n\nLeft out of the Linear release for {ctx.marker}: {note.reason}.\n\n{note.hint}"


@dataclass
class _Target:
    audience: Audience
    pipeline: Pipeline
    name: str
    ref: ReleaseRef | None
    created: bool
    released: bool
    removed: tuple[str, ...] = ()


def apply_plan(
    plan: Plan,
    client,
    cfg: Config,
    ctx: Context,
    own: dict[Audience, tuple[ReleaseRef, bool]],
) -> Outcome:
    # Resolved first, so a renamed status stops the run before anything changes.
    state_ids: dict[str, str] = {}
    if plan.moves:
        state_ids = client.state_ids(cfg.team_key)
        missing = sorted({m.to_state for m in plan.moves} - set(state_ids))
        if missing:
            raise LinearError(f"SetState: no workflow state named {', '.join(map(repr, missing))}")

    targets: list[_Target] = []
    for audience in AUDIENCES:
        planned = plan.releases.get(audience, ())
        # An open release from an earlier run is visited even when nothing
        # qualifies now, so the tickets that stopped qualifying come out of it.
        if not planned and audience not in own:
            continue
        pipeline = client.fetch_pipeline(_pipeline_name(cfg, audience))
        if audience in own:
            ref, released = own[audience]
            targets.append(_Target(audience, pipeline, ref.name, ref, False, released))
            continue
        prefix = _prefix(cfg, audience)
        base = f"{prefix} {ctx.today:%Y.%m.%d}"
        name = release_name(prefix, ctx.today, client.release_names(pipeline.id, base))
        ref = None
        if not ctx.dry_run:
            description = f"{ctx.marker}\n\n{ctx.compare_url}"
            ref = client.create_release(pipeline, name, ctx.tag, ctx.commit_sha, description, ctx.today.isoformat())
        targets.append(_Target(audience, pipeline, name, ref, True, False))

    # A release a person has already released is the record of what shipped,
    # so it is never trimmed; only open releases are corrected. Its stage is
    # read again just before a trim, since a person may release it while this
    # run is going. That narrows the window without closing it: Linear offers
    # no way to make the read and the detach one step. A release found released
    # stops the run, because the plan sent its tickets to Deployed, and once
    # Linear has moved a release's tickets to Done it does not move later ones;
    # a re-run plans them to Done.
    for target in targets:
        if target.created or target.released:
            continue
        planned_ids = {t.identifier for t in plan.releases.get(target.audience, ())}
        attached = client.release_issues(target.ref.id)
        stale = sorted(identifier for identifier in attached if identifier not in planned_ids)
        if stale and not ctx.dry_run and client.release_completed(target.ref.id):
            raise LinearError(
                f"Release {target.name!r} was moved to Released while this run was going, so its tickets "
                f"were planned to the wrong status. Run again."
            )
        target.removed = tuple(stale)
        if not ctx.dry_run:
            for identifier in stale:
                client.detach(attached[identifier], target.ref.id)

    if not ctx.dry_run:
        for target in targets:
            for t in plan.releases.get(target.audience, ()):
                if target.ref.id not in t.release_ids:
                    client.attach(t.uuid, target.ref.id)
        for move in plan.moves:
            client.set_state(move.ticket.uuid, state_ids[move.to_state])

    commented = []
    for note in plan.notes:
        if not (note.attention and note.uuid and note.hint):
            continue
        if client.has_comment(note.uuid, ctx.comment_header):
            continue
        if not ctx.dry_run:
            client.comment(note.uuid, comment_body(ctx, note))
        commented.append(note.identifier)

    results = tuple(
        ReleaseResult(
            audience=target.audience,
            name=target.name,
            url=target.ref.url if target.ref else "",
            tickets=tuple(t.identifier for t in plan.releases.get(target.audience, ())),
            created=target.created,
            released=target.released,
            removed=target.removed,
        )
        for target in targets
    )
    return Outcome(
        releases=results,
        moved=tuple((m.ticket.identifier, m.to_state) for m in plan.moves),
        commented=tuple(commented),
    )
